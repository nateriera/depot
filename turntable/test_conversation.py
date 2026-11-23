"""S4 tests: message, reopen, the input-required answer flow, queued-message delivery, records-commit
guards, and the floor projection of all of it. New file: the S1-S3 suites are untouched."""
import hashlib
import json
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import depot  # noqa: E402
import floor  # noqa: E402
from test_depot import TurntableCase  # noqa: E402

NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)
MULTILINE = "Use file name `foo_bar.py`.\n\n## Not a real heading\n  indented — with “quotes” & <tags>\n"


class ConvCase(TurntableCase):
    """TurntableCase plus helpers to craft trails and to read what a dispatch was sent."""

    def ev(self, d, type_, actor="turntable", mins_ago=0, **data):
        e = {"ts": floor.iso_z(NOW - timedelta(minutes=mins_ago)), "item": d.name, "type": type_,
             "actor": actor, "data": data}
        for p in (d / "events.jsonl", self.yard / "log" / "events.jsonl"):
            with open(p, "a") as f:
                f.write(json.dumps(e) + "\n")
        return e

    def events(self, d):
        return [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines()]

    def snapshot(self):
        return {str(p.relative_to(self.yard)): p.read_bytes() for p in sorted(self.yard.rglob("*"))
                if p.is_file() and ".git" not in p.parts}

    def status(self, d):
        r = self.depot("status")
        for line in r.stdout.splitlines():
            if line.startswith(d.name):
                return line.split("stage=")[1].split()[0]

    def done_item(self, slug="thing"):
        """An item with a crafted Done trail (reviewed pass), no runs."""
        d = self.new_item(slug)
        for i, (t, a, kw) in enumerate([
                ("brief_approved", "operator", {}), ("dispatched", "turntable", {"adapter": "codex", "run_id": "R0"}),
                ("run_finished", "turntable", {"exit_code": 0, "elapsed_s": 5, "timed_out": False}),
                ("claimed", "codex", {"report": "r"}), ("verified", "turntable", {"result": "pass"}),
                ("reviewed", "reviewer", {"result": "pass"})]):
            self.ev(d, t, a, mins_ago=100 - i, **kw)
        return d

    def approved(self, **wb):
        d = self.new_item(**wb)
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        return d

    def sent(self, d, run=-1):
        runs = sorted((d / "runs").iterdir())
        return (runs[run] / "brief.dispatched.md").read_text()


class TestMessage(ConvCase):
    def test_message_on_an_active_item_appends_and_changes_nothing(self):
        d = self.approved()
        wb_before = (d / "waybill.json").read_bytes()
        r = self.depot("message", d.name, MULTILINE)
        self.assertEqual(r.returncode, 0, r.stderr)
        e = self.events(d)[-1]
        self.assertEqual((e["type"], e["actor"], e["data"]), ("message", "operator", {"text": MULTILINE}))
        glog = [json.loads(l) for l in (self.yard / "log" / "events.jsonl").read_text().splitlines()]
        self.assertEqual(glog[-1], e)
        self.assertEqual(self.status(d), "approved")                      # no status change
        self.assertEqual((d / "waybill.json").read_bytes(), wb_before)    # no waybill edit
        self.assertFalse((d / "runs").exists())                           # no dispatch

    def test_message_is_valid_in_every_status_and_never_moves_the_status(self):
        for i, (label, trail) in enumerate([
                ("queued", []), ("blocked", [("input_required", {"question": "Q?", "run_id": "R0"})])]):
            d = self.new_item(f"s{i}")
            if label != "queued":
                self.ev(d, "brief_approved", "operator")
                self.ev(d, "dispatched", adapter="codex", run_id="R0")
            for t, kw in trail:
                self.ev(d, t, "codex", **kw)
            before = self.status(d)
            self.assertEqual(self.depot("message", d.name, "hello").returncode, 0)
            self.assertEqual(self.status(d), before, label)
        done = self.done_item("done")
        self.assertEqual(self.status(done), "reviewed")
        self.assertEqual(self.depot("message", done.name, "late note").returncode, 0)
        self.assertEqual(self.status(done), "reviewed")  # stays Done

    def test_empty_message_refused_and_nothing_appended(self):
        d = self.approved()
        before = self.snapshot()
        r = self.depot("message", d.name, "  \n ")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(before, self.snapshot())

    def test_unknown_item_refused(self):
        self.assertEqual(self.depot("message", "9999", "x").returncode, 2)


