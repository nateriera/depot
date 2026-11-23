"""Interrupted runs stay operator-owned until explicit recovery and a fresh approval."""
import argparse
import os
import shutil
import subprocess
import unittest
import threading
import uuid
from datetime import timedelta
from unittest import mock

import depot
import floor
from test_floor import YardCase, E, NOW, lifecycle, ran


class TestInterrupted(YardCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"DEPOT_YARD": str(self.root), "DEPOT_VAULT": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def unfinished(self, minutes=98, timeout=900):
        events = lifecycle(dispatched=minutes)
        events[-1]["data"].update(run_id="run1", timeout_s=timeout)
        name = self.item(events)
        item = depot.Item(self.root / "items" / name)
        (item.path / "brief.md").write_text('```depot\n{"expected_files": [], "test_command": ""}\n```\nBuild it.\n')
        return name, item

    def act(self, name, kind, **data):
        if kind == "approve" and "contract_fingerprint" not in data:
            try:
                data.update(depot.approval_data(depot.Item(self.root / "items" / name)))
            except depot.DepotError:
                pass  # invalid briefs are tested at landing
        return depot.apply_intent(floor, {"id": str(uuid.uuid4()), "item": name, "kind": kind, "data": data})

    def cycle(self):
        with mock.patch.object(depot, "poll_log"), mock.patch.object(depot, "dispatch_item") as dispatch:
            depot.poll_cycle(threading.Event())
        return dispatch

    def test_startup_cycle_marks_unfinished_run_interrupted_and_never_recovers(self):
        name, item = self.unfinished()
        self.cycle().assert_not_called()
        card = self.card(name)
        self.assertEqual((item.derived_status(), item.waybill()["status"]), ("interrupted", "interrupted"))
        self.assertEqual((card["stage"], card["hands"], card["condition"]), ("Interrupted", "operator", "interrupted"))
        self.assertFalse(card["posture"]["live"]["running"])
        self.assertTrue(card["actions"]["requeue_interrupted"]["ok"])
        for action in ("approve", "park", "adjudicate", "recover"):
            self.assertFalse(card["actions"][action]["ok"])
        before = item.events()
        self.cycle().assert_not_called()
        self.assertEqual(item.events(), before)
        self.assertEqual(before[-1]["data"]["run_id"], "run1")

    def test_recovery_needs_current_run_and_note_then_separate_approval(self):
        name, item = self.unfinished()
        self.cycle()
        for rid, note in (("old", "Inspected worker"), ("run1", " ")):
            with self.assertRaises(depot.IntentRefused):
                self.act(name, "requeue_interrupted", run_id=rid, note=note)
        intent = {"id": str(uuid.uuid4()), "item": name, "kind": "requeue_interrupted",
                  "data": {"run_id": "run1", "note": "Old worker stopped; retry after review"}}
        depot.apply_intent(floor, intent)
        before = item.events()
        depot.apply_intent(floor, intent)  # interrupted intent drain is idempotent
        self.assertEqual(before, item.events())
        self.assertEqual((item.derived_status(), self.card(name)["stage"]), ("queued", "Queued"))
        self.assertEqual(before[-1]["actor"], "operator")
        self.assertEqual(before[-1]["data"]["note"], intent["data"]["note"])
        self.cycle().assert_not_called()
        with self.assertRaises(depot.DepotError):
            depot.dispatch_item(item)  # an old approval cannot authorize the recovered run
        self.act(name, "approve")
        self.assertEqual(item.derived_status(), "approved")
        with mock.patch.object(depot, "dispatch_item", return_value=(0, "returned")) as dispatch:
            depot.poll_cycle(threading.Event())
        self.assertEqual(dispatch.call_args.args[0].name, name)

    def test_interrupted_refuses_approval_parking_and_direct_cli_bypasses(self):
        name, item = self.unfinished()
        self.cycle()
        for kind, data in (("approve", {}), ("park", {"reason": "later"})):
            with self.assertRaises(depot.IntentRefused):
                self.act(name, kind, **data)
        with self.assertRaises(depot.DepotError):
            depot.cmd_approve(argparse.Namespace(item=name))
        with self.assertRaises(depot.DepotError):
            depot.dispatch_item(item)

    def test_bound_uses_original_timeout_and_ignores_recent_heartbeats(self):
        name, item = self.unfinished(minutes=21)
        # 900s run budget + 300s cleanup grace; quiet activity cannot extend it.
        item.emit("heartbeat", "turntable", {"elapsed_s": 1260, "since_last_adapter_event_s": 0})
        depot.reconcile_unfinished_runs(now=NOW)
        self.assertEqual(item.derived_status(), "interrupted")
        recent, active = self.unfinished(minutes=19)
        depot.reconcile_unfinished_runs(now=NOW)
        self.assertEqual(active.derived_status(), "dispatched")
        self.assertTrue(self.card(recent)["posture"]["live"]["running"])
        depot.reconcile_unfinished_runs(now=NOW + timedelta(minutes=2))
        self.assertEqual(active.derived_status(), "interrupted")

    def test_finished_runs_and_recovery_on_live_or_finished_runs_are_unaffected(self):
        live, _ = self.unfinished(minutes=1)
        finished = self.item(lifecycle() + ran() + [E(89, "verified", result="pass")])
        timed_out = self.item(lifecycle() + [E(89, "run_finished", timed_out=True, exit_code=124)])
        before = self.yard_hashes()
        depot.reconcile_unfinished_runs(now=NOW)
        self.assertEqual(before, self.yard_hashes())
        self.assertEqual(self.card(finished)["stage"], "Review")
        self.assertFalse(self.card(timed_out)["posture"]["live"]["running"])
        for name in (live, finished, timed_out):
            self.assertFalse(self.card(name)["actions"]["requeue_interrupted"]["ok"])
            with self.assertRaises(depot.IntentRefused):
                self.act(name, "requeue_interrupted", run_id="run1", note="Retry")

    def test_new_dispatch_after_finished_run_is_reconciled_independently(self):
        events = lifecycle() + ran() + [E(50, "brief_approved"), E(49, "dispatched", run_id="run2", timeout_s=900)]
        name = self.item(events)
        depot.reconcile_unfinished_runs(now=NOW)
        card = self.card(name)
        self.assertEqual(card["stage"], "Interrupted")
        self.assertEqual(card["posture"]["live"]["run_id"], "run2")

    def test_interrupted_track_cannot_be_ended_or_swept_into_parking(self):
        name, item = self.unfinished()
        self.cycle()
        projected = floor.project(self.root, NOW)
        self.assertIn(name, projected["tracks"][0]["pipeline"])
        with self.assertRaises(depot.IntentRefused):
            depot.apply_intent(floor, {"id": str(uuid.uuid4()), "kind": "track_end",
                                       "item": None, "data": {"track": "t"}})
        self.assertEqual(item.derived_status(), "interrupted")
        self.assertIsNone(self.card(name)["parked"])

    @unittest.skipUnless(shutil.which("node"), "node is not available")
    def test_floor_recovery_button_requires_note_and_sends_explicit_intent(self):
        result = subprocess.run(["node", str(floor.PAGE_DIR / "test_blocked.cjs"), "interrupted"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
