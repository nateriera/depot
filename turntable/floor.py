"""Depot floor (S3): a read-only projection of the yard into stage-map cards, and a local server for it.

Standard library only. The projection is a pure function of the yard's files and `now`; nothing here
writes to the yard. Stage derivation and the truth mapping follow the S3 brief and spec §8 and §10.
"""
import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Column order (spec §8); Interrupted holds stranded runs pending recovery,
# rather than a required gate on the path to Done.
STAGES = ["Queued", "Briefed", "Building", "Gating", "Review", "Awaiting operator", "Interrupted", "Done"]
NO_CHECKER = "Checker: not supplied"
DEFAULT_QUIET_AFTER_S = 300  # spec §7: five minutes
DEFAULT_TIMEOUT_S = 900
PAGE_DIR = Path(__file__).resolve().parent.parent / "floor"

# Event types that read as a system observation when they are the newest fact.
RECORDED = {"item_created", "brief_approved", "dispatched", "heartbeat", "run_finished", "committed",
            "pushed", "push_failed", "input_required", "parked", "unparked", "message", "reopened", "blocked",
            "attached", "attachment_removed", "adjudicated", "recovered", "run_interrupted", "interrupted_requeued"}
KNOWN = RECORDED | {"claimed", "verified", "gate_result", "reviewed"}
ADAPTER_NAMES = {"codex": "Codex", "claude": "Claude"}
# ---- floor intents (S8): the write half. The floor only DROPS intents; the poll daemon lands them. ----
INTENT_KINDS = ("message", "approve", "park", "unpark", "attach", "remove_attachment", "create",
                "adjudicate", "recover", "review", "requeue_interrupted", "track_pause", "track_resume", "track_table", "track_untable", "track_end", "track_reopen")
MAX_REQUEST_BYTES = 8 * 1024 * 1024    # the whole POST body
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # decoded file content
MAX_TEXT_CHARS = 20000                 # a message or a park reason
MAX_BRIEF_BODY_CHARS = 200000          # a create intent's brief body (S10)
CREATE_STRING_FIELDS = {"title": 300, "track": 100, "adapter": 40, "body": MAX_BRIEF_BODY_CHARS,
                        "test_command": 1000}
CREATE_OPTIONAL_STRINGS = {"repo": 1000, "branch": 200}
CREATE_LISTS = {"expected_files": 200, "links": 200, "context": 200}  # max entries; each entry at most 500 characters
ITEM_RE = re.compile(r"^[0-9]{4}-[a-z0-9]+(-[a-z0-9]+)*$")
INTENT_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
INTENT_STATES = ("pending", "reconciling", "landed", "refused", "failed")
RECEIPT_LIMIT = 60                     # receipts per card in the projection (newest)
ANNOTATION_LIMIT = 120  # S3's card text treatment: first line, 120 characters, ellipsis
QUESTION_LIMIT = 200


class FloorError(Exception):
    """The yard holds a state the projection has no row for. Surfaced, never guessed at."""


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso_z(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def as_dt(now):
    if isinstance(now, str):
        return parse_ts(now)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now


def exact(seconds):
    """Duration for the stalled? evidence: 45s, 3m 12s, 10m, 2h 05m."""
    s = int(seconds)
    if s < 3600:
        return f"{s}s" if s < 60 else f"{s // 60}m" + (f" {s % 60:02d}s" if s % 60 else "")
    return span(s)


def span(seconds):
    """Compact duration for reason text: 45s, 6m, 2h 05m, 1d 3h."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    d, h = s // 86400, s % 86400 // 3600
    return f"{d}d {h}h" if h else f"{d}d"


# ---- reading the yard ----

def read_events(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def budgets(root, wb):
    """(timeout_s, quiet_after_s) for a card: the waybill's own values, else tracks.json (track entry, then
    `_default`), else the built-in defaults. A read-time fallback only; nothing is written back."""
    out = {"timeout_s": wb.get("timeout_s"), "quiet_after_s": wb.get("quiet_after_s")}
    if any(v is None for v in out.values()):
        p = Path(root) / "tracks.json"
        try:
            tracks = json.loads(p.read_text()) if p.exists() else {}
        except (OSError, ValueError):
            tracks = {}
        for entry in (tracks.get(wb.get("track")), tracks.get("_default")):
            for k in out:
                if out[k] is None and isinstance(entry, dict) and isinstance(entry.get(k), (int, float)):
                    out[k] = entry[k]
    return (out["timeout_s"] if out["timeout_s"] is not None else DEFAULT_TIMEOUT_S,
            out["quiet_after_s"] if out["quiet_after_s"] is not None else DEFAULT_QUIET_AFTER_S)


def load_yard(root):
    items_dir = Path(root) / "items"
    out = []
    if items_dir.is_dir():
        for d in sorted(items_dir.iterdir()):
            wbp = d / "waybill.json"
            if d.is_dir() and wbp.exists():
                out.append((d.name, json.loads(wbp.read_text()), read_events(d / "events.jsonl")))
    return out


def gate_completions(yard, global_log):
    """gate item name -> its gate_result event, from item trails first, then the global log."""
    done = {}
    for _name, _wb, events in yard:
        for e in events:
            g = e.get("data", {}).get("gate")
            if e.get("type") == "gate_result" and isinstance(g, str) and g not in done:
                done[g] = e
    for e in global_log:
        g = e.get("data", {}).get("gate")
        if e.get("type") == "gate_result" and isinstance(g, str) and g not in done:
            done[g] = e
    return done


# ---- text and truth ----

def hands_name(adapter, gate=False):
    base = ADAPTER_NAMES.get(adapter, adapter[:1].upper() + adapter[1:]) if adapter else "—"
    return f"{base} (gate)" if gate and adapter else base


def first_line(text, limit=120):
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def clip(text, limit):
    """Whitespace collapsed to single spaces, cut at `limit` characters with an ellipsis."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def criteria_counts(criteria):
    passed = sum(1 for c in criteria if re.match(r"^CRITERION \d+: PASS\b", c))
    return passed, len(criteria)


