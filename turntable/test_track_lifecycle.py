"""S11: track log, daemon dispatch gate, atomic end/revival records, and unchanged card projection."""
import json
import subprocess
import threading
import unittest
from datetime import datetime, timezone
from unittest import mock

import depot
import floor
from test_panel import PanelCase
from test_intake import form


class TrackCase(PanelCase):
    def act(self, kind, track="tr", process=True):
        code, body = floor.drop_intent(self.yard, {"kind": "track_" + kind, "data": {"track": track}}, self.clock)
        self.assertEqual(code, 202)
        if process:
            self.process()
        return body["id"]

    def state(self, track="tr"):
        return floor.track_state(self.yard, track)

    def board(self):
        return floor.project(self.yard, datetime.now(timezone.utc))

    def lane(self, track="tr"):
        return next(t for t in self.board()["tracks"] if t["id"] == track)

    def create(self, track="tr"):
        return floor.drop_intent(self.yard, {"kind": "create", "data": form(track=track)}, self.clock)[1]["id"]

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.yard, capture_output=True, text=True, check=True).stdout.strip()

    def seed_git(self):
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        (self.yard / ".gitignore").write_text("log/intents/\nlog/poll.log\nitems/*/work/\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "seed")

    def snapshot(self):
        return {str(p.relative_to(self.yard)): p.read_bytes() for p in self.yard.rglob("*")
                if p.is_file() and "intents" not in p.parts and ".git" not in p.parts and p.name != "poll.log"}


class TestTrackState(TrackCase):
    def test_no_log_is_active_and_does_not_write(self):
        self.new_item()
        before = self.snapshot()
        self.assertEqual(self.state(), "active")
        self.assertEqual(self.lane()["state"], "active")
        self.assertEqual(self.snapshot(), before)

    def test_all_six_events_latest_wins_no_global_lines(self):
        self.new_item()
        before = len(self.glog())
        for act, event, state in [("pause", "track_paused", "paused"), ("resume", "track_resumed", "active"),
                                  ("table", "track_tabled", "tabled"), ("untable", "track_untabled", "active"),
                                  ("end", "track_ended", "ended"), ("reopen", "track_reopened", "active")]:
            # Done-only end proves all six lifecycle events add zero global-log lines.
            if act == "end":
                depot.Item(next((self.yard / "items").iterdir())).emit("reviewed", "reviewer", {"result": "pass"})
                before += 1
            iid = self.act(act)
            self.assertEqual(self.state(), state)
            e = floor.read_events(floor.track_log(self.yard, "tr"))[-1]
            self.assertEqual((e["track"], e["type"], e["actor"], e["data"]),
                             ("tr", event, "operator", {"via": "floor", "intent": iid}))
            self.assertNotIn("item", e)
            self.assertEqual(len(self.glog()), before)

    def test_invalid_transitions_unknown_track_and_path_names_refused(self):
        self.new_item()
        before = self.snapshot()
        for act in ("resume", "untable", "reopen"):
            self.assertEqual(self.outcome(self.act(act))["state"], "refused")
        self.assertEqual(self.outcome(self.act("pause", "absent"))["state"], "refused")
        for name in ("../escape", "/absolute", "", "x/y"):
            with self.assertRaises(floor.IntentInvalid):
                floor.validate_intent({"kind": "track_end", "data": {"track": name}})
        self.assertEqual(self.snapshot(), before)

    def test_state_is_only_log_derived_and_card_derivation_unchanged(self):
        d = self.new_item()
        before = self.card(d)
        self.act("pause")
        after = self.card(d)
        self.assertEqual(before, after)
        floor.track_log(self.yard, "tr").unlink()
        self.assertEqual(self.state(), "active")

    def test_receipts_pending_landed_reconciled_and_not_board_or_card_scoped(self):
        d = self.new_item()
        iid = self.act("pause", process=False)
        self.assertEqual(self.lane()["receipts"][0]["state"], "pending")
        self.process()
        self.assertEqual(self.lane()["receipts"][0]["state"], "landed")
        self.assertEqual(self.board()["board_receipts"], [])
        self.assertEqual(self.card(d)["receipts"], [])
        floor.outcome_path(self.yard, iid).unlink()
        self.assertEqual(self.lane()["receipts"][0]["state"], "reconciling")
        self.process()
        self.assertTrue(self.outcome(iid)["reconciled"])
        self.assertEqual(len(floor.read_events(floor.track_log(self.yard, "tr"))), 1)

    def test_lanes_report_shelf_archive_counts_and_include_empty_configured_track(self):
        d = self.new_item()
        depot.Item(d).emit("reviewed", "reviewer", {"result": "pass"})
        (self.yard / "tracks.json").write_text('{"empty": {"timeout_s": 50}}')
        self.assertEqual(self.lane("empty")["card_count"], 0)
        self.assertEqual(self.lane()["done_count"], 1)
        self.act("table")
        self.assertTrue(self.lane()["shelf"])
        self.assertFalse(self.lane()["archive"])
        self.act("end")
        self.assertTrue(self.lane()["archive"])
        self.assertFalse(self.lane()["shelf"])
        self.assertEqual(self.card(d)["stage"], "Done")


class TestTrackDispatch(TrackCase):
    def test_track_end_park_survives_reopen_and_reapproval_until_unparked(self):
        for dispatch in ("cli", "poll"):
            with self.subTest(dispatch=dispatch):
                d = self.new_item(dispatch)
                self.assertEqual(self.depot("approve", d.name).returncode, 0)
                self.act("end")
                self.act("reopen")
                self.assertEqual(self.state(), "active")
                self.assertEqual(self.card(d)["parked"], "track ended")
                self.assertFalse(self.card(d)["actions"]["approve"]["ok"])
                self.assertIn("unpark it first", self.card(d)["actions"]["approve"]["why"])
                # CLI approval can land while parked; it must not release the dispatch hold.
                self.assertEqual(self.depot("approve", d.name).returncode, 0)
                before = self.snapshot()
                refusals = {}
                for _ in range(depot.REFUSAL_CAP + 1):
                    if dispatch == "cli":
                        result = self.depot("dispatch", d.name)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("parked", result.stderr)
                    else:
                        with mock.patch.object(depot, "poll_log", self.logs.append):
                            depot.poll_cycle(threading.Event(), refusals)
                self.assertEqual(refusals, {})
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.card(d)["parked"], "track ended")
                self.assertNotIn("dispatched", self.types(d))
                self.assertFalse((d / "runs").exists())
                self.assertFalse((d / "work").exists())

                before_types = self.types(d)
                iid = self.drop("unpark", d.name)
                self.process()
                self.assertEqual(self.outcome(iid)["state"], "landed")
                self.assertEqual(self.types(d), before_types + ["unparked"])
                self.assertIsNone(self.card(d)["parked"])
                self.assertEqual(depot.Item(d).derived_status(), "approved")
                if dispatch == "cli":
                    result = self.depot("dispatch", d.name)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                else:
                    self.poll_once()
                self.assertIn("dispatched", self.types(d))

                # Keep the next subtest's end sweep free of this completed run.
                depot.Item(d).emit("reviewed", "reviewer", {"result": "pass"})

    def test_never_parked_approved_item_dispatches_in_poll(self):
        d = self.new_item()
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        self.poll_once()
        self.assertIn("dispatched", self.types(d))

    def test_unpark_does_not_approve_and_a_later_park_holds_again(self):
        d = self.new_item()
        self.drop("park", d.name, reason="first hold")
        self.process()
        self.drop("unpark", d.name)
        self.process()
        self.poll_once()
        self.assertEqual(depot.Item(d).derived_status(), "queued")
        self.assertNotIn("dispatched", self.types(d))
        self.drop("park", d.name, reason="second hold")
        self.process()
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        before = self.snapshot()
        self.poll_once()
        result = self.depot("dispatch", d.name)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("parked", result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.card(d)["parked"], "second hold")

    def test_paused_approval_lands_waits_two_cycles_and_resume_dispatches(self):
        d = self.new_item()
        before = len(self.glog())
        self.act("pause")
        self.drop("approve", d.name)
        self.poll_once()
        self.poll_once()
        self.assertEqual(self.types(d), ["item_created", "brief_approved"])
        self.assertFalse((d / "runs").exists())
        self.assertEqual(len(self.glog()), before + 1)
        iid = self.act("resume", process=False)
        self.poll_once()
        self.assertEqual(self.outcome(iid)["state"], "landed")
        self.assertIn("dispatched", self.types(d))
        self.assertEqual(self.logs.count("skipping track tr: paused"), 2)

    def test_tabled_ended_skip_and_one_poll_line_per_track_not_per_item(self):
        for slug in ("one", "two"):
            d = self.new_item(slug)
            self.drop("approve", d.name)
        self.process()
        self.act("table")
        before = self.snapshot()
        self.poll_once()
        self.assertEqual(self.logs.count("skipping track tr: tabled"), 1)
        self.assertEqual(self.snapshot(), before)
        self.act("end")
        # Per-item unpark/approval still land but cannot bypass an ended track's dispatch gate.
        for it in depot.all_items():
            self.drop("unpark", it.name)
            self.drop("approve", it.name)
        self.process()
        count = len(self.glog())
        self.poll_once()
        self.assertEqual(len(self.glog()), count)
        self.assertEqual(self.logs.count("skipping track tr: ended"), 1)
        self.assertFalse(any((it.path / "runs").exists() for it in depot.all_items()))

    def test_in_flight_run_finishes_after_pause_and_other_tracks_dispatch(self):
        d = self.new_item()
        other = self.new_item("other", track="other")
        for item in (d, other):
            self.drop("approve", item.name)
        self.process()
        seen = []
        def dispatch(it):
            seen.append(it.name)
            if it.name == d.name:
                self.act("pause")
                it.emit("dispatched", "codex", {})
                it.emit("run_finished", "codex", {"exit_code": 0})
            return 0, "pass"
        with mock.patch.object(depot, "dispatch_item", side_effect=dispatch):
            self.poll_once()
        self.assertEqual(seen, [d.name, other.name])
        self.assertEqual(self.types(d)[-1], "run_finished")


