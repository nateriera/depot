import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import depot

DEPOT = str(Path(__file__).resolve().parent / "depot.py")
sys.path.insert(0, str(Path(DEPOT).parent))
import depot

FAKE_CODEX = textwrap.dedent('''\
    #!{py}
    import json, os, sys, time
    a = sys.argv[1:]
    work = a[a.index("-C") + 1]
    out = a[a.index("-o") + 1]
    stdin = sys.stdin.read()
    run = os.path.dirname(out)
    open(os.path.join(run, "stdin.txt"), "w").write(stdin)
    open(os.path.join(run, "args.json"), "w").write(json.dumps(a))
    mode = os.environ.get("FAKE_MODE", "ok")
    print(json.dumps({{"type": "thread.started", "thread_id": "T1"}}), flush=True)
    if mode in ("ok", "stowaway", "question", "silent"):
        if mode == "silent":
            time.sleep(3)
        if mode != "question":
            open(os.path.join(work, "sentinel.py"), "w").write("X = 1\\n")
            open(os.path.join(work, "test_sentinel.py"), "w").write(
                "import unittest\\nclass T(unittest.TestCase):\\n    def test_a(self):\\n        self.assertTrue(True)\\n")
        if mode == "stowaway":
            os.makedirs(os.path.join(work, "__pycache__"))
            open(os.path.join(work, "__pycache__", "x.pyc"), "w").write("x")
        report = "Which file name do you want?" if mode == "question" else "done"
        open(out, "w").write(report)
        print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 7}}}}), flush=True)
    elif mode == "sleep":
        import subprocess
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open(os.path.join(run, "child.pid"), "w").write(str(child.pid))
        time.sleep(60)
    elif mode == "fail":
        sys.exit(5)
''')

BRIEF = textwrap.dedent('''\
    ```depot
    {{"expected_files": ["sentinel.py", "test_sentinel.py"], "test_command": "{py} -m unittest"}}
    ```

    TASK: build the sentinel.
    ''')


class TurntableCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)
        self.yard = self.tmp / "yard"
        (self.yard / "log").mkdir(parents=True)
        fake = self.tmp / "fake-codex"
        fake.write_text(FAKE_CODEX.format(py=sys.executable))
        fake.chmod(0o755)
        self.env = {**os.environ, "DEPOT_YARD": str(self.yard), "DEPOT_CODEX": str(fake),
                    "DEPOT_HEARTBEAT_S": "1"}

    def depot(self, *args, **env):
        return subprocess.run([sys.executable, DEPOT, *args], capture_output=True, text=True,
                              env={**self.env, **env})

    def new_item(self, slug="thing", brief=None, **waybill):
        r = self.depot("new-item", "--title", "T", "--slug", slug, "--track", "tr")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = self.yard / "items" / r.stdout.strip()
        (d / "brief.md").write_text(BRIEF.format(py=sys.executable) if brief is None else brief)
        if waybill:
            wb = json.loads((d / "waybill.json").read_text())
            wb.update(waybill)
            (d / "waybill.json").write_text(json.dumps(wb))
        return d

    def types(self, d):
        return [json.loads(l)["type"] for l in (d / "events.jsonl").read_text().splitlines()]

    def run_dir(self, d):
        runs = list((d / "runs").iterdir())
        self.assertEqual(len(runs), 1)
        return runs[0]


class TestVerification(TurntableCase):
    def setUp(self):
        super().setUp()
        self.item_dir = self.new_item()
        self.work = self.item_dir / "work"
        self.work.mkdir()
        depot.git(self.work, "init", "-q")
        depot.git(self.work, "config", "user.name", "Test")
        depot.git(self.work, "config", "user.email", "test@example.invalid")
        depot.git(self.work, "config", "core.autocrlf", "false")
        (self.work / "main.py").write_text("old\n")
        depot.git(self.work, "add", ".")
        depot.git(self.work, "commit", "-qm", "base")
        self.base = depot.starting_point(self.work)
        self.run = self.item_dir / "runs" / "test"
        self.run.mkdir(parents=True)
        self.wb = {"expected_files": ["main.py"], "test_command": ""}

    def verify(self):
        with mock.patch.dict(os.environ, {"DEPOT_YARD": str(self.yard)}):
            return depot.verify_and_commit(depot.Item(self.item_dir), self.work, self.run,
                                           self.wb, start=self.base)[0]

    def test_live_index_cannot_smuggle_an_extra_file_or_different_bytes(self):
        (self.work / "main.py").write_text("staged wrong bytes\n")
        (self.work / "stowaway").write_text("extra\n")
        depot.git(self.work, "add", ".")
        (self.work / "stowaway").unlink()
        (self.work / "main.py").write_bytes(b"right\r\n\x00binary\xff")
        v = self.verify()
        self.assertEqual(v["result"], "pass")
        self.assertEqual(v["changed_files"], ["main.py"])
        self.assertEqual(depot.git(self.work, "ls-tree", "--name-only", v["commit"]).stdout, "main.py\n")
        self.assertEqual(depot.git(self.work, "show", v["commit"] + ":main.py", text=False).stdout,
                         b"right\r\n\x00binary\xff")
        with tempfile.TemporaryDirectory() as td:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(td) / "index")}
            depot.git(self.work, "read-tree", self.base[0], env=env)
            depot.git(self.work, "apply", "--cached", str(self.run / "diff.patch"), env=env)
            self.assertEqual(depot.git(self.work, "write-tree", env=env).stdout.strip(), v["tree"])

    def test_mutation_after_evidence_capture_fails_publication(self):
        (self.work / "main.py").write_text("candidate\n")
        publish = depot.host_commit

        def mutate(*args):
            (self.work / "main.py").write_text("late mutation\n")
            return publish(*args)

        with mock.patch.object(depot, "host_commit", side_effect=mutate):
            v = self.verify()
        self.assertEqual(v["result"], "commit_failed")
        self.assertIsNone(v["commit"])
        self.assertEqual(depot.starting_point(self.work), self.base)
        self.assertNotIn("+late mutation", (self.run / "diff.patch").read_text())

    def test_test_changes_starting_ref_fails_even_with_same_tree(self):
        (self.work / "main.py").write_text("candidate\n")
        # The sandboxed test command cannot write .git (the sandbox masks it), so the
        # ref move is simulated by mocking run_tests: the detection under test is the
        # starting-point comparison, not the test command's ability to move HEAD.
        real_run_tests = depot.run_tests

        def run_and_move_ref(work, test_command):
            result = real_run_tests(work, test_command)
            depot.git(self.work, "symbolic-ref", "HEAD", "refs/heads/other")
            return result

        with mock.patch.object(depot, "run_tests", side_effect=run_and_move_ref):
            v = self.verify()
        self.assertEqual(v["result"], "workspace_changed")
        self.assertIsNone(v["commit"])

    def test_push_publishes_recorded_commit_even_if_local_branch_moves(self):
        (self.work / "main.py").write_text("verified\n")
        v = self.verify()
        (self.work / "main.py").write_text("unverified later commit\n")
        depot.git(self.work, "add", ".")
        depot.git(self.work, "commit", "-qm", "later")
        remote = self.tmp / "remote.git"
        depot.git(self.tmp, "init", "--bare", "-q", str(remote))
        depot.git(self.work, "remote", "add", "origin", str(remote))
        branch = self.base[1].removeprefix("refs/heads/")
        with mock.patch.dict(os.environ, {"DEPOT_YARD": str(self.yard)}):
            self.assertTrue(depot.push_branch(depot.Item(self.item_dir), self.work,
                                             {"repo": str(remote), "branch": branch}, v["commit"]))
        self.assertEqual(depot.git(remote, "rev-parse", self.base[1]).stdout.strip(), v["commit"])

    def test_clean_deletion_commits_the_recorded_tree(self):
        (self.work / "main.py").unlink()
        v = self.verify()
        self.assertEqual(v["result"], "pass")
        self.assertEqual(v["base_commit"], self.base[0])
        self.assertEqual(v["changed_files"], ["main.py"])
        self.assertEqual(depot.git(self.work, "rev-parse", "HEAD^{tree}").stdout.strip(), v["tree"])
        self.assertEqual(depot.git(self.work, "ls-tree", "HEAD").stdout, "")
        self.assertIn("deleted file mode", (self.run / "diff.patch").read_text())