def event_text(e):
    t, d = e["type"], e.get("data", {})
    if t == "item_created":
        return "card created"
    if t == "brief_approved":
        return "brief approved" + (f" · via {d['via']}" if d.get("via") else "")
    if t == "dispatched":
        return f"dispatched to {d.get('adapter', 'adapter')}"
    if t == "heartbeat":
        since = d.get("since_last_adapter_event_s")
        return f"heartbeat · adapter silent for {span(since)}" if since is not None else "heartbeat"
    if t == "run_finished":
        if d.get("timed_out"):
            return "run timed out and was killed"
        return f"run finished · exit {d.get('exit_code')} · {span(d.get('elapsed_s') or 0)}"
    if t == "claimed":
        return "agent report filed"
    if t == "input_required":
        return f"question: {first_line(d.get('question'))}"
    if t == "verified":
        result = d.get("result")
        if result == "pass":
            return "tests re-run passed" if d.get("test_exit_code") == 0 else "checks passed"
        if result == "tests_failed":
            return f"tests re-run failed (exit {d.get('test_exit_code')})"
        paths = ", ".join(x.get("path", "?") for x in d.get("discrepancies", [])[:3])
        return f"file-set discrepancies: {len(d.get('discrepancies', []))} ({paths})"
    if t == "committed":
        return f"committed {str(d.get('hash', ''))[:7]}"
    if t == "pushed":
        return f"pushed {d.get('branch', '')} @ {str(d.get('remote_head', ''))[:7]}"
    if t == "push_failed":
        return f"push failed: {first_line(d.get('error'))}"
    if t == "gate_result":
        result = d.get("result", "?")
        if result == "unparseable":
            return "gate_result: unparseable"
        p, n = criteria_counts(d.get("criteria", []))
        return f"gate_result: {result}" + (f" ({p}/{n})" if n else "")
    if t == "reviewed":
        extra = f" · {d['review']}" if d.get("review") else ""
        checker = "operator" if e.get("actor") == "operator" else "reviewer"
        return f"reviewed: {d.get('result', '?')} ({checker})" + (f" · {d['note']}" if d.get("note") else extra)
    if t == "parked":
        return f"parked: {first_line(d.get('reason')) or 'no reason given'}"
    if t == "unparked":
        return "unparked: hold released, nothing dispatched"
    if t == "attached":
        return f"attached {d.get('file', '?')} ({d.get('bytes', '?')} bytes)"
    if t == "attachment_removed":
        return f"attachment removed: {d.get('file', '?')}"
    if t == "message":
        return f"{e.get('actor', '?')}: {first_line(d.get('text')) or '(empty)'}"
    if t == "reopened":
        return "reopened" + (f": {first_line(d['text'])}" if d.get("text") else "")
    if t in ("adjudicated", "recovered"):
        return t + ": " + str(d.get("note") or ((d.get("branch") or "detached HEAD") + " @ " + d.get("commit", "")))
    if t == "run_interrupted":
        return "run interrupted: " + str(d.get("reason", "operator recovery required"))
    if t == "interrupted_requeued":
        return "interrupted run re-queued: " + str(d.get("note", ""))
    if t == "blocked":
        return f"blocked: {first_line(d.get('reason')) or 'no reason given'}"
    raise FloorError(f"no text for event type {t}")


def gate_checker(e):
    name = e.get("data", {}).get("gate")
    actor = e.get("actor")
    if not isinstance(name, str) or not name or not actor:
        return NO_CHECKER
    m = re.match(r"^\d+-(.+)$", name)
    return f"{m.group(1) if m else name} ({actor})"


