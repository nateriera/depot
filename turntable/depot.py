#!/usr/bin/env python3
"""Depot turntable (S1): yard CLI, Codex dispatch, verification-lite, host-side commit.

Standard library only. Formats are documented in FORMATS.md.
"""
import argparse
import base64
import binascii
import ctypes
import errno
import hashlib
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_TIMEOUT_S = 900
DEFAULT_QUIET_AFTER_S = 300  # spec §7: five minutes
REFUSAL_CAP = 3  # consecutive pre-run dispatch refusals before the daemon blocks the item
INTERRUPTED_GRACE_S = 300  # allow host-side cleanup after the recorded run timeout
TEST_TIMEOUT_S = 300
PROTECTED_BRANCHES = ("main", "master")
GATE_MATERIALS = ("brief.md", "diff.patch", "verify.json", "manifest.json", "report.md")
TAIL_LINES = 40
CONTEXT_DIR = "context"  # S9: top-level work-dir directory holding vault notes copied in at dispatch
CAPTURE_VIA = "vault-capture"

BLOCK_RE = re.compile(r"^```depot[ \t]*\n(.*?)\n```[ \t]*$", re.S | re.M)

BRIEF_SKELETON_HEADER = '```depot\n{"expected_files": [], "test_command": ""}\n```\n\n'

BRIEF_SKELETON = """```depot
{"expected_files": [], "test_command": ""}
```

TASK: (describe the work here)

You are working in a plain directory (not a git repository you should touch): do not run any git commands. Committing is host-side.
"""


class DepotError(Exception):
    pass


class DispatchBlocked(DepotError):
    """A dispatch refused for a reason recorded on the item as a `blocked` event."""


def yard_root():
    return Path(os.environ.get("DEPOT_YARD") or Path(__file__).resolve().parent.parent)


def iso(dt=None):
    dt = dt or datetime.now(timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_json(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2) + "\n")


# ---- items and events ----

class Item:
    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.name
        self.id = self.name.split("-", 1)[0]

    @property
    def waybill_path(self):
        return self.path / "waybill.json"

    def waybill(self):
        return json.loads(self.waybill_path.read_text())

    def save_waybill(self, wb):
        write_json(self.waybill_path, wb)

    def events(self):
        p = self.path / "events.jsonl"
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]

    def emit(self, type_, actor, data=None):
        ev = {"ts": iso(), "item": self.name, "type": type_, "actor": actor, "data": data or {}}
        line = json.dumps(ev) + "\n"
        glog = yard_root() / "log" / "events.jsonl"
        glog.parent.mkdir(parents=True, exist_ok=True)
        for p in (self.path / "events.jsonl", glog):
            with open(p, "a") as f:
                f.write(line)
        return ev

    def derived_status(self):
        status = "queued"
        for e in self.events():
            t = e["type"]
            if t == "item_created":
                status = "queued"
            elif t == "brief_approved":
                status = "approved"
            elif t == "dispatched":
                status = "dispatched"
            elif t == "run_interrupted":
                status = "interrupted"
            elif t == "interrupted_requeued":
                status = "queued"
            elif t == "run_finished":
                status = "returned"
            elif t == "input_required":
                status = "blocked"
            elif t == "verified":
                status = "verified" if e["data"].get("result") == "pass" else "blocked"
            elif t in ("adjudicated", "recovered"):
                status = "review"
            elif t == "push_failed":
                status = "blocked"
            elif t == "reviewed":
                status = "reviewed" if e["data"].get("result") == "pass" else "blocked"
            elif t == "gate_result" and e["data"].get("result") != "pass":
                status = "blocked"
            elif t == "reopened":
                status = "queued"  # re-queued, not re-approved: only a later brief_approved makes it dispatchable
            elif t == "blocked":
                status = "blocked"
            # `message` is deliberately absent: a message is never a state change (spec §9).
        return status

    def sync_status(self):
        """Mirror the derived status into the waybill; the file is only rewritten when it changes."""
        wb = self.waybill()
        status = self.derived_status()
        if wb.get("status") != status:
            wb["status"] = status
            self.save_waybill(wb)


def all_items():
    d = yard_root() / "items"
    if not d.is_dir():
        return []
    return [Item(p) for p in sorted(d.iterdir()) if p.is_dir() and (p / "waybill.json").exists()]


def find_item(ref):
    ref = str(ref)
    for it in all_items():
        if it.name == ref or it.id == ref.zfill(4):
            return it
    raise DepotError(f"no such item: {ref}")


# ---- brief header ----

def parse_header(text):
    m = BLOCK_RE.search(text)
    if not m:
        raise DepotError("brief.md has no ```depot block")
    try:
        hdr = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise DepotError(f"brief.md ```depot block is not valid JSON: {e}")
    if not isinstance(hdr, dict):
        raise DepotError("```depot block must be a JSON object")
    ef, tc = hdr.get("expected_files"), hdr.get("test_command")
    if not isinstance(ef, list) or not all(isinstance(x, str) for x in ef):
        raise DepotError("```depot block: expected_files must be a list of strings")
    if not isinstance(tc, str):
        raise DepotError("```depot block: test_command must be a string")
    return hdr


def strip_header(text):
    return BLOCK_RE.sub("", text, count=1).lstrip("\n")


def task_contract(brief_text, wb):
    """The exact brief and its effective verification/destination inputs."""
    hdr = parse_header(brief_text)
    return {"brief": brief_text, "expected_files": hdr["expected_files"],
            "test_command": hdr["test_command"], "repo": wb.get("repo"), "branch": wb.get("branch")}


def contract_fingerprint(contract):
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def approval_data(item):
    contract = task_contract((item.path / "brief.md").read_text(), item.waybill())
    return {"contract_fingerprint": contract_fingerprint(contract)}


def sync_waybill_from_brief(item):
    """The brief's depot block is the source of expected_files and test_command."""
    hdr = parse_header((item.path / "brief.md").read_text())
    wb = item.waybill()
    wb["expected_files"] = hdr["expected_files"]
    wb["test_command"] = hdr["test_command"]
    item.save_waybill(wb)
    return wb


# ---- track budgets (S5): tracks.json, stamped into the waybill at filing ----

def load_tracks():
    p = yard_root() / "tracks.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except ValueError as e:
        raise DepotError(f"tracks.json is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise DepotError("tracks.json must be a JSON object")
    for name, entry in data.items():
        if not isinstance(entry, dict):
            raise DepotError(f"tracks.json: entry {name} must be an object")
        for k in ("timeout_s", "quiet_after_s"):
            if k in entry and (isinstance(entry[k], bool) or not isinstance(entry[k], (int, float)) or entry[k] <= 0):
                raise DepotError(f"tracks.json: {name}.{k} must be a positive number")
    return data


def resolve_budgets(track, explicit_timeout=None, explicit_quiet=None):
    """Per field: explicit value, else the track's tracks.json entry, else its `_default`, else built-in."""
    tracks = load_tracks()
    out = {}
    for key, explicit, builtin in (("timeout_s", explicit_timeout, DEFAULT_TIMEOUT_S),
                                   ("quiet_after_s", explicit_quiet, DEFAULT_QUIET_AFTER_S)):
        value = explicit
        for entry in (tracks.get(track), tracks.get("_default")):
            if value is None and entry and key in entry:
                value = entry[key]
        value = value if value is not None else builtin
        out[key] = int(value) if float(value).is_integer() else value
    return out


# ---- commands: new-item, approve, status, log ----

def next_item_id():
    """The next sequential item id. Every creator (new-item, vault capture, floor intake) allocates through here,
    each reading the yard as the previous creator left it: they all run in the daemon's serialized cycle."""
    ids = [int(it.id) for it in all_items() if it.id.isdigit()]
    return f"{max(ids, default=0) + 1:04d}"


def cmd_new_item(args):
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", args.slug):
        raise DepotError("slug must be lowercase letters, digits and hyphens")
    items_dir = yard_root() / "items"
    items_dir.mkdir(parents=True, exist_ok=True)
    new_id = next_item_id()
    path = items_dir / f"{new_id}-{args.slug}"
    path.mkdir()
    item = Item(path)
    (path / "brief.md").write_text(BRIEF_SKELETON)
    try:
        budgets = resolve_budgets(args.track, args.timeout_s, args.quiet_after_s)
    except DepotError:
        shutil.rmtree(path)
        raise
    wb = {
        "id": new_id,
        "slug": args.slug,
        "title": args.title,
        "track": args.track,
        "created": iso(),
        "brief": str((path / "brief.md").relative_to(yard_root())),
        "adapter": "codex",
        "model": None,
        "timeout_s": budgets["timeout_s"],
        "quiet_after_s": budgets["quiet_after_s"],
        "expected_files": [],
        "test_command": "",
        "status": "queued",
    }
    # S2 fields are written only when asked for, so a plain item's waybill is exactly S1's.
    if bool(args.repo) != bool(args.branch):
        shutil.rmtree(path)
        raise DepotError("--repo and --branch go together")
    if args.repo:
        if args.branch in PROTECTED_BRANCHES:
            shutil.rmtree(path)
            raise DepotError(f"--branch must be a review branch, not {args.branch}")
        wb["repo"], wb["branch"] = args.repo, args.branch
    if args.kind == "gate":
        if not args.target:
            shutil.rmtree(path)
            raise DepotError("a gate item needs --target")
        try:
            wb["target"] = find_item(args.target).name
        except DepotError:
            shutil.rmtree(path)
            raise
        wb["kind"] = "gate"
        wb["adapter"] = "claude"
        if args.material:
            wb["materials"] = args.material
    elif args.target or args.material:
        shutil.rmtree(path)
        raise DepotError("--target and --material only apply to --kind gate")
    item.save_waybill(wb)
    item.emit("item_created", "operator", {"title": args.title, "track": args.track})
    print(item.name)
    return 0


def cmd_approve(args):
    item = find_item(args.item)
    if item.derived_status() == "interrupted":
        raise DepotError("interrupted run: use the floor recovery action before approving")
    parse_header((item.path / "brief.md").read_text())  # raises if missing/invalid
    missing = capture_gate_missing(item)
    if missing:
        raise DepotError(f"cannot approve {item.name}: a vault capture still needs shaping: " + "; ".join(missing))
    sync_waybill_from_brief(item)
    item.emit("brief_approved", "operator", approval_data(item))
    item.sync_status()
    print(f"approved {item.name}")
    return 0


def cmd_status(args):
    items = all_items()
    if not items:
        print("no items")
    for it in items:
        ev = it.events()
        last = f"{ev[-1]['type']} @ {ev[-1]['ts']}" if ev else "-"
        print(f"{it.name}  stage={it.derived_status()}  last={last}")
    return 0


def cmd_log(args):
    for e in find_item(args.item).events():
        print(f"{e['ts']}  {e['type']}  {e['actor']}  {json.dumps(e['data'])}")
    return 0


# ---- git helpers ----

def git(cwd, *args, env=None, ident=False, check=True, text=True):
    cmd = ["git"]
    if ident:
        name, email = git_identity()
        if name:
            cmd += ["-c", f"user.name={name}"]
        if email:
            cmd += ["-c", f"user.email={email}"]
    cmd += list(args)
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=text)
    if check and r.returncode != 0:
        raise DepotError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def git_identity():
    """The yard repo's identity (falls back to the global one); applied to nested-repo commits."""
    out = []
    for key in ("user.name", "user.email"):
        r = subprocess.run(["git", "-C", str(yard_root()), "config", key], capture_output=True, text=True)
        out.append(r.stdout.strip() or None)
    return out[0], out[1]


def changed_files(work):
    r = git(work, "status", "--porcelain", "-z", "-uall")
    parts = r.stdout.split("\0")
    files, i = set(), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, path = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            i += 1  # skip the rename/copy source
        if path == CONTEXT_DIR or path.startswith(CONTEXT_DIR + "/"):
            continue  # S9: vault notes copied in for the agent are reference material, never deliverables
        files.add(path)
    return files