class TestReopen(ConvCase):
    def test_reopen_on_done_returns_to_queued_and_does_not_dispatch(self):
        d = self.done_item()
        r = self.depot("reopen", d.name, "needs the edge case")
        self.assertEqual(r.returncode, 0, r.stderr)
        e = self.events(d)[-1]
        self.assertEqual((e["type"], e["actor"], e["data"]), ("reopened", "operator", {"text": "needs the edge case"}))
        self.assertEqual(self.status(d), "queued")
        # an old brief_approved exists, but it is not a new approval
        r = self.depot("dispatch", d.name)
        self.assertEqual(r.returncode, 2)
        self.assertIn("no brief_approved event since it was reopened", r.stderr)
        # the daemon does not pick it up either
        self.depot("poll", "--once")
        self.assertFalse((d / "runs").exists())
        self.assertNotIn("dispatched", [x["type"] for x in self.events(d)[6:]])
        # only a new brief_approved makes it dispatchable again
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        self.assertEqual(self.status(d), "approved")
        self.depot("poll", "--once")
        self.assertEqual(len(list((d / "runs").iterdir())), 1)

    def test_reopen_refused_on_every_non_done_status_and_appends_nothing(self):
        trails = {
            "queued": [],
            "approved": [("brief_approved", "operator", {})],
            "dispatched": [("brief_approved", "operator", {}), ("dispatched", "turntable", {"run_id": "R0"})],
            "returned": [("brief_approved", "operator", {}), ("dispatched", "turntable", {"run_id": "R0"}),
                         ("run_finished", "turntable", {"exit_code": 0})],
            "blocked": [("brief_approved", "operator", {}), ("dispatched", "turntable", {"run_id": "R0"}),
                        ("input_required", "codex", {"question": "Q?", "run_id": "R0"})],
            "verified": [("brief_approved", "operator", {}), ("dispatched", "turntable", {"run_id": "R0"}),
                         ("run_finished", "turntable", {"exit_code": 0}), ("verified", "turntable", {"result": "pass"})],
        }
        for status, trail in trails.items():
            d = self.new_item(f"x{status}")
            for t, a, kw in trail:
                self.ev(d, t, a, **kw)
            self.assertEqual(self.status(d), status)
            before = self.snapshot()
            r = self.depot("reopen", d.name, "no")
            self.assertEqual(r.returncode, 2, status)
            self.assertIn(f"status is {status}", r.stderr)
            self.assertEqual(before, self.snapshot(), status)
        # a reviewed item reopened once is Queued: it cannot be reopened twice
        d = self.done_item("twice")
        self.assertEqual(self.depot("reopen", d.name, "first").returncode, 0)
        before = self.snapshot()
        self.assertEqual(self.depot("reopen", d.name, "second").returncode, 2)
        self.assertEqual(before, self.snapshot())

    def test_reopen_text_rides_the_next_dispatch(self):
        d = self.done_item()
        self.depot("reopen", d.name, "cover the empty case")
        self.depot("approve", d.name)
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        sent = self.sent(d)
        self.assertIn("## Messages from operator", sent)
        self.assertIn("(reopened)", sent)
        self.assertIn("cover the empty case", sent)