def review_commit(events):
    """The commit produced after the current run's passing verification, never a workspace HEAD."""
    verified = False
    commit = None
    for e in events:
        t, d = e["type"], e.get("data", {})
        if t in ("item_created", "brief_approved", "dispatched", "reopened", "recovered"):
            verified, commit = False, None
        elif t == "reviewed" and d.get("result") != "pass":
            verified, commit = False, None
        elif t == "verified":
            verified, commit = d.get("result") == "pass", None
        elif t == "committed" and verified:
            h = d.get("hash", "")
            commit = h if isinstance(h, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", h) else None
    return commit


def recovered_work(events):
    """Latest recovery pointer, retained after a later verified commit supersedes it."""
    recovery = None
    dispatched = verified = False
    for e in events:
        t, d = e["type"], e.get("data", {})
        if t == "recovered":
            recovery = {**d, "superseded_by": None}
            dispatched = verified = False
        elif t in ("brief_approved", "dispatched", "reopened"):
            verified = False
            dispatched = t == "dispatched"
        elif t == "verified":
            verified = dispatched and d.get("result") == "pass"
        elif t == "committed" and recovery and not recovery["superseded_by"] and verified:
            commit = d.get("hash", "")
            if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
                recovery["superseded_by"] = commit
    return recovery


def work_directory(events):
    """Current run's workspace, always beneath the item's ignored work directory."""
    dispatch = next((e for e in reversed(events) if e["type"] == "dispatched"), {})
    directory = dispatch.get("data", {}).get("work_directory", "work")
    path = Path(directory)
    if path.is_absolute() or not path.parts or path.parts[0] != "work" or ".." in path.parts:
        raise FloorError("invalid dispatched work directory")
    return path


def truth_of(e):
    """(class, checker) for one event. Only a passing check is verified."""
    t = e["type"]
    checked_truth = "verified" if e.get("data", {}).get("result") == "pass" else "checked_failed"
    if t == "claimed":
        return "claimed", None
    if t == "verified":
        return checked_truth, "turntable (mechanical)"
    if t == "reviewed":
        return checked_truth, "operator" if e.get("actor") == "operator" else "reviewer"
    if t == "gate_result":
        if e.get("data", {}).get("result") == "unparseable":
            return "recorded", None  # nothing was verified; the turntable recorded that the verdict could not be read
        return checked_truth, gate_checker(e)
    if t in RECORDED:
        return "recorded", None
    raise FloorError(f"no truth class for event type {t}")


def fact(e):
    cls, checker = truth_of(e)
    return {"type": e["type"], "actor": e.get("actor"), "ts": e["ts"], "text": event_text(e),
            "truth": cls, "checker": checker}


# ---- what the panel may offer: one table, used by the projection AND the daemon's validation ----

def allowed_actions(stage, parked, live, blocked, asked, open_question, recovered=False, review_failed=False):
    """{action: {ok, why}} for one card. `why` is the in-place reason when ok is false. The daemon refuses an
    intent the same table disables, so the panel's disabled control and the daemon's refusal cannot disagree."""
    no_approval = "This card cannot be dispatched from its current stage. Review its outcome or recovery options first."
    if parked is not None:
        approve = (False, "This card is parked: unpark it first, then approve.")
    elif live:
        approve = (False, "A run is already live; this panel will not dispatch another alongside it.")
    elif stage == "Briefed":
        approve = (False, "Already approved: the next daemon cycle dispatches it.")
    elif stage == "Review" and recovered and not blocked:
        approve = (True, "")
    elif (stage in ("Queued", "Awaiting operator") or (stage in ("Building", "Gating") and (blocked or asked))
          or (stage == "Review" and review_failed)):
        approve = (True, "")  # asked: the last run ended on a question, answered or not; blocked: it can be re-run
    elif stage in ("Building", "Gating"):
        approve = (False, "The turntable is still verifying the last run.")
    else:
        approve = (False, no_approval)
    if parked is not None:
        park = (False, "Already parked.")
    elif live:
        park = (False, "A run is live; parking does not cancel it.")
    elif stage in ("Queued", "Briefed", "Awaiting operator") or (stage in ("Building", "Gating") and asked):
        park = (True, "")
    else:
        park = (False, "This card has no open decision to park; its recorded decision stays unchanged.")
    if stage == "Interrupted":
        approve = park = (False, "Interrupted run: re-queue it with a recovery note first.")
    unpark = (True, "") if parked is not None else (False, "This card is not parked.")
    return {"approve": {"ok": approve[0], "why": approve[1]}, "park": {"ok": park[0], "why": park[1]},
            "requeue_interrupted": {"ok": stage == "Interrupted", "why": "" if stage == "Interrupted" else "Requires an interrupted run."},
            "unpark": {"ok": unpark[0], "why": unpark[1]},
            "answer": {"ok": bool(open_question),
                       "why": "" if open_question else "No open question: a message here is an annotation."}}


def next_posture(wb):
    """The adapter's current grant as the daemon will invoke it (None when it cannot be stated honestly)."""
    try:
        import depot
        adapter = depot.ADAPTERS.get(wb.get("adapter"))
    except Exception:
        return None
    if adapter is None:
        return None
    return {"adapter": adapter.name, "model": wb.get("model"), "sandbox": adapter.sandbox,
            "tools": list(getattr(adapter, "allowed_tools", ())) or None}


def contained_file(base, relative):
    """Resolve only files contained in this record/work directory."""
    if not isinstance(relative, str):
        return None
    target = (base / relative).resolve()
    return target if target.is_relative_to(base.resolve()) and target.is_file() else None


def blocked_details(root, name, wb, events):
    # Only the current dispatch supplies evidence; never use an older run's artifacts.
    start = max((i for i, e in enumerate(events) if e["type"] == "dispatched"), default=0)
    run_events = events[start:]
    verified = next((e["data"] for e in reversed(run_events) if e["type"] == "verified"), {})
    rid = events[start].get("data", {}).get("run_id")
    item = Path(root) / "items" / name
    run = item / "runs" / str(rid)
    valid_run = bool(rid and run.resolve().is_relative_to((item / "runs").resolve()))
    record = {}
    if valid_run and contained_file(run, "verify.json"):
        try:
            candidate = json.loads((run / "verify.json").read_text())
            record = candidate if isinstance(candidate, dict) else {}
        except (ValueError, OSError):
            pass
    discrepancies = verified.get("discrepancies", record.get("discrepancies", []))
    missing = {d["path"] for d in discrepancies if d.get("kind") == "missing"}
    extras = {d["path"] for d in discrepancies if d.get("kind") == "extra"}
    expected = set(record.get("expected_files", wb.get("expected_files", [])))
    work = item / work_directory(events)
    if not work.resolve().is_relative_to((item / "work").resolve()):
        raise FloorError("work directory escapes item workspace")
    nested = work / "repo"
    cause, explanation = "unknown", "The available evidence does not establish a cause. Review the run records."
    if (expected and missing == expected and extras and "repo/" in extras
            and all(contained_file(nested, f) for f in expected)):
        cause, explanation = "wrong-directory", "Verification checked work/, but all expected files are present one level down in work/repo/. The work may be sound; review that repository."
    elif extras and not missing and all(d.get("kind") == "extra" for d in discrepancies):
        cause, explanation = "extra-files-only", "The run changed files outside the expected file set. Review whether those additions belong."
    elif missing and not extras and all(d.get("kind") == "missing" for d in discrepancies):
        cause, explanation = "missing-files-only", "Expected files were absent from the verified changes. Review whether the run omitted work or used a different directory."
    elif verified.get("result") == "tests_failed":
        cause, explanation = "tests-failed", "The verification test command failed. Review its output in verify.json."
    recovery = None
    for repo in (nested, work):
        if not (repo / ".git").exists() or not repo.resolve().is_relative_to(work.resolve()):
            continue
        def git_value(*args):
            try:
                r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=3,
                                   env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
                return r.stdout.strip() if r.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired):
                return None
        commit = git_value("rev-parse", "--verify", "HEAD")
        branch = git_value("symbolic-ref", "--quiet", "--short", "HEAD")
        if commit:
            recovery = {"branch": branch, "commit": commit, "directory": str(repo.relative_to(item))}
            break
    links = {f: f"/records/{name}/{rid}/{f}" for f in ("verify.json", "report.md")
             if valid_run and contained_file(run, f)}
    return {"run_id": rid or "", "result": verified.get("result"), "discrepancies": discrepancies,
            "cause": cause, "explanation": explanation, "recovery": recovery, "links": links}


# ---- one card ----

