"""Operator acceptance and controls arriving during an actual dispatch."""
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

import depot
import floor
from test_panel import PanelCase
from test_intake import NODE


class TestReview(PanelCase):
    def ready(self, slug="review"):
        d = self.new_item(slug)
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        r = self.depot("dispatch", d.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        return d, self.card(d)["review_commit"]

    def test_floor_pass_records_verified_commit_and_reaches_done(self):
        d, commit = self.ready()
        self.assertEqual(len(commit), 40)
        iid = self.drop("review", d.name, result="pass", note="Meets the brief", commit=commit)
        self.process()
        self.assertEqual(self.outcome(iid)["state"], "landed")
        e = self.events(d)[-1]
        self.assertEqual((e["type"], e["actor"], e["data"]["commit"], e["data"]["note"]),
                         ("reviewed", "operator", commit, "Meets the brief"))
        self.assertEqual(self.card(d)["stage"], "Done")
        self.assertEqual(self.card(d)["checker"], "operator")
        self.assertEqual(depot.Item(d).derived_status(), "reviewed")
        self.process()
        self.assertEqual(sum(e["type"] == "reviewed" for e in self.events(d)), 1)

    def test_cli_pass_and_fail_bind_commit_and_keep_note(self):
        for result in ("pass", "fail"):
            with self.subTest(result=result):
                d, commit = self.ready(result)
                r = self.depot("review", d.name, result, "Human review detail", "--commit", commit)
                self.assertEqual(r.returncode, 0, r.stderr)
                e = self.events(d)[-1]
                self.assertEqual(e["data"], {"result": result, "note": "Human review detail", "commit": commit})
                c = self.card(d)
                if result == "pass":
                    self.assertEqual(c["stage"], "Done")
                else:
                    self.assertEqual(depot.Item(d).derived_status(), "blocked")
                    self.assertEqual(c["condition"], "blocked")
                    self.assertIn("Human review detail", c["condition_reason"])

    def test_floor_fail_blocks_with_note(self):
        d, commit = self.ready()
        iid = self.drop("review", d.name, result="fail", note="Missing edge case", commit=commit)
        self.process()
        self.assertEqual(self.outcome(iid)["state"], "landed")
        self.assertEqual(depot.Item(d).derived_status(), "blocked")
        self.assertIn("Missing edge case", self.card(d)["condition_reason"])

    def reject(self):
        d, commit = self.ready()
        iid = self.drop("review", d.name, result="fail", note="Missing edge case", commit=commit)
        self.process()
        self.assertEqual(self.outcome(iid)["state"], "landed")
        return d, commit

    def test_rejected_review_offers_approval_and_requires_fresh_dispatch(self):
        d, _ = self.reject()
        card = self.card(d)
        self.assertEqual(card["stage"], "Review")
        self.assertEqual(card["hands"], "operator")
        self.assertTrue(card["actions"]["approve"]["ok"])
        dispatches = sum(e["type"] == "dispatched" for e in self.events(d))
        iid = self.drop("approve", d.name)
        self.process()
        self.assertEqual(self.outcome(iid)["state"], "landed")
        self.assertEqual(self.card(d)["stage"], "Briefed")
        self.assertFalse(self.card(d)["actions"]["review"]["ok"])
        self.assertEqual(sum(e["type"] == "dispatched" for e in self.events(d)), dispatches)
        time.sleep(1.1)  # run IDs have one-second resolution; give the re-run a fresh ID.
        self.poll_once()
        self.assertEqual(sum(e["type"] == "dispatched" for e in self.events(d)), dispatches + 1)

    def test_rejection_uses_review_reason_without_verification_diagnosis(self):
        d, commit = self.reject()
        card = self.card(d)
        self.assertIsNone(card["blocked_details"])
        self.assertEqual(card["rejected_review"]["note"], "Missing edge case")
        self.assertEqual(card["rejected_review"]["commit"], commit)
        self.assertIn("Missing edge case", card["condition_reason"])
        self.assertFalse(card["actions"]["recover"]["ok"])

    def test_adjudication_cannot_rearm_rejected_commit_and_still_offers_rerun(self):
        d, commit = self.reject()
        run_id = self.card(d)["rejected_review"]["run_id"]
        stale = self.drop("adjudicate", d.name, run_id="stale", note="Investigated")
        self.process()
        self.assertEqual(self.outcome(stale)["state"], "refused")
        iid = self.drop("adjudicate", d.name, run_id=run_id, note="Investigated")
        self.process()
        self.assertEqual(self.outcome(iid)["state"], "landed")
        card = self.card(d)
        self.assertEqual(card["stage"], "Review")
        self.assertIsNone(card["condition"])
        self.assertIsNone(card["review_commit"])
        self.assertFalse(card["actions"]["review"]["ok"])
        self.assertTrue(card["actions"]["approve"]["ok"])
        iid = self.drop("review", d.name, result="pass", note="Try old commit", commit=commit)
        self.process()
        self.assertEqual(self.outcome(iid)["state"], "refused")
        self.assertEqual(self.depot("review", d.name, "pass", "Try old commit", "--commit", commit).returncode, 2)

    def test_stale_commit_blank_note_and_second_decision_are_refused(self):
        d, commit = self.ready()
        for h, note in (("f" * 40, "stale"), (commit, "  ")):
            iid = self.drop("review", d.name, result="pass", note=note, commit=h)
            self.process()
            self.assertEqual(self.outcome(iid)["state"], "refused")
            r = self.depot("review", d.name, "pass", note, "--commit", h)
            self.assertEqual(r.returncode, 2)
        self.assertFalse(any(e["type"] == "reviewed" for e in self.events(d)))
        self.assertEqual(self.depot("review", d.name, "pass", "ok", "--commit", commit).returncode, 0)
        self.assertEqual(self.depot("review", d.name, "fail", "late", "--commit", commit).returncode, 2)

    def test_no_acceptance_without_current_verified_commit(self):
        d = self.new_item()
        self.assertFalse(self.card(d)["actions"]["review"]["ok"])
        self.assertEqual(self.depot("review", d.name, "pass", "ok", "--commit", "f" * 40).returncode, 2)
        events = [{"type": "verified", "data": {"result": "pass"}},
                  {"type": "committed", "data": {"hash": "a" * 40}},
                  {"type": "dispatched", "data": {}}]
        self.assertIsNone(floor.review_commit(events))

    def test_existing_cinder_review_integration_still_completes(self):
        d, _ = self.ready()
        depot.Item(d).emit("reviewed", "reviewer", {"result": "pass", "review": "connector review"})
        self.assertEqual(self.card(d)["stage"], "Done")
        self.assertEqual(self.card(d)["checker"], "reviewer")
        self.assertEqual(depot.Item(d).derived_status(), "reviewed")

    def test_controls_submitted_during_run_hold_next_dispatch(self):
        for control in ("track_pause", "track_table", "park"):
            with self.subTest(control=control):
                # Isolate lanes and items for each control within the same disposable yard.
                track = control.replace("_", "-")
                first = self.new_item(track + "-first", track=track)
                second = self.new_item(track + "-second", track=track)
                for d in (first, second):
                    self.assertEqual(self.depot("approve", d.name).returncode, 0)
                original = depot.run_adapter
                started, ids = [], []

                def run(adapter, cmd, text, run_dir, item, timeout_s, cwd):
                    self.assertEqual(item.derived_status(), "dispatched")
                    started.append(item.name)
                    if item.name == first.name:
                        payload = {"kind": control, "data": {"track": track}}
                        if control == "park":
                            payload = {"kind": "park", "item": second.name, "data": {"reason": "hold next run"}}
                        ids.append(floor.drop_intent(self.yard, payload, self.clock)[1]["id"])
                    return original(adapter, cmd, text, run_dir, item, timeout_s, cwd)

                with mock.patch.object(depot, "run_adapter", run):
                    self.poll_once()
                self.assertIn(first.name, started)
                self.assertNotIn(second.name, started)
                self.assertEqual(self.outcome(ids[0])["state"], "landed")
                self.assertFalse(any(e["type"] == "dispatched" for e in self.events(second)))


@unittest.skipUnless(Path(NODE).exists(), "node is not available")
class TestReviewPage(unittest.TestCase):
    def test_floor_accept_and_reject_send_bound_commit_and_note(self):
        for result in ("pass", "fail"):
            with self.subTest(result=result):
                r = subprocess.run([NODE, str(floor.PAGE_DIR / "test_blocked.cjs"), "review-" + result],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