class TestItems(TurntableCase):
    def test_sequential_ids_and_files(self):
        a = self.new_item("one")
        b = self.new_item("two")
        self.assertEqual((a.name, b.name), ("0001-one", "0002-two"))
        wb = json.loads((a / "waybill.json").read_text())
        for k in ("id", "slug", "title", "track", "created", "brief", "adapter", "model", "timeout_s",
                  "quiet_after_s", "expected_files", "test_command", "status"):
            self.assertIn(k, wb)
        self.assertEqual((wb["adapter"], wb["status"]), ("codex", "queued"))

    def test_item_created_in_both_logs(self):
        d = self.new_item()
        self.assertEqual(self.types(d), ["item_created"])
        glog = [json.loads(l) for l in (self.yard / "log" / "events.jsonl").read_text().splitlines()]
        self.assertEqual([(e["item"], e["type"], e["actor"]) for e in glog],
                         [("0001-thing", "item_created", "operator")])

    def test_approve_refuses_missing_block(self):
        d = self.new_item(brief="no header here\n")
        r = self.depot("approve", d.name)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("brief_approved", self.types(d))

    def test_approve_refuses_invalid_json(self):
        d = self.new_item(brief="```depot\n{not json}\n```\ntext\n")
        self.assertNotEqual(self.depot("approve", d.name).returncode, 0)
        self.assertNotIn("brief_approved", self.types(d))

    def test_approve_records_operator_and_syncs_waybill(self):
        d = self.new_item()
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        ev = json.loads((d / "events.jsonl").read_text().splitlines()[-1])
        self.assertEqual((ev["type"], ev["actor"]), ("brief_approved", "operator"))
        wb = json.loads((d / "waybill.json").read_text())
        self.assertEqual(wb["expected_files"], ["sentinel.py", "test_sentinel.py"])
        self.assertEqual(wb["status"], "approved")

    def test_status_and_log(self):
        d = self.new_item()
        self.depot("approve", "0001")
        s = self.depot("status")
        self.assertIn("0001-thing", s.stdout)
        self.assertIn("approved", s.stdout)
        lg = self.depot("log", "0001")
        self.assertLess(lg.stdout.index("item_created"), lg.stdout.index("brief_approved"))