def derive(item_name, wb, events, gate_done, now, root=None):
    if not events:
        raise FloorError(f"{item_name}: empty trail")
    if events[0]["type"] != "item_created":
        raise FloorError(f"{item_name}: trail does not start with item_created ({events[0]['type']})")
    kind = wb.get("kind", "build")
    if kind not in ("build", "gate"):
        raise FloorError(f"{item_name}: unknown kind {kind}")
    timeout, quiet = budgets(root, wb) if root is not None else (
        wb.get("timeout_s", DEFAULT_TIMEOUT_S), wb.get("quiet_after_s", DEFAULT_QUIET_AFTER_S))

    st = {"stage": None, "hands": "—", "since": None}
    blocked = parked = None  # only parked/unparked events change the hold below
    blocked_since = parked_since = None
    seen = set()
    dispatched_ts = last_hb = None
    hb_data = {}
    live = False        # a run has been dispatched and has not finished
    adapter = wb.get("adapter")

    def move(stage, hands, ts):
        if st["stage"] != stage:
            st["since"] = ts
        st["stage"], st["hands"] = stage, hands

    def note(ev):
        text = ev.get("data", {}).get("text")
        return {"type": ev["type"], "author": ev.get("actor"), "ts": ev["ts"], "text": clip(text, ANNOTATION_LIMIT)}

    def need(*types):
        if not any(t in seen for t in types):
            raise FloorError(f"{item_name}: {e['type']} at {e['ts']} with no earlier {'/'.join(types)}")

    awaiting_ts = None  # set while the newest non-heartbeat event is an unanswered input_required
    question = None     # the question on the card while it waits on operator
    asked = False       # the last run ended by asking: nothing is running, so it cannot be stalled
    annotation = None   # the newest message, or the text riding on the newest reopened event
    attachments = {}    # file name -> {file, bytes, sha256, ts, state}: landed attachments, until removed
    last_dispatch = None
    rejected_review = None
    for e in events:
        t, d, ts = e["type"], e.get("data", {}), e["ts"]
        if t in ("item_created", "brief_approved", "dispatched", "reopened", "verified"):
            rejected_review = None
        before_blocked, before_parked = blocked, parked
        if t not in KNOWN:
            raise FloorError(f"{item_name}: unknown event type {t}")
        if awaiting_ts and t not in ("heartbeat", "input_required", "parked", "unparked", "attached", "attachment_removed"):
            st["since"], awaiting_ts = ts, None  # superseded: the card left Awaiting operator at this event
        if t == "item_created":
            move("Queued", "—", ts)
            blocked = None
        elif t == "brief_approved":
            move("Briefed", "Turntable", ts)
            blocked = None
        elif t == "reopened":
            need("reviewed")  # only a Done card can be reopened
            move("Queued", "—", ts)
            blocked = None
            asked = False
            annotation = note(e)
        elif t == "message":
            annotation = note(e)  # an annotation only: no stage, hands or truth change (spec §9)
        elif t == "blocked":
            blocked = first_line(d.get("reason")) or "no reason given"
        elif t == "dispatched":
            need("brief_approved")
            adapter = d.get("adapter") or adapter
            move("Gating" if kind == "gate" else "Building", hands_name(adapter, kind == "gate"), ts)
            blocked = None
            dispatched_ts, last_hb, asked, hb_data, live = ts, None, False, {}, True
            last_dispatch = {"adapter": d.get("adapter"), "model": d.get("model"), "sandbox": d.get("sandbox"),
                             "run_id": d.get("run_id"), "ts": ts}
            for a in attachments.values():
                a["state"] = "delivered"  # a dispatch since the attachment landed carried it into the workspace
        elif t == "run_interrupted":
            need("dispatched")
            live, asked = False, False
            blocked = parked = None
            move("Interrupted", "operator", ts)
        elif t == "interrupted_requeued":
            need("run_interrupted")
            move("Queued", "operator", ts)
            blocked = parked = None
            asked = False
        elif t == "heartbeat":
            need("dispatched")
            last_hb, hb_data = ts, d
        elif t in ("run_finished", "claimed"):
            need("dispatched")
            if t == "run_finished":
                live = False
            move("Building", "Turntable", ts)
        elif t == "input_required":
            need("dispatched")
            awaiting_ts, question, asked = ts, d.get("question"), True
        elif t == "verified":
            need("run_finished")
            if d.get("result") == "pass":
                move("Review", "reviewer", ts)
                blocked = None
            else:
                st["hands"] = "Turntable"
                blocked = f"verification: {d.get('result')}" + (
                    f" ({', '.join(x.get('path', '?') for x in d.get('discrepancies', [])[:3])})"
                    if d.get("discrepancies") else "")
        elif t in ("adjudicated", "recovered"):
            move("Review", "reviewer", ts)
            blocked = None
        elif t == "committed":
            need("verified")
        elif t == "pushed":
            need("verified")
            if blocked and blocked.startswith("push failed"):
                blocked = None
        elif t == "push_failed":
            need("verified")
            st["hands"] = "Turntable"
            blocked = f"push failed: {first_line(d.get('error'))}"
        elif t == "reviewed":
            if d.get("result") == "pass":
                move("Done", "—", ts)
                blocked = None
            else:
                move("Review", "operator" if e.get("actor") == "operator" else "reviewer", ts)
                review_note = d.get("note") or d.get("reason")
                rejected_review = {"note": review_note, "commit": d.get("commit"),
                                   "run_id": last_dispatch["run_id"] if last_dispatch else ""}
                blocked = f"review: {d.get('result')}" + (f" · {first_line(review_note)}" if review_note else "")
        elif t == "gate_result":
            if d.get("result") != "pass":
                blocked = f"gate {d.get('gate', 'unknown')} returned {d.get('result')}"
        elif t == "parked":
            parked = first_line(d.get("reason")) or "no reason given"
        elif t == "unparked":
            parked = None  # the hold is released; no stage change and nothing dispatched
        elif t == "attached":
            attachments[d.get("file")] = {"file": d.get("file"), "bytes": d.get("bytes"), "sha256": d.get("sha256"),
                                          "ts": ts, "state": "attached"}
        elif t == "attachment_removed":
            attachments.pop(d.get("file"), None)
        if blocked is None:
            blocked_since = None
        elif (blocked != before_blocked or t in ("blocked", "push_failed")
              or (t in ("verified", "reviewed", "gate_result") and d.get("result") != "pass")):
            blocked_since = ts
        if parked is None:
            parked_since = None
        elif parked != before_parked or t == "parked":
            parked_since = ts
        seen.add(t)

    if awaiting_ts:
        st["stage"], st["hands"], st["since"] = "Awaiting operator", "operator", awaiting_ts

    newest = [e for e in events if e["type"] != "message"][-1]  # a message annotates; it is never the newest fact
    if kind == "gate" and gate_done is not None:
        st["stage"], st["hands"], st["since"] = "Done", "—", gate_done["ts"]
        blocked = None
        newest = gate_done

    # Supervision (spec §7): the age of the last ADAPTER event, carried by the newest heartbeat, plus how long
    # ago that heartbeat was written. With no heartbeat yet, the dispatch itself is the last thing heard.
    quiet_age = elapsed = None
    if live and st["stage"] in ("Building", "Gating") and dispatched_ts is not None:
        since_dispatch = (now - parse_ts(dispatched_ts)).total_seconds()
        if last_hb is not None:
            hb_age = (now - parse_ts(last_hb)).total_seconds()
            quiet_age = hb_age + float(hb_data.get("since_last_adapter_event_s") or 0)
            elapsed = hb_age + float(hb_data["elapsed_s"]) if hb_data.get("elapsed_s") is not None else since_dispatch
        else:
            quiet_age = elapsed = since_dispatch
        quiet_age, elapsed = max(0, int(quiet_age)), max(0, int(elapsed))
    stalled = None
    if quiet_age is not None and not blocked and not asked and quiet_age > quiet:
        stalled = (f"no adapter event for {exact(quiet_age)} · elapsed {exact(elapsed)} of {exact(timeout)} budget"
                   f" · quiet limit {exact(quiet)}")

    if st["stage"] == "Interrupted":
        condition, reason = "interrupted", "Run completion was not recorded. Re-queue with a recovery note; approval is a separate action."
    elif blocked:
        condition, reason = "blocked", blocked
    elif stalled:
        condition, reason = "stalled?", stalled
    elif parked is not None:
        condition, reason = "parked", parked
    else:
        condition, reason = None, None

    condition_age = None
    if condition == "blocked":
        condition_age = max(0, int((now - parse_ts(blocked_since)).total_seconds()))
    elif condition == "parked":
        condition_age = max(0, int((now - parse_ts(parked_since)).total_seconds()))
    elif condition == "stalled?":
        condition_age = quiet_age  # time since the adapter spoke, not time past the quiet limit
    elif condition == "interrupted":
        condition_age = max(0, int((now - parse_ts(st["since"])).total_seconds()))
    newest_fact = fact(newest)
    trail = [fact(e) for e in events if e["type"] != "heartbeat"]
    open_question = bool(st["stage"] == "Awaiting operator" and question)
    details = blocked_details(root, item_name, wb, events) if blocked and not rejected_review and root is not None else None
    recovery = recovered_work(events)
    actions = allowed_actions(st["stage"], parked, live, blocked, asked, open_question,
                              recovered=bool(recovery and not recovery["superseded_by"]),
                              review_failed=bool(rejected_review))
    commit = review_commit(events)
    actions["review"] = {"ok": bool(st["stage"] == "Review" and not blocked and not live and commit),
                         "why": "Requires Review with a passing verification, a recorded commit, and no block or live run."}
    actions["adjudicate"] = {"ok": bool(blocked and not live and st["stage"] != "Interrupted"), "why": "Requires a blocked item with no live run."}
    actions["recover"] = {"ok": bool(details and details["recovery"] and not live and st["stage"] != "Interrupted"), "why": "Requires a commit in the work repository and no live run."}
    contract = None
    if root is not None:
        import depot
        try:
            contract = depot.task_contract((Path(root) / "items" / item_name / "brief.md").read_text(), wb)
            contract["fingerprint"] = depot.contract_fingerprint(contract)
        except (OSError, depot.DepotError):
            pass  # an unavailable contract disables approval in the panel
    if contract and st["stage"] == "Briefed" and not live and parked is None:
        approval = next((e for e in reversed(events) if e["type"] == "brief_approved"), None)
        if approval and approval["data"].get("contract_fingerprint") != contract["fingerprint"]:
            actions["approve"] = {"ok": True, "why": ""}
    return {
        "rejected_review": rejected_review,
        "review_commit": commit,
        "recovered_work": recovery,
        "contract": contract,
        "blocked_details": details,
        "id": wb.get("id", item_name.split("-", 1)[0]),
        "slug": wb.get("slug", item_name.split("-", 1)[-1]),
        "name": item_name,
        "title": wb.get("title", item_name),
        "track": wb.get("track", ""),
        "kind": kind,
        "stage": st["stage"],
        "hands": st["hands"],
        "stage_since": st["since"],
        "dwell_s": max(0, int((now - parse_ts(st["since"])).total_seconds())),
        "last_event": {k: newest_fact[k] for k in ("type", "actor", "ts", "text")},
        "truth": newest_fact["truth"],
        "checker": newest_fact["checker"],
        "condition": condition,
        "condition_reason": reason,
        "condition_age_s": condition_age,
        "last_adapter_event_age_s": quiet_age,
        "elapsed_s": elapsed,
        "timeout_s": timeout,
        "heartbeats": sum(1 for e in events if e["type"] == "heartbeat"),
        "annotation": annotation,
        "question": clip(question, QUESTION_LIMIT) if st["stage"] == "Awaiting operator" and question else None,
        "trail": trail,
        "parked": parked,
        "open_question": open_question,
        "question_text": question if open_question else None,  # verbatim, for the panel (the card text is clipped)
        "attachments": sorted(attachments.values(), key=lambda a: (a["ts"], a["file"] or "")),
        "posture": {"live": {**last_dispatch, "running": live} if last_dispatch else None,
                    "next": next_posture(wb)},
        "actions": actions,
    }