def starting_point(work):
    head = git(work, "rev-parse", "--verify", "-q", "HEAD", check=False)
    ref = git(work, "symbolic-ref", "-q", "HEAD", check=False)
    return (head.stdout.strip() if head.returncode == 0 else None,
            ref.stdout.strip() if ref.returncode == 0 else "HEAD")


def candidate_tree(work, base):
    """Capture deliverables independently of the agent's/live Git index."""
    with tempfile.TemporaryDirectory() as td:
        env = {**os.environ, "GIT_INDEX_FILE": os.path.join(td, "index")}
        git(work, "read-tree", base, env=env)
        paths = git(work, "ls-files", "--cached", "--others", "--exclude-standard", "-z",
                    env=env).stdout.split("\0")
        paths = sorted({p for p in paths if p and p != CONTEXT_DIR
                        and not p.startswith(CONTEXT_DIR + "/")})
        if paths:
            git(work, "--literal-pathspecs", "add", "-A", "--", *paths, env=env)
        return git(work, "write-tree", env=env).stdout.strip()


def tree_changes(work, base, tree):
    return set(filter(None, git(work, "diff", "--name-only", "--no-renames", "-z",
                                base, tree).stdout.split("\0")))


def write_tree_diff(work, out, base, tree):
    # Keep bytes: text-mode newline conversion would corrupt a CRLF patch.
    diff = git(work, "diff", "--binary", "--full-index", "--no-renames", "--no-ext-diff",
               "--no-textconv", base, tree, text=False).stdout
    Path(out).write_bytes(diff)


# ---- adapters ----

class Adapter:
    """An adapter supplies the command line and the event-stream parser for one agent CLI."""
    name = None
    sandbox = None
    env_var = None
    default_bin = None

    def binary(self):
        return os.environ.get(self.env_var) or self.default_bin

    def command(self, wb, work, run_dir):
        raise NotImplementedError

    def parse(self, ev, state):
        """Fold one decoded stream event into state: thread (session id), usage, final (report text)."""
        raise NotImplementedError


class CodexAdapter(Adapter):
    name = "codex"
    sandbox = "workspace-write"
    env_var = "DEPOT_CODEX"
    default_bin = "codex"

    def command(self, wb, work, run_dir):
        cmd = [self.binary(), "exec", "-s", self.sandbox, "--json", "-C", str(work),
               "-o", str(run_dir / "report.raw"), "--skip-git-repo-check"]
        if wb.get("model"):
            cmd += ["-m", wb["model"]]
        return cmd + ["-"]

    def parse(self, ev, state):
        if ev.get("type") == "thread.started":
            state["thread"] = ev.get("thread_id")
        if "usage" in ev:
            state["usage"] = ev["usage"]


class ClaudeAdapter(Adapter):
    """No sandbox flag exists, so the posture is a prompt gate: the local work tools are pre-approved with
    --allowedTools (Bash among them, tool-level, so a build can run its commands), and --permission-prompts none
    stays on so that anything NOT on the list (web tools, MCP tools, anything future) is denied without a prompt.
    It is a permission posture, not a filesystem sandbox: the process runs as the user and only the gate constrains it.

    --verbose is required by the CLI whenever stream-json is used with -p (checked against 2.1.316).
    --allowedTools composes with --permission-prompts none on 2.1.316 (probed live: listed tools run, WebFetch denied)."""
    name = "claude"
    sandbox = "allowed-tools+prompts-none"
    allowed_tools = ("Bash", "Read", "Write", "Edit", "Grep", "Glob")
    env_var = "DEPOT_CLAUDE"
    default_bin = "claude"

    def command(self, wb, work, run_dir):
        cmd = [self.binary(), "-p", "--output-format", "stream-json", "--verbose", "--permission-prompts", "none",
               "--allowedTools", ",".join(self.allowed_tools)]
        if wb.get("model"):
            cmd += ["--model", wb["model"]]  # after the grant: --allowedTools is variadic and ends at the next flag
        return cmd  # the brief goes on stdin; the process runs in the work dir

    def parse(self, ev, state):
        if ev.get("session_id") and not state["thread"]:
            state["thread"] = ev["session_id"]
        if ev.get("type") == "result":
            if ev.get("session_id"):
                state["thread"] = ev["session_id"]
            if "usage" in ev:
                state["usage"] = ev["usage"]
            if isinstance(ev.get("result"), str):
                state["final"] = ev["result"]


ADAPTERS = {a.name: a for a in (CodexAdapter(), ClaudeAdapter())}


def get_adapter(name):
    if name not in ADAPTERS:
        raise DepotError(f"unknown adapter: {name} (known: {', '.join(sorted(ADAPTERS))})")
    return ADAPTERS[name]


# ---- adapter run ----