class TestDispatch(TurntableCase):
    def approved(self, slug="thing", **waybill):
        d = self.new_item(slug, **waybill)
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        return d

    def test_dispatch_refuses_unapproved(self):
        d = self.new_item()
        r = self.depot("dispatch", d.name)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing", r.stderr)
        self.assertNotIn("dispatched", self.types(d))
        self.assertFalse((d / "runs").exists())
        self.assertFalse((d / "work").exists())

    def test_one_approval_cannot_dispatch_twice(self):
        d = self.approved()
        first = self.depot("dispatch", d.name)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.depot("dispatch", d.name)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("approval already consumed", second.stderr)
        self.assertEqual(self.types(d).count("dispatched"), 1)

    def test_changed_contract_requires_fresh_approval(self):
        for field in ("brief", "expected_files", "test_command"):
            with self.subTest(field=field):
                d = self.approved(slug=field.replace("_", "-"))
                brief = (d / "brief.md").read_text()
                if field == "brief":
                    brief += "Revised task.\n"
                elif field == "expected_files":
                    brief = brief.replace('["sentinel.py", "test_sentinel.py"]', '["test_sentinel.py", "sentinel.py"]')
                else:
                    brief = brief.replace("-m unittest", "-m unittest discover")
                (d / "brief.md").write_text(brief)
                refused = self.depot("dispatch", d.name)
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("task contract changed", refused.stderr)
                self.assertNotIn("dispatched", self.types(d))
                self.assertFalse((d / "work").exists())
                self.assertEqual(self.depot("approve", d.name).returncode, 0)
                dispatched = self.depot("dispatch", d.name)
                self.assertEqual(dispatched.returncode, 0, dispatched.stderr)

    def test_old_unbound_approval_is_refused(self):
        d = self.new_item()
        with open(d / "events.jsonl", "a") as f:
            f.write(json.dumps({"type": "brief_approved", "data": {}}) + "\n")
        r = self.depot("dispatch", d.name)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no fingerprint", r.stderr)
        self.assertNotIn("dispatched", self.types(d))

    def test_pre_dispatch_refusal_does_not_consume_approval(self):
        d = self.approved(adapter="unknown")
        refused = self.depot("dispatch", d.name)
        self.assertNotEqual(refused.returncode, 0)
        self.assertNotIn("dispatched", self.types(d))
        wb = json.loads((d / "waybill.json").read_text())
        wb["adapter"] = "codex"
        (d / "waybill.json").write_text(json.dumps(wb))
        retried = self.depot("dispatch", d.name)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(self.types(d).count("brief_approved"), 1)
        self.assertEqual(self.types(d).count("dispatched"), 1)

    def test_started_failure_also_consumes_approval(self):
        d = self.approved()
        self.assertNotEqual(self.depot("dispatch", d.name, DEPOT_CODEX=str(self.tmp / "missing")).returncode, 0)
        r = self.depot("dispatch", d.name)
        self.assertIn("approval already consumed", r.stderr)
        self.assertEqual(self.types(d).count("dispatched"), 1)

    def test_overlapping_dispatches_cannot_share_approval(self):
        d = self.approved()
        proc = subprocess.Popen([sys.executable, DEPOT, "dispatch", d.name],
                                env={**self.env, "FAKE_MODE": "silent"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while "dispatched" not in self.types(d) and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIn("dispatched", self.types(d))
            second = self.depot("dispatch", d.name)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("another dispatch is active", second.stderr)
            out, err = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0, (out, err))
            self.assertEqual(self.types(d).count("dispatched"), 1)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_dispatch_happy_path(self):
        d = self.approved()
        r = self.depot("dispatch", d.name)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        order = [t for t in self.types(d) if t != "heartbeat"]
        self.assertEqual(order, ["item_created", "brief_approved", "dispatched", "run_finished",
                                 "claimed", "verified", "committed"])
        glog = [json.loads(l)["type"] for l in (self.yard / "log" / "events.jsonl").read_text().splitlines()]
        self.assertEqual([t for t in glog if t != "heartbeat"], order)
        run = self.run_dir(d)
        m = json.loads((run / "manifest.json").read_text())
        for k in ("run_id", "adapter", "model", "sandbox", "started", "elapsed_s", "exit_code",
                  "session_or_thread_id", "usage", "artifacts"):
            self.assertIn(k, m)
        self.assertEqual((m["exit_code"], m["session_or_thread_id"], m["usage"], m["sandbox"]),
                         (0, "T1", {"input_tokens": 7}, "workspace-write"))
        for f in ("events.jsonl", "report.md", "verify.json", "diff.patch", "manifest.json"):
            self.assertTrue((run / f).exists(), f)
        self.assertEqual((run / "report.md").read_text(), "done")
        v = json.loads((run / "verify.json").read_text())
        self.assertEqual((v["result"], v["discrepancies"], v["test"]["exit_code"]), ("pass", [], 0))
        self.assertIn("OK", v["test"]["tail"])

    def test_dispatch_command_and_stdin(self):
        d = self.approved(model="gpt-test")
        self.depot("dispatch", d.name)
        run = self.run_dir(d)
        args = json.loads((run / "args.json").read_text())
        self.assertEqual(args[:2], ["exec", "-s"])
        for flag in ("workspace-write", "--json", "-C", "-o", "--skip-git-repo-check", "-m", "gpt-test"):
            self.assertIn(flag, args)
        self.assertEqual(args[-1], "-")
        stdin = (run / "stdin.txt").read_text()
        self.assertIn("TASK: build the sentinel.", stdin)
        self.assertNotIn("```depot", stdin)

    def test_exactly_one_host_commit_with_expected_files(self):
        d = self.approved()
        self.depot("dispatch", d.name)
        work = d / "work"
        log = subprocess.run(["git", "log", "--format=%H"], cwd=work, capture_output=True, text=True).stdout.split()
        self.assertEqual(len(log), 1)
        files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=work,
                               capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(files), ["sentinel.py", "test_sentinel.py"])
        committed = [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines()][-1]
        self.assertEqual((committed["type"], committed["data"]["hash"]), ("committed", log[0]))

    def assert_recorded_tree(self, d):
        work, run = d / "work", self.run_dir(d)
        v = json.loads((run / "verify.json").read_text())
        self.assertEqual(v["result"], "pass")
        committed_tree = depot.git(work, "rev-parse", v["commit"] + "^{tree}").stdout.strip()
        self.assertEqual(committed_tree, v["tree"])
        self.assertEqual(v["candidate_tree"], v["tree"])
        # Reconstruct the entire recorded tree solely from the base plus diff.patch.
        with tempfile.TemporaryDirectory() as td:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(td) / "index")}
            depot.git(work, "read-tree", *([v["base_commit"]] if v["base_commit"] else ["--empty"]), env=env)
            depot.git(work, "apply", "--cached", "--binary", str(run / "diff.patch"), env=env)
            self.assertEqual(depot.git(work, "write-tree", env=env).stdout.strip(), committed_tree)
            for path in v["expected_files"]:
                recorded = depot.git(work, "show", ":" + path, env=env, text=False).stdout
                committed = depot.git(work, "show", v["commit"] + ":" + path, text=False).stdout
                self.assertEqual(recorded, committed)
        return v

    def test_clean_commit_matches_recorded_diff_bytes(self):
        d = self.approved()
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        self.assert_recorded_tree(d)

    def test_test_modifies_expected_file_fails_without_commit(self):
        # The modifying test command must be in the brief BEFORE approval, so the
        # approval's fingerprint binds it; a post-approval brief change is refused.
        d = self.new_item()
        brief = BRIEF.format(py=sys.executable)
        command = f"{sys.executable} -c \"from pathlib import Path; Path('sentinel.py').write_text('X = 2')\""
        (d / "brief.md").write_text(brief.replace(f"{sys.executable} -m unittest", command.replace('"', '\\"')))
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        r = self.depot("dispatch", d.name)
        self.assertEqual(r.returncode, 1, r.stderr)
        v = json.loads((self.run_dir(d) / "verify.json").read_text())
        self.assertEqual(v["result"], "workspace_changed")
        self.assertNotEqual(v["candidate_tree"], v["tree"])
        self.assertIsNone(v["commit"])
        self.assertNotIn("committed", self.types(d))
        self.assertIn("+X = 2", (self.run_dir(d) / "diff.patch").read_text())

    def test_test_creates_expected_file_is_recorded_and_fails(self):
        # The creating test command must be in the brief BEFORE approval, so the
        # approval's fingerprint binds it; a post-approval brief change is refused.
        d = self.new_item()
        header = {"expected_files": ["sentinel.py", "test_sentinel.py", "created.py"],
                  "test_command": f"{sys.executable} -c \"from pathlib import Path; Path('created.py').write_text('NEW = 1')\""}
        (d / "brief.md").write_text("```depot\n" + json.dumps(header) + "\n```\nTASK: build sentinel.\n")
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        self.assertEqual(self.depot("dispatch", d.name).returncode, 1)
        v = json.loads((self.run_dir(d) / "verify.json").read_text())
        self.assertEqual(v["result"], "workspace_changed")
        self.assertIn("created.py", v["changed_files"])
        self.assertEqual(v["discrepancies"], [])
        self.assertIsNone(v["commit"])
        self.assertIn("+NEW = 1", (self.run_dir(d) / "diff.patch").read_text())

    def test_stowaway_is_discrepancy_and_withholds_commit(self):
        d = self.approved()
        r = self.depot("dispatch", d.name, FAKE_MODE="stowaway")
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("committed", self.types(d))
        v = json.loads((self.run_dir(d) / "verify.json").read_text())
        self.assertEqual(v["result"], "discrepancies")
        self.assertEqual(v["discrepancies"], [{"path": "__pycache__/x.pyc", "kind": "extra"}])
        self.assertIsNone(v["commit"])
        head = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=d / "work", capture_output=True)
        self.assertNotEqual(head.returncode, 0)
        last = json.loads((d / "events.jsonl").read_text().splitlines()[-1])
        self.assertEqual((last["type"], last["data"]["result"]), ("verified", "discrepancies"))
        self.assertEqual(json.loads((d / "waybill.json").read_text())["status"], "blocked")

    def test_failing_tests_with_exact_files_is_tests_failed_and_withholds_commit(self):
        d = self.approved(slug="red")
        (d / "brief.md").write_text(BRIEF.format(py=sys.executable).replace(
            f"{sys.executable} -m unittest", f"{sys.executable} -c 'import sys; sys.exit(1)'"))
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        r = self.depot("dispatch", d.name)
        self.assertEqual(r.returncode, 1)
        v = json.loads((self.run_dir(d) / "verify.json").read_text())
        self.assertEqual(v["result"], "tests_failed")
        self.assertEqual(v["discrepancies"], [])
        self.assertEqual(v["test"]["exit_code"], 1)
        self.assertIsNone(v["commit"])
        self.assertNotIn("committed", self.types(d))
        last = json.loads((d / "events.jsonl").read_text().splitlines()[-1])
        self.assertEqual((last["type"], last["data"]["result"]), ("verified", "tests_failed"))
        head = subprocess.run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=d / "work", capture_output=True)
        self.assertNotEqual(head.returncode, 0)
        self.assertEqual(json.loads((d / "waybill.json").read_text())["status"], "blocked")

    def test_missing_expected_file_is_discrepancy(self):
        d = self.approved(slug="miss")
        (d / "brief.md").write_text(BRIEF.format(py=sys.executable).replace('"test_sentinel.py"', '"nope.py"'))
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        self.depot("dispatch", d.name)
        v = json.loads((self.run_dir(d) / "verify.json").read_text())
        self.assertIn({"path": "nope.py", "kind": "missing"}, v["discrepancies"])
        self.assertIn({"path": "test_sentinel.py", "kind": "extra"}, v["discrepancies"])

    def test_timeout_kills_whole_tree(self):
        d = self.approved(timeout_s=2)
        t0 = time.monotonic()
        self.depot("dispatch", d.name, FAKE_MODE="sleep")
        self.assertLess(time.monotonic() - t0, 30)
        run = self.run_dir(d)
        m = json.loads((run / "manifest.json").read_text())
        self.assertTrue(m["timed_out"])
        self.assertEqual(m["exit_code"], 124)
        pid = int((run / "child.pid").read_text())
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertIn("run_finished", self.types(d))

    def test_heartbeats_while_run_lives(self):
        d = self.approved()
        self.depot("dispatch", d.name, FAKE_MODE="silent")
        beats = [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines()
                 if json.loads(l)["type"] == "heartbeat"]
        self.assertGreaterEqual(len(beats), 1)
        self.assertIn("since_last_adapter_event_s", beats[0]["data"])

    def test_question_report_appends_input_required(self):
        d = self.approved()
        r = self.depot("dispatch", d.name, FAKE_MODE="question")
        self.assertEqual(r.returncode, 3)
        types = self.types(d)
        self.assertEqual(types[-2:], ["claimed", "input_required"])
        self.assertNotIn("verified", types)

    def test_adapter_missing_exits_127(self):
        d = self.approved()
        self.depot("dispatch", d.name, DEPOT_CODEX=str(self.tmp / "does-not-exist"))
        m = json.loads((self.run_dir(d) / "manifest.json").read_text())
        self.assertEqual(m["exit_code"], 127)