def card_for(root, name, now):
    """One card's projection, read from just its own files (a gate item also needs the yard's gate results)."""
    d = Path(root) / "items" / name
    wb = json.loads((d / "waybill.json").read_text())
    events = read_events(d / "events.jsonl")
    gate_done = None
    if wb.get("kind") == "gate":
        gate_done = gate_completions(load_yard(root), read_events(Path(root) / "log" / "events.jsonl")).get(name)
    return derive(name, wb, events, gate_done, as_dt(now), root)


# Track state is a second projection input. No item event or card derivation is reinterpreted.
TRACK_EVENTS = {"track_paused": "paused", "track_resumed": "active", "track_tabled": "tabled",
                "track_untabled": "active", "track_ended": "ended", "track_reopened": "active"}
TRACK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,59}$")


def track_log(root, track):
    if not isinstance(track, str) or not TRACK_NAME_RE.fullmatch(track):
        raise FloorError("not a usable track name")
    base = Path(root) / "tracks"
    path = base / track / "events.jsonl"
    if base.is_symlink() or path.parent.is_symlink() or path.is_symlink():
        raise FloorError("track logs must not be symlinks")
    return path


def track_state(root, track):
    state = "active"
    for e in read_events(track_log(root, track)):
        if e.get("track") != track or e.get("type") not in TRACK_EVENTS:
            raise FloorError(f"{track}: invalid lifecycle event")
        state = TRACK_EVENTS[e["type"]]
    return state