def kill_tree(proc):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def run_adapter(adapter, cmd, stdin_text, run_dir, item, timeout_s, cwd):
    heartbeat_s = float(os.environ.get("DEPOT_HEARTBEAT_S", "30"))
    state = {"last": time.monotonic(), "thread": None, "usage": None, "final": None}
    t0 = time.monotonic()
    started = iso()
    events_f = open(run_dir / "events.jsonl", "w")
    err_f = open(run_dir / "stderr.log", "w")
    timed_out = False
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err_f, cwd=cwd,
                                text=True, encoding="utf-8", errors="replace", start_new_session=True)
    except OSError as e:
        err_f.write(f"cannot start adapter: {e}\n")
        events_f.close()
        err_f.close()
        return {"started": started, "elapsed_s": round(time.monotonic() - t0, 3), "exit_code": 127,
                "timed_out": False, "thread": None, "usage": None, "final": None}

    def feed():
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    def read():
        for line in proc.stdout:
            events_f.write(line)
            events_f.flush()
            state["last"] = time.monotonic()
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            adapter.parse(ev, state)

    threads = [threading.Thread(target=feed, daemon=True), threading.Thread(target=read, daemon=True)]
    for t in threads:
        t.start()
    deadline = t0 + timeout_s
    next_hb = t0 + heartbeat_s
    while True:
        try:
            proc.wait(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        if now >= deadline:
            timed_out = True
            kill_tree(proc)
            break
        if now >= next_hb:
            item.emit("heartbeat", "turntable",
                      {"since_last_adapter_event_s": round(now - state["last"], 1),
                       "elapsed_s": round(now - t0, 1)})
            next_hb += heartbeat_s
    for t in threads:
        t.join(timeout=5)
    events_f.close()
    err_f.close()
    code = 124 if timed_out else proc.returncode
    return {"started": started, "elapsed_s": round(time.monotonic() - t0, 3), "exit_code": code,
            "timed_out": timed_out, "thread": state["thread"], "usage": state["usage"], "final": state["final"]}


# ---- verification-lite and host-side commit ----

def tail(text, n=TAIL_LINES):
    return "\n".join(text.splitlines()[-n:])


def verification_filter(output):
    """Export a native-architecture seccomp filter; other ABIs fail closed.

    No sockets (including Unix sockets), nor io_uring's alternate network API.
    libseccomp supplies syscall numbers and architecture checks, not generated code.
    """
    lib = ctypes.CDLL("libseccomp.so.2")
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(0x7fff0000)  # SCMP_ACT_ALLOW; default bad-arch action is KILL
    if not ctx:
        raise OSError("cannot allocate verification seccomp filter")
    try:
        for name in (b"socket", b"socketpair", b"connect", b"socketcall", b"io_uring_setup"):
            number = lib.seccomp_syscall_resolve_name(name)
            if number == -1:
                raise OSError(f"unknown verification syscall: {name!r}")
            if lib.seccomp_rule_add(ctx, 0x00050000 | errno.EPERM, number, 0) != 0:
                raise OSError(f"cannot deny verification syscall: {name!r}")
        if lib.seccomp_export_bpf(ctx, output.fileno()) != 0:
            raise OSError("cannot export verification seccomp filter")
        output.seek(0)
    finally:
        lib.seccomp_release(ctx)


def verification_command(work, argv, private, filter_fd):
    """Construct an independent, fail-closed Linux verification sandbox."""
    work = Path(work).resolve(strict=True)
    # Fixed host binary: never resolve the sandbox launcher through daemon PATH.
    cmd = ["/usr/bin/bwrap", "--unshare-all", "--unshare-user", "--share-net", "--disable-userns",
           "--die-with-parent", "--new-session", "--cap-drop", "ALL",
           "--clearenv", "--proc", "/proc", "--dev", "/dev"]
    # Only system runtimes and the work tree are visible, never /home, /etc,
    # host /tmp, the yard, SSH agents, daemon processes, or publication records.
    for name in ("usr", "bin", "lib", "lib64"):
        src = Path("/") / name
        if src.is_symlink():
            cmd += ["--symlink", os.readlink(src), str(src)]
        elif src.exists():
            cmd += ["--ro-bind", str(src), str(src)]
    cmd += ["--bind", str(work), "/work"]
    hidden = {".git", ".ssh", ".aws", ".codex", ".agents", ".config",
              ".netrc", ".git-credentials", ".npmrc", ".pypirc"}
    empty_dir = Path(private) / "empty"
    empty_dir.mkdir()
    empty_file = Path(private) / "empty-file"
    empty_file.touch()
    def walk_error(error):
        raise error  # Never silently omit credential masks in an unreadable subtree.

    for parent, dirs, files in os.walk(work, followlinks=False, onerror=walk_error):
        for name in hidden.intersection(dirs + files):
            path = Path(parent) / name
            if path.is_symlink():
                raise OSError(f"verification refuses credential/repository symlink: {path}")
            source = empty_dir if path.is_dir() else empty_file
            cmd += ["--ro-bind", str(source), "/work/" + path.relative_to(work).as_posix()]
        dirs[:] = [name for name in dirs if name not in hidden]
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "TMPDIR": "/work",
           "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    for key, value in env.items():
        cmd += ["--setenv", key, value]
    # share-net avoids requiring network namespace administration. The mandatory
    # seccomp filter denies all socket access instead, including local IPC.
    return cmd + ["--seccomp", str(filter_fd), "--remount-ro", "/",
                  "--chdir", "/work", "--", *argv]


def run_tests(work, test_command):
    if not test_command.strip():
        return {"command": test_command, "exit_code": None, "tail": "", "note": "no test_command"}
    try:
        with tempfile.TemporaryDirectory(prefix="depot-verify-") as private, tempfile.TemporaryFile() as policy:
            verification_filter(policy)
            cmd = verification_command(work, shlex.split(test_command), private, policy.fileno())
            r = subprocess.run(cmd, cwd="/", capture_output=True, text=True,
                               timeout=TEST_TIMEOUT_S, env={}, stdin=subprocess.DEVNULL,
                               pass_fds=(policy.fileno(),))
        return {"command": test_command, "exit_code": r.returncode, "tail": tail(r.stdout + r.stderr)}
    except subprocess.TimeoutExpired:
        return {"command": test_command, "exit_code": 124, "tail": f"timed out after {TEST_TIMEOUT_S}s"}
    except (OSError, ValueError) as e:
        return {"command": test_command, "exit_code": 127, "tail": f"verification sandbox: {e}"}


def host_commit(item, work, base_commit, ref, tree):
    """Publish only the recorded tree, with a compare-and-swap on the pinned ref."""
    base = base_commit or git(work, "hash-object", "-w", "-t", "tree", os.devnull).stdout.strip()
    if candidate_tree(work, base) != tree or starting_point(work) != (base_commit, ref):
        raise DepotError("workspace or starting ref changed before publication")
    statuses = git(work, "diff", "--name-status", "--no-renames", base, tree).stdout.splitlines()
    verbs = [f"{'add' if status == 'A' else 'update'} {path}"
             for status, path in (line.split("\t", 1) for line in statuses)]
    stat = git(work, "diff", "--stat", base, tree).stdout.strip()
    msg = f"{item.name}: {', '.join(verbs)}\n\n{stat}\n"
    parents = ["-p", base_commit] if base_commit else []
    h = git(work, "commit-tree", tree, *parents, "-m", msg, ident=True).stdout.strip()
    git(work, "update-ref", ref, h, base_commit or "0" * len(tree))
    git(work, "read-tree", tree)  # synchronize the live index; it never supplies commit contents
    return h


def verify_and_commit(item, work, run_dir, wb, start=None):
    """Test a pinned candidate; any post-test drift fails, never silently re-baselines."""
    expected = list(wb["expected_files"])
    base_commit, ref = start if start is not None else starting_point(work)
    base = base_commit or git(work, "hash-object", "-w", "-t", "tree", os.devnull).stdout.strip()
    candidate = candidate_tree(work, base)
    tests = run_tests(work, wb["test_command"])
    tree = candidate_tree(work, base)
    changed = tree_changes(work, base, tree)
    discrepancies = [{"path": p, "kind": "extra"} for p in sorted(changed - set(expected))]
    discrepancies += [{"path": p, "kind": "missing"} for p in sorted(set(expected) - changed)]
    write_tree_diff(work, run_dir / "diff.patch", base, tree)
    tests_failed = tests["exit_code"] is not None and tests["exit_code"] != 0
    if tree != candidate or starting_point(work) != (base_commit, ref):
        result = "workspace_changed"
    elif discrepancies:
        result = "discrepancies"
    elif tests_failed:
        result = "tests_failed"
    else:
        result = "pass"
    verify = {"ts": iso(), "result": result, "expected_files": expected, "changed_files": sorted(changed),
              "discrepancies": discrepancies, "test": tests, "commit": None,
              "base_commit": base_commit, "candidate_tree": candidate, "tree": tree}
    if result == "pass" and expected:
        try:
            verify["commit"] = host_commit(item, work, base_commit, ref, tree)
        except DepotError as e:
            verify["result"] = "commit_failed"
            verify["error"] = str(e)
    write_json(run_dir / "verify.json", verify)
    item.emit("verified", "turntable", {"result": verify["result"], "discrepancies": discrepancies,
                                         "test_exit_code": tests["exit_code"]})
    push_ok = None
    if verify["commit"]:
        h = verify["commit"]
        item.emit("committed", "turntable", {"hash": h, "files": sorted(changed)})
        if wb.get("repo"):
            push_ok = push_branch(item, work, wb, h)
    return verify, push_ok


# ---- repo-targeted items: clone, review branch, push ----

def prepare_repo_work(wb, work, fresh_base=False):
    repo, branch = wb["repo"], wb.get("branch")
    if not branch:
        raise DepotError("waybill has a repo but no branch")
    if branch in PROTECTED_BRANCHES:
        raise DepotError(f"refusing to use protected branch {branch} as a review branch")
    if not (work / ".git").exists():
        if work.exists() and any(work.iterdir()):
            raise DepotError(f"{work} exists, is not empty, and is not a clone")
        git(work.parent, "clone", "-q", repo, work.name)
        if not fresh_base and git(work, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).returncode == 0:
            git(work, "checkout", "-q", branch)
        elif not fresh_base and git(work, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}", check=False).returncode == 0:
            git(work, "checkout", "-q", "-b", branch, "--track", f"origin/{branch}")
        else:
            base = git(work, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", check=False)
            if base.returncode == 0 and base.stdout.strip():
                git(work, "checkout", "-q", "-b", branch, base.stdout.strip())
            else:  # empty remote: the branch starts unborn
                git(work, "checkout", "-q", "-b", branch)
        return
    git(work, "fetch", "-q", "--prune", "origin")
    restart_branch(work, branch)


def restart_branch(work, branch):
    """Re-dispatch: the branch starts from the remote's current head, not from a prior run's clone state.

    The work tree is reset to the target and untracked non-ignored files are cleaned, so a prior run's
    uncommitted changes are gone (ignored files such as a .venv may stay). The target is origin/<branch>
    when the remote has it, else the origin default branch. A local branch's commits are never discarded:
    if its tip isn't reachable from the target, the dispatch refuses by name and destroys nothing.
    """
    local = git(work, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).returncode == 0
    if git(work, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}", check=False).returncode == 0:
        target = f"origin/{branch}"
    else:
        base = git(work, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", check=False)
        target = base.stdout.strip() if base.returncode == 0 and base.stdout.strip() else None
    if target is None:  # empty remote: nothing to restart from, so the branch is left as it is
        if local:
            git(work, "checkout", "-q", branch)
        else:
            git(work, "checkout", "-q", "-b", branch)
        return
    if local and git(work, "merge-base", "--is-ancestor", f"refs/heads/{branch}", target,
                     check=False).returncode != 0:
        raise DepotError(f"local branch {branch} holds commits that are not on origin; "
                         f"refusing to re-dispatch over them (nothing was discarded). "
                         f"Push or move those commits, then dispatch again.")
    git(work, "checkout", "-q", "-f", "-B", branch, target)
    git(work, "clean", "-q", "-f", "-d")


def push_branch(item, work, wb, commit):
    """Push the waybill's branch only, never forced, and record what the remote reports back."""
    branch = wb["branch"]
    ref = f"refs/heads/{branch}"
    r = git(work, "push", "origin", f"{commit}:{ref}", check=False)
    if r.returncode != 0:
        item.emit("push_failed", "turntable", {"branch": branch, "error": (r.stderr or r.stdout).strip()})
        return False
    ls = git(work, "ls-remote", "origin", ref, check=False)
    fields = ls.stdout.split() if ls.returncode == 0 else []
    remote = fields[0] if fields else None
    if remote != commit:
        item.emit("push_failed", "turntable", {
            "branch": branch, "error": f"read-back mismatch: remote head {remote}, expected {commit}"})
        return False
    item.emit("pushed", "turntable", {"repo": wb["repo"], "branch": branch, "remote_head": remote,
                                       "commit": commit})
    return True


# ---- gate items ----

GATE_CRITERION_RE = re.compile(r"^CRITERION (\d+): (PASS|FAIL) (?:—|–|--?) (\S.*)$")
GATE_VERDICT_RE = re.compile(r"^GATE: (PASS|FAIL)$")


def parse_gate_report(text):
    """Strict: one GATE verdict line, last, preceded by well-formed CRITERION lines that agree with it."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    verdicts = [ln for ln in lines if ln.startswith("GATE:")]
    if len(verdicts) != 1 or lines[-1] != verdicts[0]:
        return None
    m = GATE_VERDICT_RE.match(verdicts[0])
    crit = [ln for ln in lines if ln.startswith("CRITERION")]
    parsed = [GATE_CRITERION_RE.match(ln) for ln in crit]
    if not m or not crit or not all(parsed):
        return None
    all_pass = all(p.group(2) == "PASS" for p in parsed)
    if (m.group(1) == "PASS") != all_pass:
        return None
    return {"result": m.group(1).lower(), "criteria": crit}


def latest_run_dir(target):
    runs = sorted(p for p in (target.path / "runs").glob("*") if p.is_dir()) if (target.path / "runs").is_dir() else []
    if not runs:
        raise DepotError(f"target {target.name} has no runs to gate")
    return runs[-1]


def gate_materials(wb):
    """(destination name, source path) pairs: the target's brief and its latest run's records.

    A waybill `materials` entry adds one more file: `item:<name>` (from the target's item dir) or
    `run:<name>` (from its latest run dir); it is copied under its base name.
    """
    target = find_item(wb["target"])
    run = latest_run_dir(target)
    pairs = []
    for name in GATE_MATERIALS:
        src = (target.path if name == "brief.md" else run) / name
        if not src.exists():
            if name == "diff.patch":  # written only when the run produced a diff
                continue
            raise DepotError(f"gate material missing: {src}")
        pairs.append((name, src))
    for spec in wb.get("materials", []):
        where, _, name = spec.partition(":")
        if where not in ("item", "run") or not name or "/" in name:
            raise DepotError(f"bad materials entry {spec!r}: use item:<name> or run:<name>")
        src = (target.path if where == "item" else run) / name
        if not src.exists():
            raise DepotError(f"gate material missing: {src}")
        if any(name == d for d, _ in pairs):
            raise DepotError(f"gate material {name} collides with another material")
        pairs.append((name, src))
    return pairs


def prepare_gate_work(wb, work):
    pairs = gate_materials(wb)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    for name, src in pairs:
        shutil.copyfile(src, work / name)
    return [n for n, _ in pairs]


# ---- conversation (S4): what a dispatch owes the messages sent since the last one ----

def newest_event(events):
    """The newest event that is not a heartbeat (heartbeats are liveness stamps, not trail)."""
    for e in reversed(events):
        if e["type"] != "heartbeat":
            return e
    return None


def input_required_run_id(events, idx):
    """The run an input_required event belongs to: its own run_id, else the dispatched event before it."""
    rid = events[idx]["data"].get("run_id")
    if rid:
        return rid
    for e in reversed(events[:idx]):
        if e["type"] == "dispatched":
            return e["data"].get("run_id")
    return None


def delivery_plan(events):
    """Messages owed to the next dispatch: every message newer than the most recent `dispatched` event.

    The run's materials (brief.dispatched.md) are the delivery record, so a message is delivered once:
    the next dispatch starts from a newer `dispatched` line. If the last run ended in input_required and
    a message answers it, that message goes in the addendum with the question; the rest go in order.
    `problem` is set (and no answer returned) when the addendum cannot be assembled honestly.
    """
    last = max((i for i, e in enumerate(events) if e["type"] == "dispatched"), default=-1)
    tail = events[last + 1:]
    # A reopened event carries operator's message too (spec §9): it rides the next dispatch like any message.
    messages = [e for e in tail if e["type"] == "message" or (e["type"] == "reopened" and e["data"].get("text"))]
    answer = problem = None
    asked = [last + 1 + i for i, e in enumerate(tail) if e["type"] == "input_required"]
    if asked:
        idx = asked[-1]
        rid = input_required_run_id(events, idx)
        answers = [m for m in messages if rid and m["type"] == "message"
                   and m["data"].get("answers_input_required") == rid]
        if answers:
            question = events[idx]["data"].get("question")
            if not isinstance(question, str) or not question.strip():
                problem = f"input_required of run {rid} carries no question text; refusing to invent one"
            else:
                answer = {"run_id": rid, "question": question, "message": answers[0]}
    others = [m for m in messages if not (answer and m is answer["message"])]
    return {"answer": answer, "others": others, "problem": problem}


def assemble_brief(brief_text, plan, attachments=(), context=()):
    """The text sent to the agent: the brief as before, plus the addendum, messages and attachments when owed."""
    base = strip_header(brief_text)
    if not plan["answer"] and not plan["others"] and not attachments and not context:
        return base
    out = base.rstrip("\n") + "\n"
    if plan["answer"]:
        a = plan["answer"]
        out += (f"\n## Addendum — input-required answer\n\nQuestion (from run {a['run_id']}):\n\n"
                f"{a['question']}\n\noperator's answer ({a['message']['ts']}):\n\n{a['message']['data'].get('text', '')}\n")
    if plan["others"]:
        out += "\n## Messages from operator\n"
        for m in plan["others"]:
            label = " (reopened)" if m["type"] == "reopened" else ""
            out += f"\n### {m['ts']}{label}\n\n{m['data'].get('text', '')}\n"
    if attachments:
        out += (f"\n## Attachments from operator\n\nThese files are in `./{ATTACH_DIR}/` in your working directory. They are "
                "inputs, not part of the deliverable: do not move them, copy them into the deliverable, or list them "
                "among the files you changed.\n\n")
        for a in attachments:
            out += f"- `{ATTACH_DIR}/{a['file']}` ({a.get('bytes', '?')} bytes, sha256 {a.get('sha256', '?')})\n"
    if context:
        out += ("\n## Context from vault\n\nThese notes were copied from operator's vault into `./context/` in your working "
                "directory as reference material. They are not part of the deliverable: do not list them among the "
                "files you changed.\n\n")
        for c in context:
            out += f"- {c['path']} → context/{c['base']}\n"
    return out


# ---- attachments (S8): files operator attaches from the floor ride the next dispatch into the workspace ----

ATTACH_DIR = "depot-attachments"


def active_attachments(events):
    """The attached-and-not-removed files, oldest first: each is the `attached` event's data."""
    cur = {}
    for e in events:
        if e["type"] == "attached":
            cur[e["data"].get("file")] = e["data"]
        elif e["type"] == "attachment_removed":
            cur.pop(e["data"].get("file"), None)
    return list(cur.values())


def deliver_attachments(item, work, files):
    """Copy the active attachments into <work>/depot-attachments/ (replacing last run's copy) and keep that
    directory out of git via the work repo's own info/exclude, so changed-file verification, the diff and the
    host-side commit all stay exact: a delivered input is never an `extra` file."""
    dest = work / ATTACH_DIR
    if dest.exists():
        shutil.rmtree(dest)
    if not files:
        return
    dest.mkdir(parents=True)
    for a in files:
        name = a.get("file")
        src = item.path / "files" / str(name)
        if not name or "/" in name or not src.is_file():
            raise DepotError(f"attachment {name!r} is recorded but missing from {item.path / 'files'}")
        if hashlib.sha256(src.read_bytes()).hexdigest() != a.get("sha256"):
            raise DepotError(f"attachment {name} does not match its recorded sha256; refusing to deliver it")
        shutil.copyfile(src, dest / name)
    exclude = work / ".git" / "info" / "exclude"
    if exclude.parent.is_dir():
        line = f"/{ATTACH_DIR}/"
        have = exclude.read_text().splitlines() if exclude.exists() else []
        if line not in have:
            with open(exclude, "a") as f:
                f.write(("\n" if have and not exclude.read_text().endswith("\n") else "") + line + "\n")


# ---- dispatch ----

def active_park(events):
    """A park holds until a later unpark in file order; other events cannot release it."""
    for event in reversed(events):
        if event["type"] in ("parked", "unparked"):
            return event["type"] == "parked"
    return False


def finish_manifest(run_dir, manifest):
    manifest["artifacts"] = sorted(p.name for p in run_dir.iterdir() if p.name != "manifest.json") + ["manifest.json"]
    write_json(run_dir / "manifest.json", manifest)


def dispatch_item(item):
    """Dispatch one approved item. Returns (exit_code, outcome); raises DepotError on refusal."""
    # Lock the existing item log, not a new record file. All dispatch callers share this boundary.
    with open(item.path / "events.jsonl", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DepotError(f"refusing to dispatch {item.name}: another dispatch is active")
        return _dispatch_item(item)


def _dispatch_item(item):
    events = item.events()
    if item.derived_status() == "interrupted":
        raise DepotError("interrupted run: use the floor recovery action before dispatching")
    if active_park(events):
        raise DepotError(f"refusing to dispatch {item.name}: item is parked; unpark it first")
    reopened = max((i for i, e in enumerate(events) if e["type"] in ("reopened", "interrupted_requeued")), default=-1)
    approved = max((i for i, e in enumerate(events) if e["type"] == "brief_approved" and i > reopened), default=-1)
    if approved < 0:
        raise DepotError(f"refusing to dispatch {item.name}: no brief_approved event"
                         + (" since it was reopened" if reopened >= 0 else ""))
    brief_text = (item.path / "brief.md").read_text()
    if any(e["type"] == "dispatched" for e in events[approved + 1:]):
        raise DepotError(f"refusing to dispatch {item.name}: approval already consumed; approve again")
    wb = item.waybill()
    missing = capture_gate_missing(item)
    if missing:
        raise DepotError(f"refusing to dispatch {item.name}: a vault capture still needs shaping: " + "; ".join(missing))
    fingerprint = contract_fingerprint(task_contract(brief_text, wb))
    if events[approved]["data"].get("contract_fingerprint") != fingerprint:
        raise DepotError(f"refusing to dispatch {item.name}: task contract changed or approval has no fingerprint; approve again")
    plan = delivery_plan(events)
    if plan["problem"]:
        newest = newest_event(events)
        if not (newest and newest["type"] == "blocked" and newest["data"].get("reason") == plan["problem"]):
            item.emit("blocked", "turntable", {"reason": plan["problem"]})
        raise DispatchBlocked(f"refusing to dispatch {item.name}: {plan['problem']}")
    attachments = active_attachments(events)
    # Use the same snapshot that passed approval validation.
    hdr = parse_header(brief_text)
    wb.update(expected_files=hdr["expected_files"], test_command=hdr["test_command"])
    item.save_waybill(wb)
    context = resolve_context(wb)  # a missing/unsafe note refuses the dispatch before anything is created
    dispatch_text = assemble_brief(brief_text, plan, attachments, context)
    adapter = get_adapter(wb["adapter"])
    kind = wb.get("kind", "build")
    if kind not in ("build", "gate"):
        raise DepotError(f"unknown kind: {kind}")
    if kind == "gate" and not wb.get("target"):
        raise DepotError("gate item has no target")

    import floor
    recovery = floor.recovered_work(events)
    rerun_recovery = bool(kind == "build" and recovery and not recovery["superseded_by"])
    work = item.path / floor.work_directory(events)
    if rerun_recovery:
        # Keep the recovered repository intact. Each explicit approval gets a clean workspace.
        work = item.path / "work" / ".depot-reruns" / str(approved + 1)
        work.parent.mkdir(parents=True, exist_ok=True)
        if work.exists():
            raise DepotError("recovery re-run workspace already exists; inspect it and approve again")
    if not work.resolve().is_relative_to((item.path / "work").resolve()):
        raise DepotError("work directory escapes item workspace")
    if kind == "gate":
        prepare_gate_work(wb, work)
    elif wb.get("repo"):
        prepare_repo_work(wb, work, fresh_base=rerun_recovery)
    elif not (work / ".git").exists():
        work.mkdir(exist_ok=True)
        git(work, "init", "-q")

    start = starting_point(work) if kind == "build" else None
    deliver_attachments(item, work, attachments)
    deliver_context(item, work, context)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = item.path / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "brief.dispatched.md").write_text(dispatch_text, encoding="utf-8")  # the delivery record
    cmd = adapter.command(wb, work, run_dir)

    dispatched = {"adapter": adapter.name, "model": wb.get("model"), "sandbox": adapter.sandbox,
                  "timeout_s": wb["timeout_s"], "run_id": run_id,
                  "contract_fingerprint": fingerprint, "approval_seq": approved + 1}
    if work != item.path / "work":
        dispatched["work_directory"] = str(work.relative_to(item.path))
    if attachments:  # only when owed, so an item without attachments records exactly what it always did
        dispatched["attachments"] = [{"file": a["file"], "sha256": a.get("sha256")} for a in attachments]
    item.emit("dispatched", "turntable", dispatched)
    item.sync_status()
    res = run_adapter(adapter, cmd, dispatch_text, run_dir, item, wb["timeout_s"], work)

    manifest = {"run_id": run_id, "adapter": adapter.name, "model": wb.get("model"), "sandbox": adapter.sandbox,
                "started": res["started"], "elapsed_s": res["elapsed_s"], "exit_code": res["exit_code"],
                "timed_out": res["timed_out"], "session_or_thread_id": res["thread"], "usage": res["usage"],
                "context": [{"path": c["path"], "sha256": c["sha256"]} for c in context], "artifacts": []}
    raw = run_dir / "report.raw"
    if res["final"] is not None and not raw.exists():  # adapters that report in-stream, not to a file
        raw.write_text(res["final"])
    if raw.exists():
        shutil.copyfile(raw, run_dir / "report.md")
    else:
        (run_dir / "report.md").write_text("")
    write_json(run_dir / "manifest.json", manifest)
    item.emit("run_finished", "turntable", {"exit_code": res["exit_code"], "elapsed_s": res["elapsed_s"],
                                             "timed_out": res["timed_out"]})
    report_rel = str((run_dir / "report.md").relative_to(yard_root()))
    item.emit("claimed", adapter.name, {"report": report_rel})
    report = (run_dir / "report.md").read_text().strip()

    if kind == "gate":
        verdict = parse_gate_report(report)
        target = find_item(wb["target"])
        data = {"gate": item.name, "run_id": run_id, "report": report_rel}
        if verdict:
            data.update(result=verdict["result"], criteria=verdict["criteria"])
        else:
            data["result"] = "unparseable"
        target.emit("gate_result", adapter.name, data)
        target.sync_status()
        item.sync_status()
        finish_manifest(run_dir, manifest)
        print(f"{item.name}: run {run_id} exit={res['exit_code']} gate_result={data['result']} target={target.name}")
        return (0 if res["exit_code"] == 0 and verdict else 1), f"gate {data['result']}"

    if report.endswith("?"):
        item.emit("input_required", adapter.name, {"question": report, "run_id": run_id})
        item.sync_status()
        finish_manifest(run_dir, manifest)
        print(f"{item.name}: input required (see {report_rel})")
        return 3, "input_required"

    verify, push_ok = verify_and_commit(item, work, run_dir, wb, start=start)
    item.sync_status()
    finish_manifest(run_dir, manifest)
    line = f"{item.name}: run {run_id} exit={res['exit_code']} verify={verify['result']} commit={verify['commit']}"
    if push_ok is not None:
        line += f" push={'ok' if push_ok else 'failed'}"
    print(line)
    ok = res["exit_code"] == 0 and verify["result"] == "pass" and push_ok is not False
    outcome = verify["result"] if push_ok is not False else "push_failed"
    return (0 if ok else 1), outcome


def cmd_dispatch(args):
    return dispatch_item(find_item(args.item))[0]


# ---- message and reopen (S4): records-only commands ----

def records_guard():
    """Records-only commands run on a clean `main` yard clone, like the poll daemon (S2 Phase C).

    Refuses on a non-main checkout or a dirty working tree, then fast-forwards from origin so the
    append lands on the newest logs. Returns whether the yard is a git repository; a plain
    directory is appended to without the git steps, as the daemon polls one.
    """
    if not yard_is_git():
        return False
    root = yard_root()
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != "main":
        raise DepotError(f"the yard is on {branch!r}; records commands run on main only")
    dirty = git(root, "status", "--porcelain").stdout.rstrip()
    if dirty:
        raise DepotError("the yard has uncommitted changes; commit or clean them first:\n" + dirty)
    if yard_has_origin():
        r = git(root, "pull", "--ff-only", "-q", check=False)
        if r.returncode != 0:
            raise DepotError(f"git pull --ff-only failed: {(r.stderr or r.stdout).strip()}")
    return True


def records_commit(item, message):
    """Commit the item's log and the global log, then push main (never forced).

    Returns (commit, push_error); push_error is None when pushed or when there is no origin.
    A push failure leaves the local commit in place.
    """
    root = yard_root()
    paths = [str((item.path / "events.jsonl").relative_to(root)), "log/events.jsonl"]
    git(root, "add", "--", *paths)
    git(root, "commit", "-q", "-m", message, "--", *paths)
    commit = git(root, "rev-parse", "HEAD").stdout.strip()
    if not yard_has_origin():
        return commit, None
    r = git(root, "push", "origin", "refs/heads/main:refs/heads/main", check=False)
    return commit, (None if r.returncode == 0 else (r.stderr or r.stdout).strip())


def records_command(args, type_, validate, data_for, commit_msg, done_msg):
    if not args.text.strip():
        raise DepotError(f"{type_} needs non-empty text")
    git_mode = records_guard()
    item = find_item(args.item)
    events = item.events()
    validate(item)
    data = data_for(events, args.text)
    item.emit(type_, "operator", data)
    print(done_msg(item, data))
    if not git_mode:
        return 0
    commit, push_error = records_commit(item, commit_msg(item))
    if push_error:
        print(f"depot: the event is recorded and committed locally ({commit[:12]}) but the push failed: "
              f"{push_error}\ndepot: the commit stays; push it once the remote accepts main (never forced)",
              file=sys.stderr)
        return 1
    return 0


def cmd_message(args):
    def data_for(events, text):
        data = {"text": text}
        idx = max((i for i, e in enumerate(events) if e["type"] != "heartbeat"), default=None)
        if idx is not None and events[idx]["type"] == "input_required":
            rid = input_required_run_id(events, idx)
            if rid:
                data["answers_input_required"] = rid
        return data

    def done(item, data):
        extra = f" (answers input_required of run {data['answers_input_required']})" \
            if "answers_input_required" in data else ""
        return f"message recorded on {item.name}{extra}"

    return records_command(args, "message", lambda item: None, data_for,
                           lambda item: f"depot: message on {item.name}", done)


def review_data(item, commit, result, note):
    fl = floor_module()
    card = fl.card_for(yard_root(), item.name, datetime.now(timezone.utc))
    if not card["actions"]["review"]["ok"]:
        raise DepotError(card["actions"]["review"]["why"])
    if commit != card["review_commit"]:
        raise DepotError("the verified commit changed; refresh before reviewing")
    if result not in ("pass", "fail") or not note.strip():
        raise DepotError("review requires pass or fail and a non-empty note")
    return {"result": result, "note": note, "commit": commit}


def cmd_review(args):
    return records_command(args, "reviewed", lambda item: review_data(item, args.commit, args.result, args.text),
                           lambda events, text: {"result": args.result, "note": text, "commit": args.commit},
                           lambda item: f"depot: review {args.result} on {item.name}",
                           lambda item, data: f"review {data['result']} recorded on {item.name} @ {data['commit']}")


def cmd_reopen(args):
    def validate(item):
        status = item.derived_status()
        if status != "reviewed":
            raise DepotError(f"cannot reopen {item.name}: status is {status}; "
                             "only a Done card (a reviewed pass with no later reopen) can be reopened")

    return records_command(args, "reopened", validate, lambda events, text: {"text": text},
                           lambda item: f"depot: reopen {item.name}",
                           lambda item, data: f"reopened {item.name}: back to queued, needs a new brief_approved")


# ---- poll daemon ----

class PollStop(Exception):
    pass


def poll_log(msg):
    line = f"{iso()} {msg}"
    print(line, flush=True)
    p = yard_root() / "log" / "poll.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(line + "\n")


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class PollLock:
    def __init__(self):
        self.path = yard_root() / "log" / "poll.lock"

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    pid = int(self.path.read_text().strip())
                except (ValueError, OSError):
                    pid = None
                if pid and pid_alive(pid):
                    raise DepotError(f"poll is already running (pid {pid}, {self.path})")
                self.path.unlink(missing_ok=True)  # stale lock
                continue
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            return
        raise DepotError(f"could not take {self.path}")

    def release(self):
        try:
            if self.path.read_text().strip() == str(os.getpid()):
                self.path.unlink()
        except OSError:
            pass


def yard_is_git():
    return (yard_root() / ".git").exists()


def yard_has_origin():
    return git(yard_root(), "remote", "get-url", "origin", check=False).returncode == 0


def yard_push():
    """Push main (never forced). Failure is logged and retried next cycle."""
    r = git(yard_root(), "push", "origin", "refs/heads/main:refs/heads/main", check=False)
    if r.returncode != 0:
        poll_log(f"push failed (will retry next cycle): {(r.stderr or r.stdout).strip()}")
        return False
    poll_log("pushed main")
    return True


def yard_commit_message(message):
    """Commit the yard's items and global log under `message` (the daemon's own commit path). None if clean."""
    root = yard_root()
    paths = ["items"] + (["log/events.jsonl"] if (root / "log" / "events.jsonl").exists() else [])
    if (root / "log" / "vault-captures.json").exists():
        paths.append("log/vault-captures.json")
    if (root / "tracks.json").exists():
        paths.append("tracks.json")
    if (root / "tracks").exists():
        paths.append("tracks")
    git(root, "add", "--", *paths)
    if git(root, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return None
    git(root, "commit", "-q", "-m", message)
    h = git(root, "rev-parse", "HEAD").stdout.strip()
    poll_log(f"committed yard records {h[:12]} ({message})")
    return h


def yard_commit(item, outcome):
    return yard_commit_message(f"depot: {item.name} {outcome}")


# ---- floor intents (S8): the daemon is still the only writer of records ----

def floor_module():
    import floor  # lazy: the daemon only needs it once intents exist
    return floor


def global_log_seq(intent_id=None):
    """The global log's line count (the newest event's sequence); with an intent id, the line of the event that
    carries it (0 when there is none)."""
    p = yard_root() / "log" / "events.jsonl"
    if not p.exists():
        return 0
    n = found = 0
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        n += 1
        if intent_id and f'"intent": "{intent_id}"' in line:
            found = n
    return found if intent_id else n


def unlanded_message_block(fl, item_name, approve_intent):
    """Why an approve must not proceed: an earlier message intent for the same item that is still pending, or whose
    newest outcome is refused/failed. Re-read from disk at the moment of approval, never from a cached listing."""
    d = fl.intents_dir(yard_root())
    mine = []
    for p in d.glob("*.json"):
        if p.name.endswith(".outcome.json"):
            continue
        try:
            it = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if it.get("kind") == "message" and it.get("item") == item_name and \
                (it.get("ts") or "", it.get("id") or "") < (approve_intent.get("ts") or "", approve_intent.get("id") or ""):
            mine.append(it)
    mine.sort(key=lambda it: (it.get("ts") or "", it.get("id") or ""))
    for it in mine:
        if fl.read_outcome(yard_root(), it["id"]) is None:
            return f"your message {it['id'][:8]} has not landed yet; approval never dispatches around an unlanded answer"
    if mine:
        last = mine[-1]
        state = (fl.read_outcome(yard_root(), last["id"]) or {}).get("state")
        if state in ("refused", "failed"):
            return (f"your newest message {last['id'][:8]} was {state}; approval never dispatches around an "
                    "unlanded answer (send it again, or retry it)")
    return None


class IntentRefused(DepotError):
    """A deterministic validation said no: closed, with a reason. (Infrastructure errors are `failed`, not this.)"""


def apply_intent(fl, intent):
    """Land one intent as a record event. Returns the outcome fields. Raises IntentRefused for a refusal;
    any other exception is an infrastructure failure."""
    iid = intent.get("id")
    try:
        clean = fl.validate_intent(intent)
    except fl.IntentInvalid as e:
        raise IntentRefused(f"malformed intent: {e}")
    if clean["id"] != iid:
        raise IntentRefused("the intent's id does not match its file")
    if clean["kind"].startswith("track_"):
        return apply_track(fl, clean)
    if clean["kind"] == "create":
        return apply_create(fl, clean)
    item = next((it for it in all_items() if it.name == clean["item"]), None)
    if item is None:
        raise IntentRefused(f"no such item: {clean['item']}")
    events = item.events()
    for e in events:  # reconcile: the event landed but the outcome file was never written
        if e["data"].get("intent") == iid:
            return {"state": "landed", "event": {"type": e["type"], "seq": global_log_seq(iid)}, "reconciled": True}
    kind, data = clean["kind"], clean["data"]
    raw_name = (intent.get("data") or {}).get("name")
    if raw_name is not None and raw_name != data.get("name"):  # the floor reduces names at intake; a stored name must already be reduced
        raise IntentRefused(f"unsafe filename {raw_name!r}")

    def land(type_, extra):
        item.emit(type_, "operator", {**extra, "via": "floor", "intent": iid})
        item.sync_status()
        return {"state": "landed", "event": {"type": type_, "seq": global_log_seq()}}

    def card():
        try:
            return fl.card_for(yard_root(), item.name, datetime.now(timezone.utc))
        except (fl.FloorError, ValueError, KeyError) as e:
            raise IntentRefused(f"the card's state cannot be derived: {e}")

    if kind == "review":
        return land("reviewed", review_data(item, data["commit"], data["result"], data["note"]))

    if kind == "message":
        if not data["text"].strip():
            raise IntentRefused("a message needs non-empty text")
        out = {"text": data["text"]}
        idx = max((i for i, e in enumerate(events) if e["type"] != "heartbeat"), default=None)
        if idx is not None and events[idx]["type"] == "input_required":
            rid = input_required_run_id(events, idx)
            if rid:
                out["answers_input_required"] = rid
        return land("message", out)

    if kind == "requeue_interrupted":
        current = card()
        act = current["actions"][kind]
        if not act["ok"]:
            raise IntentRefused(act["why"])
        if data["run_id"] != current["posture"]["live"]["run_id"]:
            raise IntentRefused("the interrupted run changed; refresh before deciding")
        if not data["note"].strip():
            raise IntentRefused("interrupted-run recovery requires a non-empty note")
        return land("interrupted_requeued", data)

    if kind in ("adjudicate", "recover"):
        current = card()
        if not current["actions"][kind]["ok"]:
            raise IntentRefused(current["actions"][kind]["why"])
        details = (current.get("rejected_review") if kind == "adjudicate" else None) or current["blocked_details"]
        if not details or data["run_id"] != details["run_id"]:
            raise IntentRefused("the blocked run changed; refresh before deciding")
        if kind == "adjudicate":
            if not data["note"].strip():
                raise IntentRefused("adjudication requires a non-empty note")
            return land("adjudicated", {"note": data["note"], "run_id": data["run_id"]})
        recovery = details["recovery"]
        if data["commit"] != recovery["commit"]:
            raise IntentRefused("the recovery commit changed; refresh before deciding")
        return land("recovered", {**recovery, "run_id": data["run_id"]})

    if kind == "approve":
        act = card()["actions"]["approve"]
        if not act["ok"]:
            raise IntentRefused(act["why"])
        block = unlanded_message_block(fl, item.name, intent)
        if block:
            raise IntentRefused(block)
        missing = capture_gate_missing(item)
        if missing:
            raise IntentRefused("a vault capture still needs shaping: " + "; ".join(missing))
        sync_waybill_from_brief(item)  # raises DepotError (-> refused) when the brief has no valid ```depot block
        approved_data = approval_data(item)
        if data.get("contract_fingerprint") != approved_data["contract_fingerprint"]:
            raise IntentRefused("task contract changed or approval has no fingerprint; refresh and approve again")
        return land("brief_approved", {"directed_by": "operator", **approved_data})

    if kind == "park":
        reason = data["reason"].strip()
        if not reason:
            raise IntentRefused("parking needs a non-empty reason")
        act = card()["actions"]["park"]
        if not act["ok"]:
            raise IntentRefused(act["why"])
        return land("parked", {"reason": reason})

    if kind == "unpark":
        act = card()["actions"]["unpark"]
        if not act["ok"]:
            raise IntentRefused(act["why"])
        return land("unparked", {})  # releases the hold and nothing else: no dispatch, no approval

    name = data["name"]
    active = {a["file"]: a for a in active_attachments(events)}
    files_dir = item.path / "files"

    if kind == "attach":
        try:
            raw = base64.b64decode(data["content_b64"], validate=True)
        except (binascii.Error, ValueError):
            raise IntentRefused("attachment content is not valid base64")
        if len(raw) > fl.MAX_ATTACHMENT_BYTES:
            raise IntentRefused(f"attachment is over the {fl.MAX_ATTACHMENT_BYTES}-byte limit")
        if name in active:
            raise IntentRefused(f"{name} is already attached; remove it first")
        files_dir.mkdir(parents=True, exist_ok=True)
        tmp = files_dir / f".{name}.{os.getpid()}.tmp"
        tmp.write_bytes(raw)
        os.replace(tmp, files_dir / name)
        return land("attached", {"file": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})

    # remove_attachment
    if name not in active:
        raise IntentRefused(f"no such attachment: {name}")
    at = max(i for i, e in enumerate(events) if e["type"] == "attached" and e["data"].get("file") == name)
    if any(e["type"] == "dispatched" for e in events[at + 1:]):
        raise IntentRefused(f"{name} has already been delivered to a run; removing it would not recall it")
    (files_dir / name).unlink(missing_ok=True)
    return land("attachment_removed", {"file": name})


# ---- track lifecycle (S11): track-only records, with the existing item spine for the end sweep ----

TRACK_ACTS = {"track_pause": ({"active"}, "track_paused"),
              "track_resume": ({"paused"}, "track_resumed"),
              "track_table": ({"active", "paused"}, "track_tabled"),
              "track_untable": ({"tabled"}, "track_untabled"),
              "track_end": ({"active", "paused", "tabled"}, "track_ended"),
              "track_reopen": ({"ended"}, "track_reopened")}


def emit_track(fl, track, type_, iid):
    path = fl.track_log(yard_root(), track)
    path.parent.mkdir(parents=True, exist_ok=True)
    ev = {"ts": iso(), "track": track, "type": type_, "actor": "operator",
          "data": {"via": "floor", "intent": iid}}
    with path.open("a") as f:
        f.write(json.dumps(ev) + "\n")
    return ev


def apply_track(fl, clean):
    root = yard_root()
    track, iid = clean["data"]["track"], clean["id"]
    if track not in known_tracks():
        raise IntentRefused(f"no such track: {track}")
    path = fl.track_log(root, track)
    for seq, e in enumerate(fl.read_events(path), 1):
        if e.get("data", {}).get("intent") == iid:
            return {"state": "landed", "track": track, "event": {"type": e["type"], "seq": seq},
                    "reconciled": True, "records_message": f"depot: {track} {e['type']} (floor)"}
    allowed, type_ = TRACK_ACTS[clean["kind"]]
    state = fl.track_state(root, track)
    if state not in allowed:
        raise IntentRefused(f"{track} is {state}; cannot {clean['kind']}")
    sweep = []
    if type_ == "track_ended":
        cards = [c for c in fl.project(root, datetime.now(timezone.utc))["cards"] if c["track"] == track]
        pipeline = [c["name"] for c in cards if c["stage"] in ("Building", "Gating", "Review", "Interrupted")]
        if pipeline:
            raise IntentRefused("cannot end track; items in Building / Gating / Review / Interrupted: " + ", ".join(pipeline))
        sweep = [Item(root / "items" / c["name"]) for c in cards if c["stage"] != "Done"]
    # All writes enter ONE records commit. Restore byte-for-byte if an append fails. The final track marker
    # is written after the sweep, so a retry after an interrupted append can finish without duplicating parks.
    paths = [path, root / "log" / "events.jsonl"]
    for it in sweep:
        paths.extend([it.path / "events.jsonl", it.waybill_path])
    before = {p: p.read_bytes() if p.exists() else None for p in paths}
    try:
        for it in sweep:
            if not any(e["type"] == "parked" and e.get("data", {}).get("intent") == iid for e in it.events()):
                it.emit("parked", "operator", {"reason": "track ended", "via": "floor", "intent": iid})
            it.sync_status()
        emit_track(fl, track, type_, iid)
    except Exception:
        for p, raw in before.items():
            if raw is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(raw)
        raise
    return {"state": "landed", "track": track,
            "event": {"type": type_, "seq": len(fl.read_events(path))},
            "records_message": f"depot: {track} {type_} (floor)"}


# ---- floor intake (S10): the board-level `create` intent ----

TRACK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,59}$")


def relative_path_problem(p):
    """Why `p` is not a plain relative path (a file list entry, a vault path); None when it is one."""
    if not p or not p.strip():
        return "is empty"
    if p != p.strip() or any(c in p for c in "\n\r\0"):
        return "has stray whitespace or a control character"
    if p.startswith("/") or "\\" in p or re.match(r"^[A-Za-z]:", p):
        return "is not a relative path"
    if ".." in Path(p).parts:
        return "climbs out with .."
    return None


def validate_create(data):
    """Every reason a create intent cannot be filed (empty list = fine), plus the cleaned fields."""
    problems = []
    title = " ".join(data["title"].split())
    if not title:
        problems.append("title is required")
    track = data["track"].strip()
    if not track:
        problems.append("track is required")
    elif not TRACK_NAME_RE.match(track) or track.startswith("_"):
        problems.append(f"track {track!r} is not a usable name (letters, digits, space, . _ -; at most 60; not starting "
                        "with _ or a symbol)")
    if data["adapter"] not in ADAPTERS:
        problems.append(f"adapter must be one of {', '.join(sorted(ADAPTERS))} (got {data['adapter']!r})")
    if not data["body"].strip():
        problems.append("brief body is required")
    expected = []
    for entry in data["expected_files"]:
        e = entry.strip()
        if not e:
            continue  # blank lines in the list input are not entries
        why = relative_path_problem(e)
        if why:
            problems.append(f"expected file {entry!r} {why}")
        elif e not in expected:
            expected.append(e)
    if not expected:
        problems.append("expected files is required (at least one)")
    test_command = data["test_command"].strip()
    if not test_command:
        problems.append("test command is required")
    elif "\n" in test_command or "\r" in test_command:
        problems.append("test command must be a single line")
    else:
        try:
            shlex.split(test_command)
        except ValueError as e:
            problems.append(f"test command cannot be split into arguments: {e}")
    repo, branch = (data.get("repo") or "").strip(), (data.get("branch") or "").strip()
    if bool(repo) != bool(branch):
        problems.append("repo and branch go together")
    elif branch in PROTECTED_BRANCHES:
        problems.append(f"branch must be a review branch, not {branch}")
    budgets = {}
    for key in ("timeout_s", "quiet_after_s"):
        v = data.get(key)
        if v is not None:
            if v <= 0:
                problems.append(f"{key} must be a positive number")
            else:
                budgets[key] = int(v) if float(v).is_integer() else v
    lists = {}
    for key in ("links", "context"):
        clean = []
        for entry in data.get(key) or []:
            if not entry.strip():
                continue
            why = relative_path_problem(entry.strip())
            if why:
                problems.append(f"{key} entry {entry!r} {why}")
            else:
                clean.append(entry.strip())
        if clean:
            lists[key] = clean
    return problems, {"title": title, "track": track, "adapter": data["adapter"], "body": data["body"],
                      "expected_files": expected, "test_command": test_command, "repo": repo, "branch": branch,
                      "budgets": budgets, "lists": lists}


def known_tracks():
    """Tracks that exist: those named in tracks.json (besides `_default`) and those any item carries."""
    names = floor_module().track_names(yard_root())
    for it in all_items():
        try:
            t = it.waybill().get("track")
        except (OSError, ValueError):
            t = None
        if t:
            names.add(t)
    return names


def render_tracks_json(tracks):
    """tracks.json in the file's existing style: one key per line, each entry compact."""
    return "{\n" + ",\n".join(f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in tracks.items()) + "\n}\n"


def apply_create(fl, clean):
    """File a new item from a floor form. Queued, unapproved: creation is never approval."""
    iid = clean["id"]
    for it in all_items():  # reconcile: the item landed but the outcome file was never written
        for e in it.events():
            if e["type"] == "item_created" and e["data"].get("intent") == iid:
                if e["data"].get("revives_track"):
                    track = e["data"]["track"]
                    if not any(x.get("data", {}).get("intent") == iid for x in fl.read_events(fl.track_log(yard_root(), track))):
                        emit_track(fl, track, "track_untabled", iid)
                return {"state": "landed", "item": it.name, "created": True,
                        "event": {"type": "item_created", "seq": global_log_seq(iid)}, "reconciled": True}
    problems, c = validate_create(clean["data"])
    if problems:
        raise IntentRefused("nothing filed: " + "; ".join(f"({i}) {p}" for i, p in enumerate(problems, 1)))
    state = fl.track_state(yard_root(), c["track"])
    if state == "ended":
        raise IntentRefused("track ended — reopen it from the Archive to file new work")
    tracks = load_tracks()
    new_track = c["track"] not in known_tracks()
    if new_track:  # a track exists when its first item exists: its entry carries the _default budgets
        default = tracks.get("_default", {})
        tracks[c["track"]] = {"timeout_s": default.get("timeout_s", DEFAULT_TIMEOUT_S),
                              "quiet_after_s": default.get("quiet_after_s", DEFAULT_QUIET_AFTER_S)}
    root = yard_root()
    if new_track:
        (root / "tracks.json").write_text(render_tracks_json(tracks))
    d = None
    revival_before = {}
    if state == "tabled":
        for p in (fl.track_log(root, c["track"]), root / "log" / "events.jsonl"):
            revival_before[p] = p.read_bytes() if p.exists() else None
    try:
        budgets = resolve_budgets(c["track"], c["budgets"].get("timeout_s"), c["budgets"].get("quiet_after_s"))
        slug = slugify_capture(c["title"])
        new_id = next_item_id()
        d = root / "items" / f"{new_id}-{slug}"
        d.mkdir(parents=True)
        item = Item(d)
        header = json.dumps({"expected_files": c["expected_files"], "test_command": c["test_command"]})
        (d / "brief.md").write_text(f"```depot\n{header}\n```\n\n{c['body']}", encoding="utf-8")
        wb = {"id": new_id, "slug": slug, "title": c["title"], "track": c["track"], "created": iso(),
              "brief": str((d / "brief.md").relative_to(root)), "adapter": c["adapter"], "model": None,
              "timeout_s": budgets["timeout_s"], "quiet_after_s": budgets["quiet_after_s"],
              "expected_files": c["expected_files"], "test_command": c["test_command"], "status": "queued"}
        if c["repo"]:
            wb["repo"], wb["branch"] = c["repo"], c["branch"]
        wb.update(c["lists"])
        item.save_waybill(wb)
        parse_header((d / "brief.md").read_text())  # what was generated is exactly what dispatch will read
        created_data = {"via": "floor", "intent": iid, "track": c["track"]}
        if state == "tabled":
            created_data["revives_track"] = True  # durable recovery plan if interrupted before the paired track append
        item.emit("item_created", "operator", created_data)
        item.sync_status()
        if state == "tabled":
            emit_track(fl, c["track"], "track_untabled", iid)
    except Exception:
        if state == "tabled":
            for p, raw in revival_before.items():
                if raw is None:
                    p.unlink(missing_ok=True)
                else:
                    p.write_bytes(raw)
            if d is not None and d.is_dir():
                shutil.rmtree(d)
        if d is not None and d.is_dir() and not (d / "events.jsonl").exists():
            shutil.rmtree(d)  # a half-made item with no record is not an item
        if new_track:  # nothing was filed, so the track must not appear either
            (root / "tracks.json").write_text(render_tracks_json({k: v for k, v in tracks.items() if k != c["track"]}))
        raise
    return {"state": "landed", "item": item.name, "event": {"type": "item_created", "seq": global_log_seq()},
            "created": True}


def process_intents(git_mode):
    """Land pending intents oldest first, at cycle start and between dispatches. Each gets an outcome
    file: landed / refused (deterministic; closed) / failed (infrastructure; retryable). Returns how many landed."""
    root = yard_root()
    d = root / "log" / "intents"
    if not d.is_dir():
        return 0
    fl = floor_module()
    pending = []
    for p in sorted(d.glob("*.json")):
        if p.name.endswith(".outcome.json") or fl.outcome_path(root, p.stem).exists():
            continue
        try:
            intent = json.loads(p.read_text())
            if not isinstance(intent, dict):
                raise ValueError("not an object")
        except (OSError, ValueError) as e:
            intent = {"id": p.stem, "ts": "", "_unreadable": str(e)}
        pending.append(intent)
    pending.sort(key=lambda it: (it.get("ts") or "", it.get("id") or ""))
    landed = 0
    for intent in pending:
        iid = intent.get("id") or ""
        try:
            if intent.get("_unreadable"):
                raise IntentRefused(f"the intent file is unreadable: {intent['_unreadable']}")
            out = apply_intent(fl, intent)
        except DepotError as e:  # includes IntentRefused
            out = {"state": "refused", "reason": str(e)}
        except Exception as e:
            out = {"state": "failed", "reason": f"{type(e).__name__}: {e}"}
        out.update(intent=iid, ts=iso())
        records_message = out.pop("records_message", None)
        if out["state"] == "landed":
            landed += 1
            if records_message and git_mode:
                try:
                    yard_commit_message(records_message)
                except DepotError as e:
                    # Leave the intent pending and stop this batch. No later create or sweep can commit around it.
                    poll_log(f"could not commit track act (will reconcile next cycle): {e}")
                    return landed
            if out.pop("created", False) and git_mode:  # one records commit per landed create
                try:
                    yard_commit_message(f"depot: {out['item']} created (floor)")
                except DepotError as e:
                    poll_log(f"could not commit {out['item']} (will reconcile next cycle): {e}")
                    return landed
        if not floor_module().INTENT_ID_RE.match(iid):
            poll_log(f"intent {iid!r}: bad id; not recording an outcome")
            continue
        try:
            fl.write_atomic(fl.outcome_path(root, iid), json.dumps(out, sort_keys=True))
        except OSError as e:  # the event (if any) is in the log; the next cycle reconciles by intent id
            poll_log(f"intent {iid[:8]}: could not write its outcome ({e}); the next cycle will reconcile it")
            continue
        poll_log(f"intent {iid[:8]} ({intent.get('kind', '?')} on {intent.get('item', '?')}): {out['state']}"
                 + (f" - {out['reason']}" if out.get("reason") else ""))
    if landed and git_mode:
        try:
            yard_commit_message(f"depot: floor intents ({landed} landed)")
        except DepotError as e:
            poll_log(f"could not commit landed intents (will retry with the next records commit): {e}")
    return landed


def refuse_or_cap(it, refusals, error, git_mode):
    """Count a pre-run refusal. On the REFUSAL_CAP-th consecutive one, record a single `blocked` event."""
    rec = refusals.setdefault(it.name, {"count": 0, "events": len(it.events()), "capped": False})
    rec["count"] += 1
    poll_log(f"dispatch of {it.name} refused ({rec['count']}/{REFUSAL_CAP}): {error}")
    if rec["count"] < REFUSAL_CAP:
        return
    reason = f"dispatch refused {REFUSAL_CAP} times: {error}"
    newest = newest_event(it.events())
    if not (newest and newest["type"] == "blocked" and newest["data"].get("reason") == reason):
        it.emit("blocked", "turntable", {"reason": reason})
        it.sync_status()
        if git_mode:
            yard_commit(it, "blocked")
    rec["capped"], rec["events"] = True, len(it.events())
    poll_log(f"{it.name} blocked after {REFUSAL_CAP} refusals; no further attempts until its state changes")


def reconcile_unfinished_runs(git_mode=False, now=None):
    """Record abandoned runs, never authorize or start a replacement run.

    Poll dispatches synchronously: this scan runs between dispatches under the poll lock.
    Use the original dispatch budget, not quiet age (heartbeats are not completion).
    """
    now = now or datetime.now(timezone.utc)
    for item in all_items():
        pending = None
        for event in item.events():
            if event["type"] == "dispatched":
                pending = event
            elif event["type"] in ("run_finished", "run_interrupted"):
                pending = None
        if pending is None:
            continue
        data = pending["data"]
        bound = float(data.get("timeout_s", item.waybill().get("timeout_s", DEFAULT_TIMEOUT_S))) + INTERRUPTED_GRACE_S
        age = (now - datetime.fromisoformat(pending["ts"].replace("Z", "+00:00"))).total_seconds()
        if age <= bound:
            continue
        item.emit("run_interrupted", "turntable", {
            "run_id": data.get("run_id"), "dispatched_at": pending["ts"], "bound_s": bound,
            "reason": "No run_finished recorded after the run timeout plus cleanup grace; operator recovery required."})
        item.sync_status()
        poll_log(f"interrupted {item.name}: operator recovery required")
        if git_mode:
            yard_commit(item, "interrupted")


def poll_cycle(stop, refusals=None):
    """One daemon cycle. `refusals` is the in-memory consecutive-refusal count per item (a restart forgives it;
    the `blocked` event is the durable record)."""
    refusals = {} if refusals is None else refusals
    root = yard_root()
    git_mode = yard_is_git()
    if git_mode:
        branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if branch != "main":
            raise PollStop(f"yard is on branch {branch}; the daemon operates on main")
        if yard_has_origin():
            r = git(root, "pull", "--ff-only", "-q", check=False)
            if r.returncode != 0:
                counts = git(root, "rev-list", "--left-right", "--count", "main...origin/main", check=False)
                ahead, behind = counts.stdout.split() if counts.returncode == 0 else ("unknown", "unknown")
                poll_log(f"git pull --ff-only failed (ahead={ahead} behind={behind}; "
                         f"skipping cycle, will retry next cycle): {(r.stderr or r.stdout).strip()}")
                return
            poll_log("pulled (ff-only)")
    reconcile_unfinished_runs(git_mode)
    try:
        process_intents(git_mode)
    except Exception:  # keep the daemon alive, but do not dispatch around pending controls
        import traceback
        poll_log(f"intent processing crashed: {traceback.format_exc().strip()}")
        return
    try:
        vault_capture_scan(git_mode, poll_log)
    except Exception:  # a bridge failure is logged and never stops the daemon or blocks a dispatch
        import traceback
        poll_log(f"vault capture scan crashed: {traceback.format_exc().strip()}")
    approved = [it for it in all_items() if it.derived_status() == "approved" and not active_park(it.events())]
    poll_log(f"cycle: {len(approved)} approved item(s)" + (f": {', '.join(i.name for i in approved)}" if approved else ""))
    skipped_tracks = set()
    for it in approved:
        # A run can take minutes. Land new operator decisions before considering another candidate.
        try:
            process_intents(git_mode)
            fl = floor_module()
            for path in fl.intents_dir(root).glob("*.json"):
                if not path.name.endswith(".outcome.json") and not fl.outcome_path(root, path.stem).exists():
                    poll_log("pending intents remain; deferring further dispatches")
                    return
        except Exception:
            import traceback
            poll_log(f"intent processing crashed; deferring dispatches: {traceback.format_exc().strip()}")
            return
        track = it.waybill().get("track", "")
        state = floor_module().track_state(root, track)
        if state != "active":
            if track not in skipped_tracks:
                poll_log(f"skipping track {track}: {state}")
                skipped_tracks.add(track)
            continue
        if it.derived_status() != "approved" or fl.card_for(root, it.name, datetime.now(timezone.utc))["parked"]:
            continue
        if stop.is_set():
            poll_log("stop requested; not starting further dispatches")
            break
        rec = refusals.get(it.name)
        if rec and rec["events"] != len(it.events()):
            refusals.pop(it.name)  # the item's log grew since the last attempt: the count starts over
            rec = None
        if rec and rec["capped"]:
            continue
        poll_log(f"dispatching {it.name}")
        before = len(it.events())
        try:
            code, outcome = dispatch_item(it)
        except DispatchBlocked as e:  # a `blocked` event was appended: commit it so the record is not left dirty
            poll_log(f"dispatch of {it.name} refused: {e}")
            if git_mode:
                yard_commit(it, "blocked")
            continue
        except DepotError as e:
            if len(it.events()) == before:  # refused before any run began: the only kind the cap counts
                refuse_or_cap(it, refusals, e, git_mode)
            else:
                poll_log(f"dispatch of {it.name} refused: {e}")
            continue
        except Exception:  # keep the loop alive; the traceback lands in poll.log
            import traceback
            poll_log(f"dispatch of {it.name} crashed: {traceback.format_exc().strip()}")
            continue
        refusals.pop(it.name, None)
        poll_log(f"dispatched {it.name}: exit={code} outcome={outcome}")
        if git_mode:
            yard_commit(it, outcome)
    try:
        vault_project_all(poll_log)  # after the cycle's records commits: notes describe committed state
    except Exception:
        import traceback
        poll_log(f"vault projection crashed: {traceback.format_exc().strip()}")
    if git_mode and yard_has_origin():
        ahead = git(root, "rev-list", "--count", "origin/main..main", check=False)
        if ahead.returncode != 0 or int(ahead.stdout.strip() or 0) > 0:
            yard_push()


# ---- vault bridge (S9): projection out, capture in, agent context ----
# The vault is operator's Obsidian vault (DEPOT_VAULT). The daemon is the only writer on both sides, and it writes only
# Depot/Items/, Depot/Inbox/Processed/ (and removes the filed capture from Depot/Inbox/). Unset or missing = disabled.

def vault_root():
    v = os.environ.get("DEPOT_VAULT")
    if not v:
        return None
    p = Path(v).expanduser()
    return p if p.is_dir() else None


def capture_ledger_path():
    return yard_root() / "log" / "vault-captures.json"


def load_capture_ledger():
    p = capture_ledger_path()
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    if not isinstance(data, dict):
        raise DepotError("log/vault-captures.json must be a JSON object")
    return data


def capture_origin(events):
    return any(e["type"] == "item_created" and e["data"].get("via") == CAPTURE_VIA for e in events)


def capture_gate_missing(item):
    """What a vault-captured item still lacks before it may be approved or dispatched; [] for every other item
    (their approval behaviour is unchanged)."""
    if not capture_origin(item.events()):
        return []
    missing = []
    if "adapter" not in item.waybill():
        missing.append('the waybill has no "adapter" key')
    hdr = parse_header((item.path / "brief.md").read_text())
    if not hdr["expected_files"]:
        missing.append("the brief header's expected_files is empty")
    if not hdr["test_command"].strip():
        missing.append("the brief header's test_command is empty")
    return missing


def slugify_capture(title):
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if len(s) > 40:
        cut = s[:40]
        s = cut.rsplit("-", 1)[0] if s[40] != "-" and "-" in cut else cut
    return s.strip("-") or "capture"


def capture_title(text):
    for line in text.splitlines():
        t = line.strip().lstrip("#").strip()
        if t:
            return t
    return None


def stamp_capture(raw, item_name):
    """The capture's bytes with `depot_item` / `depot_filed` frontmatter prepended (merged into an existing block)."""
    stamp = f"depot_item: {item_name}\ndepot_filed: {iso()}\n".encode()
    if raw.startswith(b"---\n") and b"\n---\n" in raw[4:]:
        return b"---\n" + stamp + raw[4:]
    return b"---\n" + stamp + b"---\n" + raw


def move_capture_to_processed(vault, path, raw, item_name):
    processed = vault / "Depot" / "Inbox" / "Processed"
    processed.mkdir(parents=True, exist_ok=True)
    dest = processed / path.name
    n = 2
    while dest.exists():
        dest = processed / f"{path.stem}-{n}{path.suffix}"
        n += 1
    tmp = processed / f".{dest.name}.tmp"
    tmp.write_bytes(stamp_capture(raw, item_name))
    os.replace(tmp, dest)
    path.unlink()


def file_capture(path, raw, text, git_mode):
    """Create the item for one capture: Queued, draft defaults, adapter deliberately absent. Returns the Item."""
    title = capture_title(text)
    slug = slugify_capture(title)
    items_dir = yard_root() / "items"
    items_dir.mkdir(parents=True, exist_ok=True)
    new_id = next_item_id()
    budgets = resolve_budgets("inbox")  # the existing resolution: waybill > track > _default > built-in
    d = items_dir / f"{new_id}-{slug}"
    d.mkdir()
    item = Item(d)
    (d / "brief.md").write_bytes(BRIEF_SKELETON_HEADER.encode() + raw)
    wb = {"id": new_id, "slug": slug, "title": title, "track": "inbox", "created": iso(),
          "brief": str((d / "brief.md").relative_to(yard_root())), "model": None,
          "timeout_s": budgets["timeout_s"], "quiet_after_s": budgets["quiet_after_s"],
          "links": [], "context": [], "status": "queued"}
    item.save_waybill(wb)
    item.emit("item_created", "turntable", {"title": title, "track": "inbox", "via": CAPTURE_VIA, "source": path.name})
    return item


def vault_capture_scan(git_mode, log):
    """File every new capture note in <vault>/Depot/Inbox/. Returns [(item name, filename)] filed this call."""
    vault = vault_root()
    if vault is None:
        return []
    inbox = vault / "Depot" / "Inbox"
    if not inbox.is_dir():
        return []
    filed = []
    try:
        ledger = load_capture_ledger()
    except (DepotError, ValueError) as e:
        log(f"vault capture: cannot read the ledger, scanning nothing: {e}")
        return []
    for path in sorted(p for p in inbox.iterdir() if p.is_file() and p.suffix == ".md" and not p.name.startswith(".")):
        try:
            raw = path.read_bytes()
            key = f"{path.name}:{hashlib.sha256(raw).hexdigest()}"
            if key in ledger:  # already filed (a re-sync, or a receipt that failed): repair the receipt, never re-file
                try:
                    move_capture_to_processed(vault, path, raw, ledger[key])
                except OSError as e:
                    log(f"vault capture: {path.name} is already filed as {ledger[key]}; receipt still pending: {e}")
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                log(f"vault capture: {path.name} is not valid UTF-8; skipped, not filed")
                continue
            if capture_title(text) is None:
                log(f"vault capture: {path.name} has no text; skipped, not filed")
                continue
            item = file_capture(path, raw, text, git_mode)
            ledger[key] = item.name
            write_json(capture_ledger_path(), ledger)
            filed.append((item.name, path.name))
            log(f"vault capture: filed {item.name} from {path.name}")
            if git_mode:
                yard_commit_message(f"depot: {item.name} created (vault capture)")
            try:
                move_capture_to_processed(vault, path, raw, item.name)
            except OSError as e:  # the ledger prevents a duplicate; the file stays for the next cycle's repair
                log(f"vault capture: filed {item.name} but could not stamp and move {path.name}: {e}")
        except Exception as e:  # one bad capture never stops the others or the daemon
            log(f"vault capture: {path.name} failed: {type(e).__name__}: {e}")
    return filed


def yaml_scalar(v):
    v = str(v)
    if re.fullmatch(r"[A-Za-z0-9_./ -]+", v) and v == v.strip() and v.lower() not in (
            "true", "false", "null", "yes", "no", "on", "off", "~") and not re.fullmatch(r"[-.0-9]+", v):
        return v
    return json.dumps(v)


def wikilink(rel):
    """`Notes/reference.md` -> `[[Notes/reference]]`; None for an entry that cannot be one."""
    if not isinstance(rel, str):
        return None
    r = rel.strip()
    if r.lower().endswith(".md"):
        r = r[:-3]
    if not r or r.startswith("/") or ".." in Path(r).parts or any(c in r for c in "[]|\n\r"):
        return None
    return f"[[{r}]]"


def render_projection(item, card, wb, events):
    quiet = [e for e in events if e["type"] != "heartbeat"]
    created = wb.get("created") or events[0]["ts"]
    updated = quiet[-1]["ts"] if quiet else created
    fm = ["---", f'depot_id: "{card["id"]}"', f"track: {yaml_scalar(card['track'])}"]
    if "adapter" in wb:
        fm.append(f"adapter: {yaml_scalar(wb['adapter'])}")
    fm += [f"stage: {yaml_scalar(card['stage'])}", f"truth: {card['truth']}", f"created: {created}",
           f"updated: {updated}", "tags: [depot/item]", "---"]
    title = " ".join(str(card["title"]).split())
    out = "\n".join(fm) + "\n"
    out += (f"<!-- Generated by Depot — the record lives in the yard (items/{item.name}). "
            "Annotate the card, not this note. -->\n\n")
    # The card's own newest fact (a message is never the headline: floor rule since S4), minus heartbeats, which
    # only liveness-stamp a run and would rewrite the note every 30s. Same wording as the card.
    facts = [f for f in card["trail"] if f["type"] != "message"]
    headline = facts[-1]["text"] if facts else card["last_event"]["text"]
    out += f"# {card['id']} — {title}\n\n{headline}\n\n"
    out += f"Brief: items/{item.name}/brief.md (yard)\n"
    links = [w for w in (wikilink(x) for x in (wb.get("links") or [])) if w]
    if links:
        out += "\n## Links\n" + "".join(f"- {w}\n" for w in links)
    return out


def vault_project_all(log):
    """Regenerate each item's note in <vault>/Depot/Items/ when its content changed. Returns item names rewritten.
    Stage and truth come from the floor's own derivation; a note is only ever overwritten by its own item."""
    vault = vault_root()
    if vault is None:
        return []
    floor = floor_module()
    out_dir = vault / "Depot" / "Items"
    now = datetime.now(timezone.utc)
    written = []
    for item in all_items():
        try:
            card = floor.card_for(yard_root(), item.name, now)
            text = render_projection(item, card, item.waybill(), item.events())
            dest = out_dir / f"{item.name}.md"
            if dest.exists() and dest.read_text(encoding="utf-8") == text:
                continue
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp = out_dir / f".{item.name}.md.tmp"
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, dest)
            written.append(item.name)
        except Exception as e:  # one item's projection never stops the others
            log(f"vault projection of {item.name} failed: {type(e).__name__}: {e}")
    return written


def resolve_context(wb):
    """The waybill's `context` notes, resolved against the vault: [{path, src, base, sha256}] in waybill order.
    A missing, unsafe or colliding note refuses the dispatch (pre-run DepotError); nothing is silently skipped."""
    paths = wb.get("context") or []
    if not paths:
        return []
    vault = vault_root()
    if vault is None:
        raise DepotError("the waybill lists context notes but DEPOT_VAULT is not set (or is not a directory)")
    root = vault.resolve()
    out, seen = [], set()
    for rel in paths:
        if not isinstance(rel, str) or not rel.strip():
            raise DepotError(f"bad context entry {rel!r}: use a vault-relative note path")
        src = (vault / rel).resolve()
        if root not in src.parents:
            raise DepotError(f"context path {rel!r} is outside the vault")
        if not src.is_file():
            raise DepotError(f"context note missing from the vault: {rel}")
        base = Path(rel).name
        if base in seen:
            raise DepotError(f"two context notes share the name {base}; rename one or list only one")
        seen.add(base)
        out.append({"path": rel, "src": src, "base": base, "sha256": hashlib.sha256(src.read_bytes()).hexdigest()})
    return out


def deliver_context(item, work, context):
    """Copy the context notes into <work>/context/, and keep that directory out of git so the diff and the host
    commit never see it (the changed-set computation excludes it for every run, see changed_files)."""
    dest = work / CONTEXT_DIR
    if not context:
        runs = sorted(p for p in (item.path / "runs").glob("*") if p.is_dir()) if (item.path / "runs").is_dir() else []
        try:
            previous = json.loads((runs[-1] / "manifest.json").read_text()).get("context") if runs else None
        except (OSError, ValueError):
            previous = None
        if previous and dest.exists():
            shutil.rmtree(dest)  # last run's notes must not linger when this run's manifest says none
        return
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for c in context:
        shutil.copyfile(c["src"], dest / c["base"])
    exclude = work / ".git" / "info" / "exclude"
    if exclude.parent.is_dir():
        line = f"/{CONTEXT_DIR}/"
        have = exclude.read_text().splitlines() if exclude.exists() else []
        if line not in have:
            with open(exclude, "a") as f:
                f.write(("\n" if have and not exclude.read_text().endswith("\n") else "") + line + "\n")


def cmd_vault_sync(args):
    """One capture scan plus projection regeneration, with the poll cycle's records-commit behaviour."""
    if vault_root() is None:
        raise DepotError("the vault bridge is disabled: DEPOT_VAULT is unset or is not a directory")
    git_mode = records_guard()
    err = lambda m: print(m, file=sys.stderr)  # noqa: E731
    filed = vault_capture_scan(git_mode, err)
    for name, source in filed:
        print(f"filed {name} from {source}")
    projected = vault_project_all(err)
    for name in projected:
        print(f"projected {name}")
    if not filed and not projected:
        print("nothing to do")
    if filed and git_mode and yard_has_origin():
        r = git(yard_root(), "push", "origin", "refs/heads/main:refs/heads/main", check=False)
        if r.returncode != 0:
            print(f"depot: filed items are committed locally but the push failed: {(r.stderr or r.stdout).strip()}",
                  file=sys.stderr)
            return 1
    return 0


def cmd_poll(args):
    lock = PollLock()
    lock.acquire()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    poll_log(f"poll start pid={os.getpid()} interval={args.interval}s once={args.once}")
    refusals = {}
    try:
        while True:
            try:
                poll_cycle(stop, refusals)
            except PollStop as e:
                poll_log(f"poll stopping: {e}")
                return 3
            if args.once or stop.is_set():
                break
            deadline = time.monotonic() + args.interval
            while time.monotonic() < deadline and not stop.is_set():
                time.sleep(0.25)
            if stop.is_set():
                break
        return 0
    finally:
        poll_log("poll stop")
        lock.release()


def cmd_floor(args):
    sys.dont_write_bytecode = True  # the floor writes nothing, not even bytecode
    import floor
    return floor.serve(yard_root(), int(os.environ.get("DEPOT_FLOOR_PORT", "8765")))


def main(argv=None):
    p = argparse.ArgumentParser(prog="depot.py", description="Depot turntable")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("new-item")
    s.add_argument("--title", required=True)
    s.add_argument("--slug", required=True)
    s.add_argument("--track", required=True)
    s.add_argument("--repo", help="git remote URL for a repo-targeted item (needs --branch)")
    s.add_argument("--branch", help="review branch for the return push (needs --repo)")
    s.add_argument("--timeout-s", type=float, help="run timeout; overrides tracks.json")
    s.add_argument("--quiet-after-s", type=float, help="adapter-quiet limit for stalled?; overrides tracks.json")
    s.add_argument("--kind", choices=("build", "gate"), default="build")
    s.add_argument("--target", help="gate items: the item being gated")
    s.add_argument("--material", action="append",
                   help="gate items: extra material, item:<name> or run:<name> (repeatable)")
    s.set_defaults(fn=cmd_new_item)
    for name, fn in (("approve", cmd_approve), ("dispatch", cmd_dispatch), ("log", cmd_log)):
        s = sub.add_parser(name)
        s.add_argument("item")
        s.set_defaults(fn=fn)
    for name, fn, hlp in (("message", cmd_message, "attach a message to any card (never a state change)"),
                          ("reopen", cmd_reopen, "return a Done card to Queued, with a message riding on it")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("item")
        s.add_argument("text")
        s.set_defaults(fn=fn)
    s = sub.add_parser("review", help="accept or reject the recorded verified commit with a note")
    s.add_argument("item")
    s.add_argument("result", choices=("pass", "fail"))
    s.add_argument("text", help="review note (required)")
    s.add_argument("--commit", required=True, help="full verified work commit hash")
    s.set_defaults(fn=cmd_review)
    s = sub.add_parser("status")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("vault-sync", help="one vault capture scan + projection regeneration (needs DEPOT_VAULT)")
    s.set_defaults(fn=cmd_vault_sync)
    s = sub.add_parser("poll")
    s.add_argument("--once", action="store_true", help="run exactly one cycle, then exit")
    s.add_argument("--interval", type=float, default=60.0, help="seconds between cycles (default 60)")
    s.set_defaults(fn=cmd_poll)
    s = sub.add_parser("floor", help="serve the read-only stage map on 127.0.0.1 (port: DEPOT_FLOOR_PORT, default 8765)")
    s.set_defaults(fn=cmd_floor)
    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except DepotError as e:
        print(f"depot: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