FAKE_CLAUDE = textwrap.dedent('''\
    #!{py}
    import json, os, sys
    stdin = sys.stdin.read()
    rec = os.environ["FAKE_RECORD"]
    open(os.path.join(rec, "claude-args.json"), "w").write(json.dumps(sys.argv[1:]))
    open(os.path.join(rec, "claude-stdin.txt"), "w").write(stdin)
    open(os.path.join(rec, "claude-cwd.txt"), "w").write(os.getcwd())
    mode = os.environ.get("FAKE_MODE", "build")
    print(json.dumps({{"type": "system", "subtype": "init", "session_id": "S1"}}), flush=True)
    if mode == "build":
        open("sentinel.py", "w").write("X = 1\\n")
        open("test_sentinel.py", "w").write(
            "import unittest\\nclass T(unittest.TestCase):\\n    def test_a(self):\\n        self.assertTrue(True)\\n")
        text = "built it"
    elif mode == "gate_pass":
        text = ("Checked everything.\\nCRITERION 1: PASS \\u2014 file set exact\\n"
                "CRITERION 2: PASS \\u2014 6 tests\\nGATE: PASS\\n")
    elif mode == "gate_fail":
        text = ("CRITERION 1: PASS \\u2014 file set exact\\nCRITERION 2: FAIL \\u2014 only 5 tests\\nGATE: FAIL\\n")
    elif mode == "gate_inconsistent":
        text = "CRITERION 1: FAIL \\u2014 nope\\nGATE: PASS\\n"
    else:
        text = "I looked at it and it seems fine. GATE: PASS but not on its own line.\\nThanks!\\n"
    print(json.dumps({{"type": "assistant", "message": "x"}}), flush=True)
    print(json.dumps({{"type": "result", "subtype": "success", "session_id": "S1",
                       "usage": {{"input_tokens": 3, "output_tokens": 4}}, "result": text}}), flush=True)
''')