def track_names(root):
    names = set()
    config = Path(root) / "tracks.json"
    try:
        data = json.loads(config.read_text()) if config.exists() else {}
    except (OSError, ValueError):
        data = {}  # match the floor's existing corrupt-budget fallback
    if isinstance(data, dict):
        names.update(k for k in data if k != "_default")
    base = Path(root) / "tracks"
    if base.exists():
        names.update(p.parent.name for p in base.glob("*/events.jsonl"))
    return names


def project(root, now):
    """The whole floor as plain data: a pure function of the yard's files and `now`."""
    now = as_dt(now)
    yard = load_yard(root)
    gate_done = gate_completions(yard, read_events(Path(root) / "log" / "events.jsonl"))
    cards = [derive(name, wb, events, gate_done.get(name), now, root) for name, wb, events in yard]
    receipts = receipts_by_item(root, yard)
    for c in cards:
        c["receipts"] = receipts.get(c["name"], [])[-RECEIPT_LIMIT:]
    board_receipts = receipts.get(None, [])[-RECEIPT_LIMIT:]  # board-level intents (create) bind to no card
    cards.sort(key=lambda c: (c["track"], c["id"], c["name"]))
    tracks = sorted({c["track"] for c in cards} | track_names(root))
    lanes = []
    for t in tracks:
        state = track_state(root, t)
        mine = [c for c in cards if c["track"] == t]
        lanes.append({"id": t, "name": t, "state": state, "done_count": sum(c["stage"] == "Done" for c in mine),
                      "card_count": len(mine), "open_count": sum(c["stage"] != "Done" for c in mine),
                      "pipeline": [c["name"] for c in mine if c["stage"] in ("Building", "Gating", "Review", "Interrupted")],
                      "shelf": state == "tabled", "archive": state == "ended",
                      "receipts": receipts.get("track:" + t, [])[-RECEIPT_LIMIT:]})
    return {"now": iso_z(now), "stages": STAGES,
            "tracks": lanes, "cards": cards, "board_receipts": board_receipts}


def cards_json(root, now):
    return json.dumps(project(root, now), sort_keys=True, ensure_ascii=False).encode("utf-8")


# ---- intents: file formats shared by the floor (intake) and the poll daemon (processing) ----

class IntentInvalid(Exception):
    """An intent whose shape is wrong. The floor answers 400; the daemon records a refusal."""


def intents_dir(root):
    return Path(root) / "log" / "intents"


def intent_path(root, intent_id):
    return intents_dir(root) / f"{intent_id}.json"


def outcome_path(root, intent_id):
    return intents_dir(root) / f"{intent_id}.outcome.json"


def safe_basename(name):
    """A filename reduced to a harmless basename: no directories, no traversal, no hidden-file dot, a short safe
    alphabet. None when nothing usable is left."""
    if not isinstance(name, str):
        return None
    base = name.replace("\\", "/").split("/")[-1].strip()
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")
    return base[:120] or None


def validate_create_data(data):
    """Shape only for a board-level create (S10): types and sizes. Whether a title is blank, a path is relative, an
    adapter exists or a branch is protected is the daemon's call, and it names every failure."""
    if not isinstance(data, dict):
        raise IntentInvalid("data must be an object")
    allowed = set(CREATE_STRING_FIELDS) | set(CREATE_OPTIONAL_STRINGS) | set(CREATE_LISTS) | {"timeout_s", "quiet_after_s"}
    extra = sorted(set(data) - allowed)
    if extra:
        raise IntentInvalid(f"create: unexpected data field(s): {', '.join(extra)}")
    clean = {}
    for key, limit in CREATE_STRING_FIELDS.items():
        if key not in data:
            raise IntentInvalid(f"create: data.{key} is required")
        if not isinstance(data[key], str):
            raise IntentInvalid(f"create: data.{key} must be a string")
        if len(data[key]) > limit:
            raise IntentInvalid(f"create: data.{key} is over {limit} characters")
        clean[key] = data[key]
    for key, limit in CREATE_OPTIONAL_STRINGS.items():
        if data.get(key) is not None:
            if not isinstance(data[key], str):
                raise IntentInvalid(f"create: data.{key} must be a string")
            if len(data[key]) > limit:
                raise IntentInvalid(f"create: data.{key} is over {limit} characters")
            clean[key] = data[key]
    for key, limit in CREATE_LISTS.items():
        if key not in data and key != "expected_files":
            continue
        if key not in data:
            raise IntentInvalid(f"create: data.{key} is required")
        v = data[key]
        if not isinstance(v, list) or not all(isinstance(x, str) and len(x) <= 500 for x in v) or len(v) > limit:
            raise IntentInvalid(f"create: data.{key} must be a list of at most {limit} strings of at most 500 characters")
        clean[key] = list(v)
    for key in ("timeout_s", "quiet_after_s"):
        if data.get(key) is not None:
            if isinstance(data[key], bool) or not isinstance(data[key], (int, float)):
                raise IntentInvalid(f"create: data.{key} must be a number")
            clean[key] = data[key]
    return clean