class TestEnd(TrackCase):
    def test_refuses_naming_building_gating_review_and_writes_nothing(self):
        for slug, kind, review in [("building", "build", False), ("gating", "gate", False), ("review", "build", True)]:
            d = self.new_item(slug, kind=kind)
            it = depot.Item(d)
            it.emit("brief_approved", "operator")
            it.emit("dispatched", "codex", {})
            if review:
                it.emit("run_finished", "codex", {})
                it.emit("verified", "turntable", {"result": "pass"})
        before = self.snapshot()
        out = self.outcome(self.act("end"))
        self.assertEqual(out["state"], "refused")
        for it in depot.all_items():
            self.assertIn(it.name, out["reason"])
        self.assertEqual(self.snapshot(), before)

    def test_sweep_exact_open_items_one_commit_reopen_keeps_parks_and_budgets(self):
        queued = self.new_item("queued")
        briefed = self.new_item("briefed")
        depot.Item(briefed).emit("brief_approved", "operator")
        awaiting = self.new_item("awaiting")
        for typ, data in [("brief_approved", {}), ("dispatched", {}), ("input_required", {"question": "Which?"})]:
            depot.Item(awaiting).emit(typ, "operator", data)
        done = self.new_item("done")
        depot.Item(done).emit("reviewed", "reviewer", {"result": "pass"})
        other = self.new_item("other", track="other")
        parked = self.new_item("parked")
        depot.Item(parked).emit("parked", "operator", {"reason": "earlier hold"})
        (self.yard / "tracks.json").write_text('{"tr": {"timeout_s": 500, "quiet_after_s": 20}}')
        budgets = (self.yard / "tracks.json").read_bytes()
        untouched = {p: (p / "events.jsonl").read_bytes() for p in (done, other)}
        self.seed_git()
        start = self.git("rev-parse", "HEAD")
        count = len(self.glog())
        iid = self.act("end", process=False)
        depot.process_intents(True)
        self.assertEqual(self.git("rev-list", "--count", f"{start}..HEAD"), "1")
        paths = self.git("show", "--name-only", "--format=").splitlines()
        self.assertIn("tracks/tr/events.jsonl", paths)
        self.assertIn("log/events.jsonl", paths)
        for p in (queued, briefed, awaiting, parked):
            self.assertIn(str(p.relative_to(self.yard) / "events.jsonl"), paths)
            last = self.events(p)[-1]
            self.assertEqual((last["type"], last["data"]["reason"], last["data"]["intent"]), ("parked", "track ended", iid))
        self.assertEqual(len(self.glog()), count + 4)  # only mirrored item parks, never track events
        for p, raw in untouched.items():
            self.assertEqual((p / "events.jsonl").read_bytes(), raw)
        self.assertEqual(self.state(), "ended")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.act("reopen")
        self.assertEqual(self.state(), "active")
        for p in (queued, briefed, awaiting, parked):
            self.assertEqual(self.card(p)["parked"], "track ended")
        self.assertEqual((self.yard / "tracks.json").read_bytes(), budgets)

    def test_append_failure_restores_end_and_sweep_then_retry_succeeds(self):
        self.new_item("one")
        self.new_item("two")
        before = self.snapshot()
        iid = self.act("end", process=False)
        with mock.patch.object(depot, "emit_track", side_effect=OSError("disk full")):
            self.process()
        self.assertEqual(self.outcome(iid)["state"], "failed")
        self.assertEqual(self.snapshot(), before)
        obj = json.loads(floor.intent_path(self.yard, iid).read_text())
        floor.drop_intent(self.yard, obj, self.clock)
        self.process()
        self.assertEqual(self.outcome(iid)["state"], "landed")
        self.assertEqual(self.state(), "ended")

    def test_records_commit_failure_leaves_intent_pending_and_reconciles_one_commit(self):
        self.new_item()
        self.seed_git()
        iid = self.act("end", process=False)
        with mock.patch.object(depot, "yard_commit_message", side_effect=depot.DepotError("commit failed")) as commit:
            depot.process_intents(True)
        self.assertEqual(commit.call_count, 1)
        self.assertIsNone(self.outcome(iid))
        depot.process_intents(True)
        self.assertEqual(self.outcome(iid)["state"], "landed")
        self.assertEqual(len([e for e in self.glog() if e["type"] == "parked"]), 1)
        self.assertEqual(self.git("status", "--porcelain"), "")