def git_run(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def make_remote(base, files=None):
    """A bare remote whose main has one commit; returns its path."""
    remote = Path(base) / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    seed = Path(base) / "seed"
    seed.mkdir()
    git_run(seed, "init", "-q", "-b", "main")
    for name, text in (files or {"README.md": "hello\n"}).items():
        (seed / name).parent.mkdir(parents=True, exist_ok=True)
        (seed / name).write_text(text)
    git_run(seed, "add", "-A")
    git_run(seed, "commit", "-q", "-m", "seed")
    git_run(seed, "push", "-q", str(remote), "main")
    return remote


class S2Case(TurntableCase):
    def setUp(self):
        super().setUp()
        fake = self.tmp / "fake-claude"
        fake.write_text(FAKE_CLAUDE.format(py=sys.executable))
        fake.chmod(0o755)
        self.rec = self.tmp / "rec"
        self.rec.mkdir()
        self.env.update(DEPOT_CLAUDE=str(fake), FAKE_RECORD=str(self.rec))

    def approved(self, slug="thing", **waybill):
        d = self.new_item(slug, **waybill)
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        return d

    def events(self, d):
        return [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines()]

    def of_type(self, d, t):
        return [e for e in self.events(d) if e["type"] == t]


class TestWaybillFields(S2Case):
    def test_plain_waybill_has_no_s2_fields(self):
        wb = json.loads((self.new_item() / "waybill.json").read_text())
        for k in ("repo", "branch", "kind", "target", "materials"):
            self.assertNotIn(k, wb)

    def test_repo_and_branch_go_together_and_not_protected(self):
        base = ("new-item", "--title", "T", "--slug", "x", "--track", "t")
        self.assertNotEqual(self.depot(*base, "--repo", "/nowhere").returncode, 0)
        self.assertNotEqual(self.depot(*base, "--branch", "b").returncode, 0)
        self.assertNotEqual(self.depot(*base, "--repo", "/nowhere", "--branch", "main").returncode, 0)
        self.assertFalse((self.yard / "items").exists() and list((self.yard / "items").iterdir()))

    def test_gate_needs_target(self):
        r = self.depot("new-item", "--title", "G", "--slug", "g", "--track", "t", "--kind", "gate")
        self.assertNotEqual(r.returncode, 0)
        r = self.depot("new-item", "--title", "G", "--slug", "g", "--track", "t", "--kind", "gate",
                       "--target", "0009-nope")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(list((self.yard / "items").iterdir()))


class TestRepoItems(S2Case):
    def repo_item(self, slug="repo", branch="review/x", **kw):
        self.remote = make_remote(self.tmp)
        r = self.depot("new-item", "--title", "T", "--slug", slug, "--track", "t",
                       "--repo", str(self.remote), "--branch", branch)
        self.assertEqual(r.returncode, 0, r.stderr)
        d = self.yard / "items" / r.stdout.strip()
        (d / "brief.md").write_text(BRIEF.format(py=sys.executable))
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        return d

    def remote_ref(self, ref):
        out = subprocess.run(["git", "ls-remote", str(self.remote), ref], capture_output=True, text=True).stdout
        return out.split()[0] if out.split() else None

    def test_changed_repository_destination_requires_reapproval(self):
        for field in ("repo", "branch"):
            with self.subTest(field=field):
                if field == "repo":
                    d = self.repo_item(slug=field)
                else:
                    d = self.approved(slug=field, repo=str(self.remote), branch="review/x")
                wb = json.loads((d / "waybill.json").read_text())
                if field == "repo":
                    other = self.tmp / "other.git"
                    git_run(self.tmp, "clone", "-q", "--bare", str(self.remote), str(other))
                    wb[field] = str(other)
                else:
                    wb[field] = "review/revised"
                (d / "waybill.json").write_text(json.dumps(wb))
                refused = self.depot("dispatch", d.name)
                self.assertNotEqual(refused.returncode, 0)
                self.assertIn("task contract changed", refused.stderr)
                self.assertNotIn("dispatched", self.types(d))
                self.assertFalse((d / "work").exists())
                self.assertEqual(self.depot("approve", d.name).returncode, 0)
                result = self.depot("dispatch", d.name)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(git_run(d / "work", "remote", "get-url", "origin"), wb["repo"])
                self.assertEqual(git_run(d / "work", "branch", "--show-current"), wb["branch"])

    def test_clone_branch_commit_and_push_readback(self):
        d = self.repo_item()
        wb = json.loads((d / "waybill.json").read_text())
        self.assertEqual((wb["repo"], wb["branch"]), (str(self.remote), "review/x"))
        r = self.depot("dispatch", d.name)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        work = d / "work"
        self.assertEqual(git_run(work, "rev-parse", "--abbrev-ref", "HEAD"), "review/x")
        self.assertEqual(git_run(work, "remote", "get-url", "origin"), str(self.remote))
        self.assertTrue((work / "README.md").exists())  # a clone, not an init
        head = git_run(work, "rev-parse", "HEAD")
        self.assertEqual(git_run(work, "rev-parse", "HEAD~1"), self.remote_ref("refs/heads/main"))
        order = [e["type"] for e in self.events(d) if e["type"] != "heartbeat"]
        self.assertEqual(order[-4:], ["claimed", "verified", "committed", "pushed"])
        pushed = self.of_type(d, "pushed")[0]
        self.assertEqual(pushed["actor"], "turntable")
        self.assertEqual((pushed["data"]["remote_head"], pushed["data"]["commit"]), (head, head))
        self.assertEqual(self.remote_ref("refs/heads/review/x"), head)
        branches = subprocess.run(["git", "-C", str(self.remote), "branch", "--format=%(refname:short)"],
                                  capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(branches), ["main", "review/x"])

    def test_existing_remote_branch_is_checked_out(self):
        d = self.repo_item(branch="review/y")
        seed = self.tmp / "seed"
        git_run(seed, "checkout", "-q", "-b", "review/y")
        (seed / "prior.txt").write_text("prior\n")
        git_run(seed, "add", "-A")
        git_run(seed, "commit", "-q", "-m", "prior work")
        git_run(seed, "push", "-q", str(self.remote), "review/y")
        prior = git_run(seed, "rev-parse", "HEAD")
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        work = d / "work"
        self.assertEqual(git_run(work, "rev-parse", "HEAD~1"), prior)
        self.assertEqual(self.remote_ref("refs/heads/review/y"), git_run(work, "rev-parse", "HEAD"))

    def test_second_dispatch_reuses_clone_on_branch(self):
        d = self.repo_item()
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        first = git_run(d / "work", "rev-parse", "HEAD")
        time.sleep(1.1)  # run ids are second-resolution
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        self.depot("dispatch", d.name)
        self.assertEqual(git_run(d / "work", "rev-parse", "--abbrev-ref", "HEAD"), "review/x")
        self.assertEqual(git_run(d / "work", "rev-parse", "HEAD~0"), first)  # nothing new to commit: unchanged

    def test_redispatch_starts_from_the_moved_remote_head(self):
        d = self.repo_item()
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        other = self.tmp / "other"
        git_run(self.tmp, "clone", "-q", "-b", "review/x", str(self.remote), str(other))
        (other / "moved.txt").write_text("moved on origin\n")
        git_run(other, "add", "-A")
        git_run(other, "commit", "-q", "-m", "moved on origin")
        git_run(other, "push", "-q", "origin", "review/x")
        moved = self.remote_ref("refs/heads/review/x")
        time.sleep(1.1)  # run ids are second-resolution
        # the fake agent re-writes identical files, so this run has no change and verifies as discrepancies;
        # what's under test is the tree the run starts from, not its verdict
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        self.depot("dispatch", d.name)
        work = d / "work"
        self.assertTrue((work / "moved.txt").exists())
        self.assertEqual(git_run(work, "rev-parse", "HEAD"), self.remote_ref("refs/heads/review/x"))
        self.assertIn(moved, git_run(work, "rev-list", "HEAD").split())

    def test_redispatch_discards_uncommitted_changes_from_the_prior_run(self):
        d = self.repo_item()
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        work = d / "work"
        (work / "README.md").write_text("dirty, never committed\n")
        (work / "junk.txt").write_text("leftover from a prior run\n")
        time.sleep(1.1)  # run ids are second-resolution
        # the fake agent re-writes identical files, so this run has no change and verifies as discrepancies;
        # what's under test is the tree the run starts from, not its verdict
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        self.depot("dispatch", d.name)
        self.assertFalse((work / "junk.txt").exists())
        head = self.remote_ref("refs/heads/review/x")
        self.assertEqual(git_run(self.remote, "show", f"{head}:README.md"), "hello")
        self.assertEqual(git_run(self.remote, "ls-tree", "--name-only", head, "junk.txt"), "")

    def test_redispatch_refuses_a_local_branch_with_unpushed_commits(self):
        d = self.repo_item()
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        work = d / "work"
        git_run(work, "commit", "-q", "--allow-empty", "-m", "unpushed local commit")
        unpushed = git_run(work, "rev-parse", "HEAD")
        remote_before = self.remote_ref("refs/heads/review/x")
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        r = self.depot("dispatch", d.name)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("review/x", r.stdout + r.stderr)
        self.assertIn("not on origin", r.stdout + r.stderr)
        self.assertEqual(git_run(work, "rev-parse", "refs/heads/review/x"), unpushed)  # left intact
        self.assertEqual(self.remote_ref("refs/heads/review/x"), remote_before)
        self.assertEqual(len(list((d / "runs").iterdir())), 1)  # no second run began

    def test_redispatch_recreates_a_never_pushed_branch_from_origin_head(self):
        d = self.repo_item()
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        work = d / "work"
        git_run(work, "checkout", "-q", "-f", "-B", "review/x", "origin/main")  # local-only, at origin's head
        git_run(self.remote, "update-ref", "-d", "refs/heads/review/x")  # the remote never had it
        time.sleep(1.1)  # run ids are second-resolution
        # the fake agent re-writes identical files, so this run has no change and verifies as discrepancies;
        # what's under test is the tree the run starts from, not its verdict
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        self.depot("dispatch", d.name)
        self.assertEqual(git_run(work, "rev-parse", "HEAD~1"), self.remote_ref("refs/heads/main"))

    def test_push_failure_records_push_failed_and_exits_1(self):
        d = self.repo_item()
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'rejected by test hook' >&2\nexit 1\n")
        hook.chmod(0o755)
        r = self.depot("dispatch", d.name)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.of_type(d, "pushed"), [])
        failed = self.of_type(d, "push_failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("rejected by test hook", failed[0]["data"]["error"])
        self.assertEqual(failed[0]["data"]["branch"], "review/x")
        self.assertEqual(len(self.of_type(d, "committed")), 1)  # the local commit stands
        self.assertEqual(json.loads((d / "waybill.json").read_text())["status"], "blocked")
        self.assertIsNone(self.remote_ref("refs/heads/review/x"))

    def test_discrepancy_run_pushes_nothing(self):
        d = self.repo_item()
        self.depot("dispatch", d.name, FAKE_MODE="stowaway")
        self.assertEqual(self.of_type(d, "pushed"), [])
        self.assertEqual(self.of_type(d, "push_failed"), [])
        self.assertIsNone(self.remote_ref("refs/heads/review/x"))


class TestReviewedStatus(S2Case):
    def append(self, d, result):
        ev = {"ts": "2026-10-30T00:00:00.000Z", "item": d.name, "type": "reviewed", "actor": "reviewer",
              "data": {"result": result}}
        with open(d / "events.jsonl", "a") as f:
            f.write(json.dumps(ev) + "\n")

    def test_reviewed_pass_and_other(self):
        d = self.approved()
        self.depot("dispatch", d.name)
        self.append(d, "pass")
        self.assertIn("stage=reviewed", self.depot("status").stdout)
        self.append(d, "defects")
        self.assertIn("stage=blocked", self.depot("status").stdout)


class TestClaudeAdapter(S2Case):
    def test_command_stdin_manifest_and_verification(self):
        d = self.approved(adapter="claude", model="m-1")
        r = self.depot("dispatch", d.name)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        args = json.loads((self.rec / "claude-args.json").read_text())
        self.assertEqual(args, ["-p", "--output-format", "stream-json", "--verbose", "--permission-prompts", "none",
                                "--allowedTools", "Bash,Read,Write,Edit,Grep,Glob", "--model", "m-1"])
        stdin = (self.rec / "claude-stdin.txt").read_text()
        self.assertIn("TASK: build the sentinel.", stdin)
        self.assertNotIn("```depot", stdin)
        self.assertEqual(Path((self.rec / "claude-cwd.txt").read_text()).resolve(), (d / "work").resolve())
        run = self.run_dir(d)
        m = json.loads((run / "manifest.json").read_text())
        self.assertEqual((m["adapter"], m["model"], m["session_or_thread_id"], m["exit_code"]),
                         ("claude", "m-1", "S1", 0))
        self.assertEqual(m["usage"], {"input_tokens": 3, "output_tokens": 4})
        self.assertEqual(m["sandbox"], "allowed-tools+prompts-none")
        self.assertEqual((run / "report.md").read_text(), "built it")
        self.assertTrue((run / "events.jsonl").read_text().strip())
        self.assertEqual(json.loads((run / "verify.json").read_text())["result"], "pass")
        self.assertEqual(len(self.of_type(d, "committed")), 1)

    def test_model_flag_absent_when_not_pinned(self):
        d = self.approved(adapter="claude")
        self.depot("dispatch", d.name)
        self.assertNotIn("--model", json.loads((self.rec / "claude-args.json").read_text()))

    def test_unknown_adapter_refused_without_dispatched_event(self):
        d = self.approved(adapter="nope")
        r = self.depot("dispatch", d.name)
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown adapter", r.stderr)
        self.assertNotIn("dispatched", self.types(d))


class TestGateItems(S2Case):
    def setUp(self):
        super().setUp()
        self.target = self.approved("built")
        self.assertEqual(self.depot("dispatch", self.target.name).returncode, 0)
        (self.run_dir(self.target) / "probes.txt").write_text("P1 ok\n")
        self.snap = self.snapshot(self.target)

    def snapshot(self, d):
        return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*"))
                if p.is_file() and "work" not in p.relative_to(d).parts}

    def gate(self, *extra):
        r = self.depot("new-item", "--title", "G", "--slug", "gate", "--track", "t", "--kind", "gate",
                       "--target", self.target.name, *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        g = self.yard / "items" / r.stdout.strip()
        (g / "brief.md").write_text('```depot\n{"expected_files": [], "test_command": ""}\n```\n\nGATE TASK\n')
        self.assertEqual(self.depot("approve", g.name).returncode, 0)
        return g

    def test_gate_waybill_defaults(self):
        wb = json.loads((self.gate() / "waybill.json").read_text())
        self.assertEqual((wb["kind"], wb["target"], wb["adapter"]), ("gate", self.target.name, "claude"))

    def test_pass_verdict_lands_on_target_and_gate_is_read_only(self):
        g = self.gate("--material", "item:events.jsonl", "--material", "run:probes.txt")
        r = self.depot("dispatch", g.name, FAKE_MODE="gate_pass")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        report = (self.run_dir(g) / "report.md").read_text()
        ev = self.of_type(self.target, "gate_result")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["actor"], "claude")
        d = ev[0]["data"]
        self.assertEqual(d["result"], "pass")
        self.assertEqual(d["gate"], g.name)
        self.assertEqual(d["run_id"], self.run_dir(g).name)
        self.assertEqual(d["criteria"], [l for l in report.splitlines() if l.startswith("CRITERION")])
        # gate trail completes normally and skips verification
        types = [e["type"] for e in self.events(g) if e["type"] != "heartbeat"]
        self.assertEqual(types, ["item_created", "brief_approved", "dispatched", "run_finished", "claimed"])
        self.assertFalse((self.run_dir(g) / "verify.json").exists())
        m = json.loads((self.run_dir(g) / "manifest.json").read_text())
        self.assertEqual((m["adapter"], m["session_or_thread_id"]), ("claude", "S1"))
        # materials: exactly the copies, no repo
        got = sorted(p.name for p in (g / "work").iterdir())
        self.assertEqual(got, sorted(["brief.md", "diff.patch", "verify.json", "manifest.json", "report.md",
                                      "events.jsonl", "probes.txt"]))
        self.assertEqual(Path((self.rec / "claude-cwd.txt").read_text()).resolve(), (g / "work").resolve())
        # pass leaves the target's status alone, and nothing of the target but its log was rewritten
        self.assertEqual(self.depot("status").stdout.count("stage=verified"), 1)
        after = self.snapshot(self.target)
        self.assertEqual({k for k in after if after[k] != self.snap.get(k)}, {"events.jsonl"})
        self.assertTrue(after["events.jsonl"].startswith(self.snap["events.jsonl"]))

    def test_default_materials_are_exactly_the_five(self):
        g = self.gate()
        self.depot("dispatch", g.name, FAKE_MODE="gate_pass")
        self.assertEqual(sorted(p.name for p in (g / "work").iterdir()),
                         ["brief.md", "diff.patch", "manifest.json", "report.md", "verify.json"])

    def test_fail_verdict_blocks_target(self):
        g = self.gate()
        self.depot("dispatch", g.name, FAKE_MODE="gate_fail")
        ev = self.of_type(self.target, "gate_result")[0]
        self.assertEqual(ev["data"]["result"], "fail")
        self.assertEqual(len(ev["data"]["criteria"]), 2)
        self.assertIn("stage=blocked", self.depot("status").stdout.splitlines()[0])
        self.assertEqual(json.loads((self.target / "waybill.json").read_text())["status"], "blocked")

    def test_unparseable_is_recorded_not_guessed(self):
        for mode in ("garbage", "gate_inconsistent"):
            with self.subTest(mode=mode):
                before = len(self.of_type(self.target, "gate_result"))
                g = self.gate()
                time.sleep(1.1)
                r = self.depot("dispatch", g.name, FAKE_MODE=mode)
                self.assertEqual(r.returncode, 1)
                ev = self.of_type(self.target, "gate_result")[before]
                self.assertEqual(ev["data"]["result"], "unparseable")
                self.assertNotIn("criteria", ev["data"])
                self.assertTrue(ev["data"]["report"].endswith("report.md"))
                self.assertTrue((self.yard / ev["data"]["report"]).exists())
                self.assertEqual(json.loads((self.target / "waybill.json").read_text())["status"], "blocked")

    def test_parser_strictness(self):
        sys.path.insert(0, str(Path(DEPOT).parent))
        import depot
        ok = "CRITERION 1: PASS — e\nGATE: PASS"
        self.assertEqual(depot.parse_gate_report(ok)["result"], "pass")
        self.assertIsNone(depot.parse_gate_report(ok + "\nthanks"))                     # verdict not last
        self.assertIsNone(depot.parse_gate_report("GATE: PASS"))                        # no criteria
        self.assertIsNone(depot.parse_gate_report(ok + "\nGATE: PASS"))                 # two verdicts
        self.assertIsNone(depot.parse_gate_report("CRITERION 1: PASS - e\n**GATE: PASS**"))
        self.assertEqual(depot.parse_gate_report("CRITERION 1: FAIL - e\nGATE: FAIL")["result"], "fail")

    def test_missing_material_refuses_before_dispatch(self):
        g = self.gate("--material", "run:absent.txt")
        r = self.depot("dispatch", g.name, FAKE_MODE="gate_pass")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("dispatched", self.types(g))
        self.assertEqual(self.of_type(self.target, "gate_result"), [])