class TestInputRequiredAnswer(ConvCase):
    def asked(self):
        d = self.approved()
        r = self.depot("dispatch", d.name, FAKE_MODE="question")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        time.sleep(1.1)  # run ids have one-second resolution; the fresh dispatch must get its own
        return d

    def test_input_required_event_carries_the_run_id(self):
        d = self.asked()
        ir = self.events(d)[-1]
        self.assertEqual(ir["type"], "input_required")
        self.assertEqual(ir["data"]["run_id"], self.run_dir(d).name)
        self.assertEqual(ir["data"]["question"], "Which file name do you want?")

    def test_answer_is_linked_then_delivered_by_a_fresh_dispatch_with_a_verbatim_addendum(self):
        d = self.asked()
        run1 = self.run_dir(d).name
        self.assertEqual(self.depot("message", d.name, MULTILINE).returncode, 0)
        msg = self.events(d)[-1]
        self.assertEqual(msg["data"], {"text": MULTILINE, "answers_input_required": run1})
        self.assertEqual(self.status(d), "blocked")  # answered is not a new status
        # not dispatchable until re-approved
        self.depot("poll", "--once")
        self.assertEqual(len(list((d / "runs").iterdir())), 1)
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        r = self.depot("poll", "--once")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        runs = sorted((d / "runs").iterdir())
        self.assertEqual(len(runs), 2)  # a fresh dispatch, not a resume
        sent = (runs[1] / "brief.dispatched.md").read_text()
        question = [e for e in self.events(d) if e["type"] == "input_required"][0]["data"]["question"]
        original = depot.strip_header((d / "brief.md").read_text())
        self.assertTrue(sent.startswith(original.rstrip("\n")))                 # the original brief comes first
        self.assertIn("## Addendum — input-required answer", sent)
        self.assertIn(f"\n\n{question}\n\n", sent)                               # question, byte-identical
        self.assertIn(f"\n\n{MULTILINE}\n", sent)                                # answer, byte-identical
        self.assertNotIn("## Messages from operator", sent)                          # the answer is not delivered twice
        self.assertEqual((runs[1] / "stdin.txt").read_text(), sent)              # what the agent got is what is recorded

    def test_other_messages_ride_beside_the_addendum_in_order(self):
        d = self.asked()
        self.depot("message", d.name, "the answer")
        self.depot("message", d.name, "second thought\nline two")
        self.depot("message", d.name, "third")
        self.depot("approve", d.name)
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        sent = self.sent(d)
        self.assertLess(sent.index("the answer"), sent.index("## Messages from operator"))
        tail = sent.split("## Messages from operator")[1]
        self.assertLess(tail.index("second thought\nline two"), tail.index("third"))
        self.assertNotIn("the answer", tail)
        # only the first message after the question is the answer
        links = [e["data"].get("answers_input_required") for e in self.events(d) if e["type"] == "message"]
        self.assertEqual([bool(x) for x in links], [True, False, False])

    def test_missing_question_refuses_dispatch_and_derives_blocked_with_the_reason(self):
        d = self.approved()
        self.ev(d, "dispatched", adapter="codex", run_id="R9")
        self.ev(d, "run_finished", exit_code=0, elapsed_s=1, timed_out=False)
        self.ev(d, "input_required", "codex", run_id="R9")  # no question text
        self.depot("message", d.name, "the answer")
        self.depot("approve", d.name)
        self.assertEqual(self.status(d), "approved")
        r = self.depot("dispatch", d.name)
        self.assertEqual(r.returncode, 2)
        self.assertIn("carries no question text", r.stderr)
        self.assertFalse((d / "runs").exists())   # nothing was dispatched
        blocked = [e for e in self.events(d) if e["type"] == "blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertIn("refusing to invent one", blocked[0]["data"]["reason"])
        self.assertEqual(self.status(d), "blocked")
        # repeating the attempt does not pile up events, and the daemon leaves it alone
        self.depot("dispatch", d.name)
        self.depot("poll", "--once")
        self.assertEqual(len([e for e in self.events(d) if e["type"] == "blocked"]), 1)
        self.assertFalse((d / "runs").exists())
        card = {c["name"]: c for c in floor.project(self.yard, NOW)["cards"]}[d.name]
        self.assertEqual(card["condition"], "blocked")
        self.assertIn("refusing to invent one", card["condition_reason"])

    def test_approving_without_answering_dispatches_with_no_addendum(self):
        d = self.asked()
        self.depot("approve", d.name)  # e.g. operator edited the brief instead of answering
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        self.assertNotIn("## Addendum", self.sent(d))


class TestQueuedMessageDelivery(ConvCase):
    def test_queued_message_rides_the_next_dispatch_verbatim_and_exactly_once(self):
        d = self.approved()
        self.depot("message", d.name, MULTILINE)
        self.depot("message", d.name, "second")
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        first = self.sent(d)
        self.assertIn("## Messages from operator", first)
        self.assertIn(MULTILINE, first)
        self.assertLess(first.index(MULTILINE), first.index("second"))   # in order
        self.assertNotIn("## Addendum", first)                           # no question was asked
        # the original brief precedes the messages, unchanged
        self.assertTrue(first.startswith(depot.strip_header((d / "brief.md").read_text()).rstrip("\n")))
        # a later dispatch starts from the newer dispatched line: nothing is re-delivered
        self.ev(d, "reviewed", "reviewer", result="pass")
        time.sleep(1.1)  # distinct run id for the second dispatch
        self.depot("reopen", d.name, "again")
        self.depot("approve", d.name)
        self.depot("message", d.name, "fresh note")
        self.depot("dispatch", d.name)  # the fake agent rewrites identical files, so verification may flag "no change"
        self.assertEqual(len(list((d / "runs").iterdir())), 2)
        second = self.sent(d)
        self.assertIn("fresh note", second)
        self.assertNotIn("second", second.split("## Messages from operator")[1])
        self.assertNotIn(MULTILINE, second)

    def test_no_messages_means_the_brief_is_sent_exactly_as_before(self):
        d = self.approved()
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        self.assertEqual(self.sent(d), depot.strip_header((d / "brief.md").read_text()))

    def test_delivery_plan_unit(self):
        T = lambda t, **kw: {"ts": "2026-10-31T00:00:00.000Z", "type": t, "actor": "x", "data": kw}  # noqa: E731
        events = [T("dispatched", run_id="R1"), T("message", text="old"), T("dispatched", run_id="R2"),
                  T("input_required", question="Q?", run_id="R2"),
                  T("message", text="A", answers_input_required="R2"), T("message", text="B")]
        plan = depot.delivery_plan(events)
        self.assertEqual((plan["answer"]["question"], plan["answer"]["message"]["data"]["text"]), ("Q?", "A"))
        self.assertEqual([m["data"]["text"] for m in plan["others"]], ["B"])   # "old" predates R2's dispatch
        self.assertIsNone(plan["problem"])
        # an answer linked to some other run is not this question's answer
        events[4] = T("message", text="A", answers_input_required="R1")
        plan = depot.delivery_plan(events)
        self.assertIsNone(plan["answer"])
        self.assertEqual([m["data"]["text"] for m in plan["others"]], ["A", "B"])


class GitYardCase(ConvCase):
    """A yard that is a git clone on main with an origin, for the records-commit rules."""

    def git(self, *a, cwd=None):
        return subprocess.run(["git", *a], cwd=cwd or self.yard, capture_output=True, text=True)

    def make_git(self):
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        (self.yard / ".gitignore").write_text("items/*/work/\nlog/poll.lock\nlog/poll.log\n")
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "t@example.invalid")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "seed")
        self.git("remote", "add", "origin", str(self.origin))
        self.assertEqual(self.git("push", "-q", "-u", "origin", "main").returncode, 0)

    def head(self, cwd=None):
        return self.git("rev-parse", "HEAD", cwd=cwd).stdout.strip()

    def origin_head(self):
        return subprocess.run(["git", "--git-dir", str(self.origin), "rev-parse", "main"], capture_output=True,
                              text=True).stdout.strip()