class TestLifecycleIntake(TrackCase):
    def test_paused_intake_is_queued_and_separate_approval_waits(self):
        self.new_item()
        self.act("pause")
        iid = self.create()
        self.poll_once()
        made = self.yard / "items" / self.outcome(iid)["item"]
        self.assertEqual(self.types(made), ["item_created"])
        self.drop("approve", made.name)
        self.poll_once()
        self.assertEqual(self.types(made), ["item_created", "brief_approved"])
        self.assertEqual(self.state(), "paused")

    def test_tabled_intake_revives_and_creates_in_one_commit_with_shared_intent(self):
        self.new_item()
        self.act("table")
        self.seed_git()
        start = self.git("rev-parse", "HEAD")
        before = len(self.glog())
        iid = self.create()
        depot.process_intents(True)
        out = self.outcome(iid)
        self.assertEqual(self.state(), "active")
        self.assertEqual(self.git("rev-list", "--count", f"{start}..HEAD"), "1")
        paths = self.git("show", "--name-only", "--format=").splitlines()
        self.assertIn("tracks/tr/events.jsonl", paths)
        self.assertIn(f"items/{out['item']}/events.jsonl", paths)
        self.assertEqual(floor.read_events(floor.track_log(self.yard, "tr"))[-1]["data"]["intent"], iid)
        self.assertEqual(len(self.glog()), before + 1)
        self.assertEqual(self.glog()[-1]["type"], "item_created")
        self.assertEqual(depot.Item(self.yard / "items" / out["item"]).derived_status(), "queued")

    def test_ended_intake_refused_without_any_record_write(self):
        self.new_item()
        self.act("end")
        before = self.snapshot()
        iid = self.create()
        self.process()
        self.assertEqual(self.outcome(iid)["state"], "refused")
        self.assertIn("track ended — reopen it from the Archive", self.outcome(iid)["reason"])
        self.assertEqual(self.snapshot(), before)

    def test_interrupted_revival_finishes_before_creation_records_commit(self):
        self.new_item()
        self.act("table")
        self.seed_git()
        iid = self.create()
        # Simulate a process loss after item_created, before its paired track event and records commit.
        def interrupt(*args):
            raise KeyboardInterrupt()
        with mock.patch.object(depot, "emit_track", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.process()
        self.assertIsNone(self.outcome(iid))
        self.assertEqual(self.state(), "tabled")
        depot.process_intents(True)
        self.assertEqual(self.state(), "active")
        self.assertTrue(self.outcome(iid)["reconciled"])
        self.assertEqual(len([e for e in self.glog() if e["type"] == "item_created" and e["data"].get("intent") == iid]), 1)
        self.assertIn("tracks/tr/events.jsonl", self.git("show", "--name-only", "--format=").splitlines())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_failed_revival_rolls_back_created_item_and_global_event(self):
        self.new_item()
        self.act("table")
        before = self.snapshot()
        iid = self.create()
        with mock.patch.object(depot, "emit_track", side_effect=OSError("disk full")):
            self.process()
        self.assertEqual(self.outcome(iid)["state"], "failed")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.state(), "tabled")


if __name__ == "__main__":
    unittest.main()