class TestPoll(S2Case):
    def test_once_dispatches_approved_only(self):
        a = self.approved("one")
        b = self.new_item("two")  # not approved
        r = self.depot("poll", "--once")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("committed", self.types(a))
        self.assertNotIn("dispatched", self.types(b))
        self.assertTrue((self.yard / "log" / "poll.log").read_text().strip())
        self.assertFalse((self.yard / "log" / "poll.lock").exists())
        # a second cycle finds nothing left to do
        r2 = self.depot("poll", "--once")
        self.assertEqual(len([t for t in self.types(a) if t == "dispatched"]), 1)
        self.assertIn("0 approved", r2.stdout)

    def test_second_instance_refused_while_lock_pid_lives(self):
        (self.yard / "log" / "poll.lock").write_text(str(os.getpid()))
        r = self.depot("poll", "--once")
        self.assertEqual(r.returncode, 2)
        self.assertIn("already running", r.stderr)
        self.assertEqual((self.yard / "log" / "poll.lock").read_text(), str(os.getpid()))  # untouched

    def test_stale_lock_is_replaced(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        (self.yard / "log" / "poll.lock").write_text(str(dead.pid))
        self.assertEqual(self.depot("poll", "--once").returncode, 0)
        self.assertFalse((self.yard / "log" / "poll.lock").exists())

    def test_sigterm_finishes_current_dispatch_then_stops(self):
        d = self.approved("slow")
        proc = subprocess.Popen([sys.executable, DEPOT, "poll", "--interval", "30"], env={**self.env, "FAKE_MODE": "silent"},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(100):  # wait for the dispatch to start
            if "dispatched" in self.types(d):
                break
            time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertIn("committed", self.types(d))  # the dispatch ran to completion
        self.assertFalse((self.yard / "log" / "poll.lock").exists())

    def test_git_yard_pulls_dispatches_commits_and_pushes_main(self):
        origin = make_remote(self.tmp, {".gitignore": "items/*/work/\n", "log/events.jsonl": ""})
        reviewer = self.tmp / "reviewer"
        git_run(self.tmp, "clone", "-q", str(origin), str(reviewer))
        cenv = {**self.env, "DEPOT_YARD": str(reviewer)}
        r = subprocess.run([sys.executable, DEPOT, "new-item", "--title", "T", "--slug", "wc", "--track", "t"],
                           env=cenv, capture_output=True, text=True)
        item = r.stdout.strip()
        (reviewer / "items" / item / "brief.md").write_text(BRIEF.format(py=sys.executable))
        sys.path.insert(0, str(Path(DEPOT).parent))
        import depot
        approved_data = depot.approval_data(depot.Item(reviewer / "items" / item))
        ev = {"ts": "2026-10-30T00:00:00.000Z", "item": item, "type": "brief_approved", "actor": "operator",
              "data": {"via": "connector", "directed_by": "operator", **approved_data}}
        for p in (reviewer / "items" / item / "events.jsonl", reviewer / "log" / "events.jsonl"):
            with open(p, "a") as f:
                f.write(json.dumps(ev) + "\n")
        git_run(reviewer, "add", "-A")
        git_run(reviewer, "commit", "-q", "-m", "file item via connector")
        git_run(reviewer, "push", "-q", "origin", "main")

        yard = self.tmp / "yard-clone"
        git_run(self.tmp, "clone", "-q", str(origin), str(yard))
        penv = {**self.env, "DEPOT_YARD": str(yard)}
        r = subprocess.run([sys.executable, DEPOT, "poll", "--once"], env=penv, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"dispatching {item}", r.stdout)
        self.assertIn("pushed main", r.stdout)

        check = self.tmp / "readback"
        git_run(self.tmp, "clone", "-q", str(origin), str(check))
        log = [json.loads(l) for l in (check / "items" / item / "events.jsonl").read_text().splitlines()]
        types = [e["type"] for e in log if e["type"] != "heartbeat"]
        self.assertEqual(types, ["item_created", "brief_approved", "dispatched", "run_finished", "claimed",
                                 "verified", "committed"])
        self.assertEqual(log[1]["data"], {"via": "connector", "directed_by": "operator", **approved_data})
        run = next((check / "items" / item / "runs").iterdir())
        for f in ("manifest.json", "verify.json", "report.md", "events.jsonl"):
            self.assertTrue((run / f).exists(), f)
        self.assertFalse((check / "items" / item / "work").exists())  # nested repo stays out of the yard
        self.assertEqual(git_run(check, "log", "-1", "--format=%s"), f"depot: {item} pass")
        self.assertEqual(git_run(check, "rev-parse", "HEAD"), git_run(yard, "rev-parse", "HEAD"))

    def failed_pull_yard(self, dirty=False):
        item = self.approved()
        files = {str(p.relative_to(self.yard)): p.read_text()
                 for p in self.yard.rglob("*") if p.is_file()}
        files.update({".gitignore": "items/*/work/\nlog/poll.log\nlog/poll.lock\nlog/intents/\n",
                      "shared.txt": "base\n"})
        origin = make_remote(self.tmp, files)
        operator = self.tmp / "seed"
        clone = self.tmp / "poll-yard"
        git_run(self.tmp, "clone", "-q", str(origin), str(clone))
        self.yard = clone
        self.env["DEPOT_YARD"] = str(clone)
        if dirty:
            (clone / "shared.txt").write_text("uncommitted operator edit\n")
        else:
            for i in range(2):
                (clone / "local.txt").write_text(f"local {i}\n")
                git_run(clone, "add", "local.txt")
                git_run(clone, "commit", "-q", "-m", f"local {i}")
        for i in range(1 if dirty else 7):
            (operator / "shared.txt").write_text(f"remote {i}\n")
            git_run(operator, "add", "shared.txt")
            git_run(operator, "commit", "-q", "-m", f"remote {i}")
        git_run(operator, "push", "-q", str(origin), "main")
        iid = "12345678-1234-4234-8234-123456789abc"
        pending = clone / "log" / "intents" / f"{iid}.json"
        pending.parent.mkdir()
        pending.write_text(json.dumps({"id": iid, "ts": "2026-11-22T00:00:00Z", "item": item.name,
                                       "kind": "message", "data": {"text": "pending message"}}))
        return origin, operator, clone / "items" / item.name, pending

    def start_poll(self):
        proc = subprocess.Popen([sys.executable, DEPOT, "poll", "--interval", "0.5"], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def cleanup():
            if proc.poll() is None:
                proc.terminate()
            proc.communicate(timeout=30)
        self.addCleanup(cleanup)
        return proc

    def wait_for_poll_log(self, proc, text):
        deadline = time.monotonic() + 15
        log = self.yard / "log" / "poll.log"
        while time.monotonic() < deadline:
            content = log.read_text() if log.exists() else ""
            if text in content:
                return content
            if proc.poll() is not None:
                self.fail(f"poll exited {proc.returncode}: {proc.communicate()}\n{content}")
            time.sleep(0.05)
        self.fail(f"poll did not log {text!r}: {content}")

    def test_diverged_poll_stays_up_holds_lock_and_skips_work(self):
        origin, _, item, pending = self.failed_pull_yard()
        head = git_run(self.yard, "rev-parse", "HEAD")
        remote_head = git_run(origin, "rev-parse", "main")
        records = (item / "events.jsonl").read_bytes()
        intent = pending.read_bytes()
        proc = self.start_poll()
        self.wait_for_poll_log(proc, "ahead=2 behind=7")
        # Observe more than one failed cycle in this same process.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            log = (self.yard / "log" / "poll.log").read_text()
            if log.count("git pull --ff-only failed") >= 2:
                break
            time.sleep(0.05)
        self.assertGreaterEqual(log.count("git pull --ff-only failed"), 2)
        self.assertIn("Not possible to fast-forward", log)  # actual git stderr
        self.assertIsNone(proc.poll())
        self.assertEqual((self.yard / "log" / "poll.lock").read_text(), str(proc.pid))
        second = self.depot("poll", "--once")
        self.assertEqual(second.returncode, 2)
        self.assertIn("already running", second.stderr)
        self.assertEqual((item / "events.jsonl").read_bytes(), records)
        self.assertFalse((item / "runs").exists())
        self.assertEqual(pending.read_bytes(), intent)
        self.assertEqual(list(pending.parent.glob("*.outcome.json")), [])
        self.assertEqual(git_run(self.yard, "rev-parse", "HEAD"), head)
        self.assertEqual(git_run(origin, "rev-parse", "main"), remote_head)

    def test_dirty_pull_skips_dispatch_and_intents_until_next_cycle(self):
        _, _, item, pending = self.failed_pull_yard(dirty=True)
        records = (item / "events.jsonl").read_bytes()
        proc = self.start_poll()
        log = self.wait_for_poll_log(proc, "will retry next cycle")
        self.assertIn("ahead=0 behind=1", log)
        self.assertIn("would be overwritten by merge", log)
        self.assertIsNone(proc.poll())
        self.assertEqual((item / "events.jsonl").read_bytes(), records)
        self.assertFalse((item / "runs").exists())
        self.assertTrue(pending.exists())
        self.assertEqual(list(pending.parent.glob("*.outcome.json")), [])
        self.assertEqual((self.yard / "shared.txt").read_text(), "uncommitted operator edit\n")

    def test_operator_reconciliation_resumes_pull_and_dispatch_without_restart(self):
        origin, operator, item, pending = self.failed_pull_yard()
        proc = self.start_poll()
        self.wait_for_poll_log(proc, "ahead=2 behind=7")
        # Operator action in a separate checkout: preserve both histories on origin,
        # so the running daemon's next ff-only pull can advance without intervention.
        git_run(operator, "fetch", "-q", str(self.yard), "main")
        git_run(operator, "merge", "--no-edit", "FETCH_HEAD")
        (operator / "recovery.txt").write_text("arrived during recovery\n")
        git_run(operator, "add", "recovery.txt")
        git_run(operator, "commit", "-q", "-m", "recovery marker")
        git_run(operator, "push", "-q", str(origin), "main")
        self.wait_for_poll_log(proc, f"dispatched {item.name}: exit=0 outcome=pass")
        self.wait_for_poll_log(proc, "pushed main")
        self.assertIsNone(proc.poll())
        self.assertEqual((self.yard / "log" / "poll.lock").read_text(), str(proc.pid))
        self.assertEqual((self.yard / "recovery.txt").read_text(), "arrived during recovery\n")
        self.assertEqual(self.types(item).count("dispatched"), 1)
        self.assertIn("committed", self.types(item))
        outcome = pending.with_name(pending.stem + ".outcome.json")
        self.assertEqual(json.loads(outcome.read_text())["state"], "landed")
        log = (self.yard / "log" / "poll.log").read_text()
        self.assertEqual(log.count("poll start pid="), 1)
        self.assertIn("pulled (ff-only)", log)
        self.assertNotIn("poll stop", log)

    def test_once_failed_pull_logs_and_exits_normally_without_work(self):
        _, _, item, pending = self.failed_pull_yard()
        r = self.depot("poll", "--once")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ahead=2 behind=7", r.stdout)
        self.assertNotIn("dispatched", self.types(item))
        self.assertTrue(pending.exists())
        self.assertEqual(list(pending.parent.glob("*.outcome.json")), [])
        self.assertFalse((self.yard / "log" / "poll.lock").exists())

    def test_git_yard_off_main_refuses(self):
        origin = make_remote(self.tmp, {"log/events.jsonl": ""})
        y = self.tmp / "y"
        git_run(self.tmp, "clone", "-q", str(origin), str(y))
        git_run(y, "checkout", "-q", "-b", "feature")
        r = subprocess.run([sys.executable, DEPOT, "poll", "--once"], env={**self.env, "DEPOT_YARD": str(y)},
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 3)
        self.assertIn("operates on main", r.stdout)


class TestVerificationSandbox(TurntableCase):
    def setUp(self):
        super().setUp()
        self.work = self.tmp / "work"
        self.work.mkdir()

    def run_code(self, code):
        (self.work / "probe.py").write_text(textwrap.dedent(code))
        return depot.run_tests(self.work, f"{shlex.quote(sys.executable)} probe.py")

    def assert_pass(self, result):
        self.assertEqual(result["exit_code"], 0, result["tail"])

    def test_outside_writes_refused_including_symlinks_and_children(self):
        outside = self.tmp / "outside"
        outside.write_text("unchanged")
        (self.work / "link").symlink_to(outside)
        self.assert_pass(self.run_code(f"""
            import pathlib, subprocess, sys
            for target in ({str(outside)!r}, '../outside', '/escape', '/usr/escape', 'link'):
                try:
                    pathlib.Path(target).write_text('escaped')
                except OSError:
                    pass
                else:
                    raise AssertionError(target)
            r = subprocess.run([sys.executable, '-c',
                "open({str(outside)!r}, 'w').write('child escape')"])
            assert r.returncode != 0
            pathlib.Path('inside').write_text('allowed')
        """))
        self.assertEqual(outside.read_text(), "unchanged")
        self.assertEqual((self.work / "inside").read_text(), "allowed")
        result = self.run_code("open('../escape', 'w').write('escaped')")
        self.assertNotEqual(result["exit_code"], 0)
        self.assertFalse((self.tmp / "escape").exists())

    def test_environment_and_credential_paths_are_unavailable(self):
        home = self.tmp / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".ssh" / "id_ed25519").write_text("private key")
        for name in (".aws", ".codex", ".ssh", ".git"):
            (self.work / name).mkdir()
            (self.work / name / "secret").write_text("secret")
        (self.work / ".netrc").write_text("machine example password secret")
        secrets = {"GH_TOKEN": "host-token", "AWS_SECRET_ACCESS_KEY": "host-key",
                   "SSH_AUTH_SOCK": str(self.work / "agent.sock"), "HOME": str(home),
                   "GIT_CONFIG_GLOBAL": str(home / "gitconfig"), "PYTHONPATH": str(home),
                   "CUSTOM_AUTH_MATERIAL": "secret", "LD_PRELOAD": str(home / "evil.so")}
        with mock.patch.dict(os.environ, secrets):
            self.assert_pass(self.run_code(f"""
                import os, pathlib
                assert set(os.environ) <= {{'PATH', 'HOME', 'TMPDIR', 'LANG',
                                          'PYTHONDONTWRITEBYTECODE', 'LC_CTYPE', 'PWD'}}
                assert os.environ['HOME'] == '/nonexistent'
                for name in {list(secrets)!r}:
                    assert os.environ.get(name) not in {list(secrets.values())!r}
                assert not pathlib.Path({str(home)!r}).exists()
                for name in ('.aws', '.codex', '.ssh', '.git'):
                    assert not list(pathlib.Path(name).iterdir())
                assert pathlib.Path('.netrc').read_text() == ''
            """))

    def test_no_network_or_local_publication_service_access(self):
        self.assert_pass(self.run_code("""
            import socket
            for family in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX):
                try:
                    socket.socket(family, socket.SOCK_STREAM)
                except PermissionError:
                    pass
                else:
                    raise AssertionError('socket access granted')
        """))

    def test_repository_commit_push_and_metadata_writes_refused(self):
        remote = make_remote(self.tmp)
        git_run(self.work, "clone", "-q", str(remote), ".")
        before = git_run(self.work, "rev-parse", "HEAD")
        self.assert_pass(self.run_code(f"""
            import pathlib, subprocess
            commands = [
                ['git', '-c', 'user.name=agent', '-c', 'user.email=a@b',
                 'commit', '--allow-empty', '-m', 'unauthorized'],
                ['git', 'push', 'origin', 'HEAD:refs/heads/escaped'],
                ['git', '--git-dir={str(remote)}', 'update-ref', 'refs/heads/escaped', '{before}'],
            ]
            for command in commands:
                assert subprocess.run(command, capture_output=True).returncode != 0
            for name in ('.git/config', '.git/HEAD', '.git/hooks/pre-commit'):
                try:
                    pathlib.Path(name).write_text('attack')
                except OSError:
                    pass
                else:
                    raise AssertionError(name)
        """))
        self.assertEqual(git_run(self.work, "rev-parse", "HEAD"), before)
        self.assertEqual(git_run(self.tmp, "--git-dir", str(remote), "branch", "--format=%(refname)"),
                         "refs/heads/main")

    def test_passing_verification_commits_and_pushes_with_host_authority(self):
        remote = make_remote(self.tmp)
        git_run(self.work, "clone", "-q", str(remote), ".")
        git_run(self.work, "checkout", "-q", "-b", "review/sandbox")
        before = git_run(self.work, "rev-parse", "HEAD")
        outside = self.tmp / "host-commit-receipt"
        hook = self.work / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!{sys.executable}\nimport os, pathlib\n"
                        "assert os.environ['PUBLICATION_TOKEN'] == 'host-only'\n"
                        f"pathlib.Path({str(outside)!r}).write_text('host committed')\n")
        hook.chmod(0o755)
        (self.work / "test_clean.py").write_text(
            "import os, unittest\nclass Clean(unittest.TestCase):\n"
            "    def test_clean(self):\n"
            "        self.assertNotIn('PUBLICATION_TOKEN', os.environ)\n")
        item = depot.Item(self.new_item())
        run = item.path / "run"
        run.mkdir()
        wb = {"expected_files": ["test_clean.py"], "test_command": "python3 -m unittest",
              "repo": str(remote), "branch": "review/sandbox"}
        with mock.patch.dict(os.environ, {"DEPOT_YARD": str(self.yard),
                                          "PUBLICATION_TOKEN": "host-only"}):
            verify, pushed = depot.verify_and_commit(item, self.work, run, wb)
        self.assertEqual(verify["result"], "pass", verify)
        self.assertTrue(pushed)
        # Publication uses commit-tree on the recorded tree, so agent hooks never run:
        # the hook file is present but its receipt is absent, and the commit is exactly
        # the recorded tree on the pinned base.
        self.assertFalse(outside.exists())
        self.assertEqual(git_run(self.work, "rev-parse", verify["commit"] + "^{tree}"), verify["tree"])
        self.assertEqual(git_run(self.work, "rev-parse", verify["commit"] + "^"), before)
        self.assertEqual(git_run(self.tmp, "--git-dir", str(remote), "rev-parse", "review/sandbox"),
                         verify["commit"])
        self.assertEqual(self.types(item.path)[-3:], ["verified", "committed", "pushed"])

    def test_sandbox_setup_failure_does_not_execute_tests(self):
        with mock.patch.object(depot, "verification_filter", side_effect=OSError("unavailable")):
            result = self.run_code("open('executed', 'w').write('unsafe')")
        self.assertEqual(result["exit_code"], 127)
        self.assertIn("verification sandbox", result["tail"])
        self.assertFalse((self.work / "executed").exists())

    def test_timeout_leaves_no_test_descendants(self):
        with mock.patch.object(depot, "TEST_TIMEOUT_S", 0.5):
            result = self.run_code("""
                import subprocess, sys, time
                subprocess.Popen([sys.executable, '-c',
                    "import time; time.sleep(1); open('late-write', 'w').write('escaped')"])
                time.sleep(60)
            """)
        self.assertEqual(result["exit_code"], 124, result)
        time.sleep(1)
        self.assertFalse((self.work / "late-write").exists())


if __name__ == "__main__":
    unittest.main()