class TestRecordsCommit(GitYardCase):
    def test_message_commits_exactly_the_two_logs_and_pushes(self):
        d = self.approved()
        self.make_git()
        r = self.depot("message", d.name, "hello")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.git("log", "-1", "--format=%s").stdout.strip(), f"depot: message on {d.name}")
        files = self.git("show", "--name-only", "--format=", "HEAD").stdout.split()
        self.assertEqual(sorted(files), sorted([f"items/{d.name}/events.jsonl", "log/events.jsonl"]))
        self.assertEqual(self.head(), self.origin_head())                 # pushed
        self.assertEqual(self.git("status", "--porcelain").stdout, "")    # nothing left dirty

    def test_reopen_commit_message_and_files(self):
        d = self.done_item()
        self.make_git()
        self.assertEqual(self.depot("reopen", d.name, "again").returncode, 0)
        self.assertEqual(self.git("log", "-1", "--format=%s").stdout.strip(), f"depot: reopen {d.name}")
        self.assertEqual(self.head(), self.origin_head())
        self.assertEqual(self.git("status", "--porcelain").stdout, "")    # the waybill cache is not touched

    def test_refused_on_a_dirty_tree_and_nothing_appended(self):
        d = self.approved()
        self.make_git()
        (self.yard / "stray.txt").write_text("x")
        before_events, head = (d / "events.jsonl").read_bytes(), self.head()
        r = self.depot("message", d.name, "hello")
        self.assertEqual(r.returncode, 2)
        self.assertIn("uncommitted changes", r.stderr)
        self.assertEqual((d / "events.jsonl").read_bytes(), before_events)
        self.assertEqual(self.head(), head)

    def test_refused_on_a_non_main_checkout_for_both_commands(self):
        d = self.done_item()
        self.make_git()
        self.git("checkout", "-q", "-b", "feature")
        before = self.snapshot()
        for args in (("message", d.name, "x"), ("reopen", d.name, "x")):
            r = self.depot(*args)
            self.assertEqual(r.returncode, 2)
            self.assertIn("main only", r.stderr)
        self.assertEqual(before, self.snapshot())

    def test_push_failure_keeps_the_local_commit_reports_and_exits_nonzero(self):
        d = self.approved()
        self.make_git()
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho rejected-by-test >&2\nexit 1\n")
        hook.chmod(0o755)
        remote_before = self.origin_head()
        r = self.depot("message", d.name, "hello")
        self.assertEqual(r.returncode, 1)
        self.assertIn("push failed", r.stderr)
        self.assertEqual(self.origin_head(), remote_before)                       # nothing forced through
        self.assertEqual(self.git("log", "-1", "--format=%s").stdout.strip(), f"depot: message on {d.name}")
        self.assertEqual(self.events(d)[-1]["type"], "message")                   # the event was not dropped
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_pulls_fast_forward_before_appending(self):
        d = self.approved()
        self.make_git()
        other = self.tmp / "other"
        self.git("clone", "-q", str(self.origin), str(other), cwd=self.tmp)
        self.git("config", "user.name", "T", cwd=other)
        self.git("config", "user.email", "t@example.invalid", cwd=other)
        (other / "NOTE").write_text("upstream moved")
        self.git("add", "-A", cwd=other)
        self.git("commit", "-q", "-m", "upstream", cwd=other)
        self.git("push", "-q", cwd=other)
        self.assertEqual(self.depot("message", d.name, "hello").returncode, 0)
        self.assertTrue((self.yard / "NOTE").exists())
        self.assertEqual(self.head(), self.origin_head())

    def test_a_blocked_dispatch_is_committed_by_the_daemon(self):
        d = self.approved()
        self.ev(d, "dispatched", adapter="codex", run_id="R9")
        self.ev(d, "run_finished", exit_code=0, elapsed_s=1, timed_out=False)
        self.ev(d, "input_required", "codex", run_id="R9")
        self.depot("message", d.name, "the answer")
        self.depot("approve", d.name)
        self.make_git()
        self.depot("poll", "--once")
        self.assertEqual(self.git("log", "-1", "--format=%s").stdout.strip(), f"depot: {d.name} blocked")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")