def validate_intent(obj):
    """Shape only, by kind; returns the normalized intent {id, ts, item, kind, data}. Whether the item exists, the
    card is in the right state or a reason is non-empty is the daemon's call, not the floor's."""
    if not isinstance(obj, dict):
        raise IntentInvalid("an intent must be a JSON object")
    kind = obj.get("kind")
    if kind not in INTENT_KINDS:
        raise IntentInvalid(f"unknown kind {kind!r} (known: {', '.join(INTENT_KINDS)})")
    if kind.startswith("track_"):
        if obj.get("item") is not None:
            raise IntentInvalid("track acts take no item")
        iid = obj.get("id")
        if iid is not None and (not isinstance(iid, str) or not INTENT_ID_RE.fullmatch(iid)):
            raise IntentInvalid("id must be a lowercase uuid")
        data = obj.get("data", {})
        if not isinstance(data, dict) or set(data) != {"track"}:
            raise IntentInvalid("track acts require only data.track")
        track = data["track"]
        if not isinstance(track, str) or not TRACK_NAME_RE.fullmatch(track):
            raise IntentInvalid("not a usable track name")
        return {"id": iid, "ts": obj.get("ts"), "item": None, "kind": kind, "data": {"track": track}}
    if kind == "create":  # board-level: it binds to no item
        if obj.get("item") is not None:
            raise IntentInvalid("create is board-level: it takes no item")
        iid = obj.get("id")
        if iid is not None and (not isinstance(iid, str) or not INTENT_ID_RE.match(iid)):
            raise IntentInvalid("id must be a lowercase uuid")
        return {"id": iid, "ts": obj.get("ts"), "item": None, "kind": "create",
                "data": validate_create_data(obj.get("data", {}))}
    item = obj.get("item")
    if not isinstance(item, str) or not ITEM_RE.match(item):
        raise IntentInvalid("item must be an item directory name like 0010-panel-question")
    iid = obj.get("id")
    if iid is not None and (not isinstance(iid, str) or not INTENT_ID_RE.match(iid)):
        raise IntentInvalid("id must be a lowercase uuid")
    ts = obj.get("ts")
    data = obj.get("data", {})
    if not isinstance(data, dict):
        raise IntentInvalid("data must be an object")
    fields = {"message": {"text"}, "approve": {"contract_fingerprint"}, "park": {"reason"}, "unpark": set(),
              "attach": {"name", "content_b64"}, "remove_attachment": {"name"},
              "adjudicate": {"note", "run_id"}, "recover": {"run_id", "commit"}, "review": {"result", "note", "commit"},
              "requeue_interrupted": {"note", "run_id"}}[kind]
    extra = sorted(set(data) - fields)
    if extra:
        raise IntentInvalid(f"{kind}: unexpected data field(s): {', '.join(extra)}")
    clean = {}
    for key in sorted(fields):
        if kind == "approve" and key not in data:
            continue  # legacy intents can be queued, but the daemon refuses unbound approval
        if key not in data:
            raise IntentInvalid(f"{kind}: data.{key} is required")
        v = data[key]
        if not isinstance(v, str):
            raise IntentInvalid(f"{kind}: data.{key} must be a string")
        clean[key] = v
    for key in ("text", "reason", "note", "run_id", "commit"):
        if key in clean and len(clean[key]) > MAX_TEXT_CHARS:
            raise IntentInvalid(f"{kind}: data.{key} is over {MAX_TEXT_CHARS} characters")
    if kind == "review" and clean["result"] not in ("pass", "fail"):
        raise IntentInvalid("review: result must be pass or fail")
    if "name" in clean:
        safe = safe_basename(clean["name"])
        if safe is None:
            raise IntentInvalid(f"{kind}: data.name has no usable filename")
        clean["name"] = safe
    if "content_b64" in clean:
        if len(clean["content_b64"]) > (MAX_ATTACHMENT_BYTES * 4) // 3 + 8:
            raise IntentInvalid(f"attach: file is over the {MAX_ATTACHMENT_BYTES}-byte limit")
        try:
            raw = base64.b64decode(clean["content_b64"], validate=True)
        except (binascii.Error, ValueError):
            raise IntentInvalid("attach: data.content_b64 is not valid base64")
        if len(raw) > MAX_ATTACHMENT_BYTES:
            raise IntentInvalid(f"attach: file is over the {MAX_ATTACHMENT_BYTES}-byte limit")
    return {"id": iid, "ts": ts, "item": item, "kind": kind, "data": clean}