# ---- the floor ----

def E(mins_ago, type_, actor="turntable", **data):
    return {"ts": floor.iso_z(NOW - timedelta(minutes=mins_ago)), "type": type_, "actor": actor, "data": data}


class FloorCase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        (self.root / "log").mkdir()
        (self.root / "log" / "events.jsonl").write_text("")
        self.n = 0

    def item(self, events, slug="thing", **wb):
        self.n += 1
        name = f"{self.n:04d}-{slug}"
        d = self.root / "items" / name
        d.mkdir(parents=True)
        base = {"id": f"{self.n:04d}", "slug": slug, "title": f"Title {slug}", "track": "t",
                "adapter": "codex", "quiet_after_s": 120}
        base.update(wb)
        (d / "waybill.json").write_text(json.dumps(base))
        (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        return name

    def card(self, name, now=NOW):
        return {c["name"]: c for c in floor.project(self.root, now)["cards"]}[name]


DONE = [E(100, "item_created", "operator"), E(99, "brief_approved", "operator"),
        E(98, "dispatched", adapter="codex", run_id="R0"),
        E(90, "run_finished", exit_code=0, elapsed_s=30, timed_out=False), E(90, "claimed", "codex", report="r"),
        E(89, "verified", result="pass", discrepancies=[], test_exit_code=0),
        E(30, "reviewed", "reviewer", result="pass")]
ACTIVE = DONE[:3] + [E(1, "heartbeat")]
ASKED = DONE[:5] + [E(80, "input_required", "codex", question="Which file name do you want?", run_id="R0")]


class TestFloorConversation(FloorCase):
    def test_done_card_with_a_later_message_stays_done_and_shows_the_annotation(self):
        # spec §9: a message on a Done card is annotation only
        bare = self.card(self.item(DONE, slug="bare"))
        c = self.card(self.item(DONE + [E(10, "message", "operator", text="Remember to rotate the key.")], slug="noted"))
        for k in ("stage", "hands", "stage_since", "dwell_s", "truth", "checker", "condition", "last_event"):
            self.assertEqual(c[k], bare[k], k)
        self.assertEqual((c["stage"], c["truth"], c["checker"], c["dwell_s"]), ("Done", "verified", "reviewer", 30 * 60))
        self.assertEqual(c["annotation"], {"type": "message", "author": "operator", "ts": floor.iso_z(NOW - timedelta(minutes=10)),
                                           "text": "Remember to rotate the key."})
        self.assertIsNone(bare["annotation"])
        self.assertEqual([t["type"] for t in c["trail"]][-1], "message")  # and it is in the path

    def test_message_on_an_active_card_changes_no_stage_hands_or_truth(self):
        bare = self.card(self.item(ACTIVE, slug="bare"))
        c = self.card(self.item(ACTIVE + [E(0.5, "message", "operator", text="also handle empty input")], slug="noted"))
        for k in ("stage", "hands", "dwell_s", "truth", "checker", "condition", "last_event", "heartbeats"):
            self.assertEqual(c[k], bare[k], k)
        self.assertEqual(c["annotation"]["text"], "also handle empty input")

    def test_newest_message_wins_and_long_text_is_clipped(self):
        long = "word " * 80
        c = self.card(self.item(DONE + [E(20, "message", "operator", text="first"),
                                        E(10, "message", "operator", text=long)]))
        self.assertEqual(len(c["annotation"]["text"]), floor.ANNOTATION_LIMIT)
        self.assertTrue(c["annotation"]["text"].endswith("…"))
        self.assertNotIn("\n", c["annotation"]["text"])

    def test_reopened_returns_to_queued_with_dwell_from_the_reopen_and_the_text_as_annotation(self):
        c = self.card(self.item(DONE + [E(5, "reopened", "operator", text="cover the edge case")]))
        self.assertEqual((c["stage"], c["hands"], c["dwell_s"], c["condition"]), ("Queued", "—", 5 * 60, None))
        self.assertEqual(c["annotation"]["type"], "reopened")
        self.assertEqual(c["annotation"]["text"], "cover the edge case")
        self.assertEqual((c["last_event"]["type"], c["truth"]), ("reopened", "recorded"))
        # and it moves on only by approval, like any card
        c2 = self.card(self.item(DONE + [E(5, "reopened", "operator", text="x"), E(2, "brief_approved", "operator")], slug="b"))
        self.assertEqual((c2["stage"], c2["hands"], c2["dwell_s"]), ("Briefed", "Turntable", 2 * 60))

    def test_reopened_without_a_done_card_is_a_log_discrepancy_not_a_guess(self):
        name = self.item(ACTIVE + [E(0.5, "reopened", "operator", text="x")])
        with self.assertRaises(floor.FloorError):
            self.card(name)

    def test_input_required_card_shows_the_question_text(self):
        c = self.card(self.item(ASKED))
        self.assertEqual((c["stage"], c["hands"]), ("Awaiting operator", "operator"))
        self.assertEqual(c["question"], "Which file name do you want?")
        self.assertIsNone(self.card(self.item(DONE, slug="plain"))["question"])

    def test_an_answered_card_leaves_awaiting_operator_without_a_false_stall(self):
        c = self.card(self.item(ASKED + [E(3, "message", "operator", text="use foo.py", answers_input_required="R0")]))
        self.assertNotEqual(c["stage"], "Awaiting operator")
        self.assertIsNone(c["question"])
        self.assertIsNone(c["condition"])           # a run that asked is not a run that stalled
        self.assertEqual(c["annotation"]["text"], "use foo.py")
        self.assertEqual(c["last_event"]["type"], "input_required")  # the message is not the newest fact
        # re-approval then a fresh dispatch: Briefed, then Building (the path 0004 takes)
        c2 = self.card(self.item(ASKED + [E(3, "message", "operator", text="use foo.py", answers_input_required="R0"),
                                          E(2, "brief_approved", "operator")], slug="b"))
        self.assertEqual(c2["stage"], "Briefed")
        c3 = self.card(self.item(ASKED + [E(3, "message", "operator", text="use foo.py", answers_input_required="R0"),
                                          E(2, "brief_approved", "operator"),
                                          E(1, "dispatched", adapter="codex", run_id="R1")], slug="c"))
        self.assertEqual((c3["stage"], c3["hands"]), ("Building", "Codex"))
        self.assertEqual(c3["annotation"]["text"], "use foo.py")

    def test_a_stall_without_a_question_is_still_flagged(self):
        c = self.card(self.item(DONE[:3]))  # dispatched 98 minutes ago, nothing since
        self.assertEqual(c["condition"], "stalled?")

    def test_blocked_event_sets_the_condition_with_its_reason(self):
        c = self.card(self.item(ASKED[:5] + [E(80, "input_required", "codex", run_id="R0"),
                                             E(3, "message", "operator", text="a", answers_input_required="R0"),
                                             E(2, "brief_approved", "operator"),
                                             E(1, "blocked", reason="no question text")]))
        self.assertEqual((c["condition"], c["condition_reason"]), ("blocked", "no question text"))

    def test_serving_an_annotated_yard_writes_nothing(self):
        self.item(DONE + [E(10, "message", "operator", text="note")], slug="done")
        self.item(DONE + [E(5, "reopened", "operator", text="again")], slug="re")
        self.item(ASKED, slug="asked")
        snap = lambda: {str(p): hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
                        for p in sorted(self.root.rglob("*")) if p.is_file()}
        before = snap()
        srv = floor.make_server(self.root, 0, now_fn=lambda: NOW)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/cards.json"
            with urllib.request.urlopen(url) as r:
                cards = json.loads(r.read())["cards"]
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual({c["id"]: c["stage"] for c in cards}, {"0001": "Done", "0002": "Queued", "0003": "Awaiting operator"})
        self.assertEqual(before, snap())  # byte-identical

    def test_page_renders_annotation_and_question_read_only(self):
        js = (Path(floor.PAGE_DIR) / "floor.js").read_text()
        for needle in ("card.annotation", "card.question", "a.text"):
            self.assertIn(needle, js)
        # S8: the page may now POST an intent (pinned in test_floor.TestPageWritesOnlyIntents), so "method:" and
        # "POST" are no longer forbidden here; the page still ships no form or literal textarea markup.
        for forbidden in ("<form", "<textarea"):
            self.assertNotIn(forbidden, js + (Path(floor.PAGE_DIR) / "index.html").read_text())


if __name__ == "__main__":
    unittest.main()