def write_atomic(path, text):
    tmp = Path(str(path) + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_outcome(root, intent_id):
    p = outcome_path(root, intent_id)
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def drop_intent(root, obj, now_fn=None):
    """Write one validated intent file under log/intents/. Returns (status, body):
    202 new, 200 the same intent re-presented (or retried after a failure), 409 same id with different content."""
    intent = validate_intent(obj)
    intent["id"] = intent["id"] or str(uuid.uuid4())
    intent["ts"] = iso_z((now_fn or (lambda: datetime.now(timezone.utc)))())
    d = intents_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    path = intent_path(root, intent["id"])
    if path.exists():
        try:
            old = json.loads(path.read_text())
        except (OSError, ValueError):
            old = None
        if not old or (old.get("item"), old.get("kind"), old.get("data")) != (intent["item"], intent["kind"], intent["data"]):
            return 409, {"error": f"intent {intent['id']} already exists with different content"}
        outcome = read_outcome(root, intent["id"])
        if outcome and outcome.get("state") == "failed":  # the explicit retry: same id, same payload
            try:
                outcome_path(root, intent["id"]).unlink()
            except OSError:
                pass
            return 200, {"id": intent["id"], "retried": True}
        return 200, {"id": intent["id"], "duplicate": True}
    tmp = Path(str(path) + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(intent, sort_keys=True), encoding="utf-8")
    try:
        os.link(tmp, path)  # publish atomically, and never over an existing intent
    except FileExistsError:
        tmp.unlink(missing_ok=True)
        return 200, {"id": intent["id"], "duplicate": True}
    tmp.unlink(missing_ok=True)
    return 202, {"id": intent["id"]}


def summarize_intent(intent):
    d = intent.get("data", {})
    kind = intent.get("kind")
    if kind and kind.startswith("track_"):
        return d.get("track") or ""
    if kind == "review":
        decision = {"pass": "Accept", "fail": "Reject"}.get(d.get("result"), "Review")
        return " · ".join(filter(None, [decision, clip(d.get("note"), 160), (d.get("commit") or "")[:7]]))
    if kind in ("recover", "adjudicate", "requeue_interrupted"):
        label = {"recover": "Recover", "adjudicate": "Adjudicate", "requeue_interrupted": "Re-queue"}[kind]
        return " · ".join(filter(None, [label, clip(d.get("note"), 160),
                                       (d.get("commit") or "")[:7], d.get("run_id")]))
    if kind == "message":
        return clip(d.get("text"), 160)
    if kind == "park":
        return clip(d.get("reason"), 160)
    if kind in ("attach", "remove_attachment"):
        return d.get("name") or ""
    if kind == "create":
        return clip(d.get("title"), 160)
    return ""


def receipts_by_item(root, yard):
    """item name -> receipts, oldest first. A receipt's state comes from the outcome file and the log, never from
    the click: no outcome and no event is `pending`; no outcome but an event carrying the intent id is
    `reconciling` (the daemon committed the event and has not yet written the outcome)."""
    d = intents_dir(root)
    if not d.is_dir():
        return {}
    in_log = {name: {e.get("data", {}).get("intent") for e in events if e.get("data", {}).get("intent")}
              for name, _wb, events in yard}
    for track in track_names(root) | {wb.get("track", "") for _, wb, _ in yard}:
        in_log["track:" + track] = {e.get("data", {}).get("intent") for e in read_events(track_log(root, track))}
    out = {}
    for p in sorted(d.glob("*.json")):
        if p.name.endswith(".outcome.json"):
            continue
        try:
            intent = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        key = ("track:" + intent.get("data", {}).get("track", "")) if str(intent.get("kind", "")).startswith("track_") else intent.get("item")
        iid = intent.get("id")
        outcome = read_outcome(root, iid) or {}
        state = outcome.get("state")
        if state not in ("landed", "refused", "failed"):
            seen = in_log.get(key, set()) if key else set().union(*in_log.values())
            state = "reconciling" if iid in seen else "pending"
        out.setdefault(key, []).append({
            "id": iid, "ts": intent.get("ts"), "kind": intent.get("kind"), "summary": summarize_intent(intent),
            "state": state, "reason": outcome.get("reason"), "event": outcome.get("event"),
            "filed": outcome.get("item")})
    for lst in out.values():
        lst.sort(key=lambda r: (r["ts"] or "", r["id"] or ""))
    return out


# ---- server: GET, plus one POST route (/intents) that only drops a file; localhost only ----

class FloorHandler(BaseHTTPRequestHandler):
    server_version = "DepotFloor"
    protocol_version = "HTTP/1.0"

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._same_origin():
            return self._json(403, {"error": "cross-origin or foreign-host request refused"})
        path = self.path.split("?", 1)[0]
        try:
            if path == "/":
                self._send(200, (PAGE_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/floor.js":
                self._send(200, (PAGE_DIR / "floor.js").read_bytes(), "text/javascript; charset=utf-8")
            elif path.startswith("/records/"):
                parts = path.split("/")
                if (len(parts) == 5 and ITEM_RE.fullmatch(parts[2]) and
                        re.fullmatch(r"[A-Za-z0-9_-]+", parts[3]) and parts[4] in ("verify.json", "report.md")):
                    base = self.server.yard / "items" / parts[2] / "runs"
                    target = contained_file(base, parts[3] + "/" + parts[4])
                    if target:
                        return self._send(200, target.read_bytes(), "text/plain; charset=utf-8")
                self._send(404, b"not found\n", "text/plain; charset=utf-8")
            elif path == "/cards.json":
                try:
                    body = cards_json(self.server.yard, self.server.now_fn())
                    self._send(200, body, "application/json; charset=utf-8")
                except (FloorError, ValueError, KeyError, OSError) as e:
                    err = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode()
                    self._send(500, err, "application/json; charset=utf-8")
            else:
                self._send(404, b"not found\n", "text/plain; charset=utf-8")
        except OSError as e:
            self._send(500, f"cannot read page files: {e}\n".encode(), "text/plain; charset=utf-8")

    def _refuse(self):
        if not self._same_origin():
            return self._json(403, {"error": "cross-origin or foreign-host request refused"})
        self._send(405, b"the floor accepts GET, and POST /intents only\n", "text/plain; charset=utf-8",
                   {"Allow": "GET, POST"})

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, sort_keys=True).encode("utf-8"), "application/json; charset=utf-8")

    def _same_origin(self):
        """CSRF / DNS-rebinding guard for every handler. It is not authentication: anything on this machine
        can still talk to the port. A page in the browser cannot, because it cannot forge Host or Origin."""
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in allowed:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in allowed}

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/intents":
            return self._refuse()
        if not self._same_origin():
            return self._json(403, {"error": "cross-origin or foreign-host request refused"})
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self._json(415, {"error": "Content-Type must be application/json"})
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            return self._json(411, {"error": "Content-Length is required"})
        if int(length) > MAX_REQUEST_BYTES:
            return self._json(413, {"error": f"request is over the {MAX_REQUEST_BYTES}-byte limit"})
        raw = self.rfile.read(int(length))
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "body is not valid JSON"})
        try:
            code, body = drop_intent(self.server.yard, obj, self.server.now_fn)
        except IntentInvalid as e:
            return self._json(400, {"error": str(e)})
        except OSError as e:
            return self._json(500, {"error": f"cannot write the intent: {e}"})
        self._json(code, body)

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _refuse


def make_server(yard, port=0, now_fn=None):
    """A server bound to 127.0.0.1 only. `now_fn` lets tests pin the clock."""
    srv = ThreadingHTTPServer(("127.0.0.1", port), FloorHandler)
    srv.yard = Path(yard)
    srv.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    return srv


def serve(yard, port):
    srv = make_server(yard, port)
    host, bound = srv.server_address[:2]
    print(f"floor: http://{host}:{bound}/  (yard {srv.yard}; GET, and POST /intents to drop intents; ctrl-c to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
