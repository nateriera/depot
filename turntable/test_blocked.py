"""Blocked evidence and explicit operator decisions; uses disposable yards only."""
import json
import os
import subprocess
import uuid
from pathlib import Path
from unittest import mock

import depot
import floor
from test_floor import YardCase, E, lifecycle, ran


class TestBlocked(YardCase):
    def blocked(self, discrepancies=None, result="discrepancies"):
        record = json.loads((Path(__file__).parent / "fixtures/blocked-verification.json").read_text())
        if discrepancies is not None:
            record["discrepancies"] = discrepancies
        record["result"] = result
        events = lifecycle() + ran() + [E(89, "verified", result=result, discrepancies=record["discrepancies"])]
        events[2]["data"]["run_id"] = "run1"
        name = self.item(events, expected_files=record["expected_files"])
        item = self.root / "items" / name
        run = item / "runs/run1"
        run.mkdir(parents=True)
        (run / "verify.json").write_text(json.dumps(record))
        (run / "report.md").write_text("Existing work for review")
        return name, item, record

    def repo(self, item):
        repo = item / "work/repo"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "demo/existing-work", str(repo)], check=True)
        for f in ("turntable/depot.py", "turntable/test_depot.py", "turntable/FORMATS.md"):
            p = repo / f
            p.parent.mkdir(exist_ok=True)
            p.write_text("work\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "work"], check=True)
        return repo, subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    def test_wrong_directory_full_evidence_actual_repository_and_links(self):
        name, item, record = self.blocked()
        repo, commit = self.repo(item)
        before = self.yard_hashes()
        c = self.card(name)
        d = c["blocked_details"]
        self.assertEqual(d["cause"], "wrong-directory")
        self.assertEqual(d["discrepancies"], record["discrepancies"])
        self.assertEqual(len(d["discrepancies"]), 6)
        self.assertEqual(d["recovery"], {"branch": "demo/existing-work", "commit": commit, "directory": "work/repo"})
        self.assertTrue(c["actions"]["recover"]["ok"])
        self.assertTrue(c["actions"]["approve"]["ok"])
        self.assertEqual(set(d["links"]), {"verify.json", "report.md"})
        self.assertEqual(before, self.yard_hashes())

    def test_extra_missing_and_test_failure_have_distinct_causes_without_recovery(self):
        for kind, cause in (("extra", "extra-files-only"), ("missing", "missing-files-only")):
            name, _, _ = self.blocked([{"path": "a", "kind": kind}])
            c = self.card(name)
            self.assertEqual(c["blocked_details"]["cause"], cause)
            self.assertFalse(c["actions"]["recover"]["ok"])
            self.assertTrue(c["actions"]["approve"]["ok"])
        name, _, _ = self.blocked([], "tests_failed")
        self.assertEqual(self.card(name)["blocked_details"]["cause"], "tests-failed")

    def test_mixed_without_nested_files_is_unknown_and_no_stale_run_evidence(self):
        name, item, _ = self.blocked()
        self.assertEqual(self.card(name)["blocked_details"]["cause"], "unknown")
        events = floor.read_events(item / "events.jsonl")
        events += [E(3, "brief_approved"), E(2, "dispatched", run_id="run2"), E(1, "run_finished"), E(0, "verified", result="tests_failed", discrepancies=[])]
        (item / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        d = self.card(name)["blocked_details"]
        self.assertEqual(d["run_id"], "run2")
        self.assertEqual(d["links"], {})
        self.assertEqual(d["discrepancies"], [])

    def test_unborn_repository_does_not_offer_recovery_and_detached_commit_does(self):
        name, item, _ = self.blocked()
        work = item / "work"
        work.mkdir()
        subprocess.run(["git", "init", "-q", str(work)], check=True)
        self.assertFalse(self.card(name)["actions"]["recover"]["ok"])
        repo, commit = self.repo(item)
        subprocess.run(["git", "-C", str(repo), "checkout", "--detach", "-q", commit], check=True)
        c = self.card(name)
        self.assertTrue(c["actions"]["recover"]["ok"])
        self.assertIsNone(c["blocked_details"]["recovery"]["branch"])
        self.assertEqual(c["blocked_details"]["recovery"]["commit"], commit)

    def test_live_run_offers_neither_override_nor_recovery(self):
        name, item, _ = self.blocked()
        self.repo(item)
        events = lifecycle() + [E(1, "blocked", reason="operator hold")]
        (item / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        c = self.card(name)
        self.assertFalse(c["actions"]["recover"]["ok"])
        self.assertFalse(c["actions"]["adjudicate"]["ok"])
        self.assertFalse(c["actions"]["approve"]["ok"])

    def apply(self, name, kind, **data):
        with mock.patch.dict(os.environ, {"DEPOT_YARD": str(self.root)}):
            return depot.apply_intent(floor, {"id": str(uuid.uuid4()), "kind": kind, "item": name, "data": data})

    def test_adjudication_requires_note_and_current_run_records_override_without_dispatch(self):
        name, item, _ = self.blocked()
        for note, rid in (("", "run1"), ("Reviewed", "old")):
            with self.assertRaises(depot.IntentRefused):
                self.apply(name, "adjudicate", note=note, run_id=rid)
        self.apply(name, "adjudicate", note="Verifier used the wrong directory", run_id="run1")
        c = self.card(name)
        self.assertEqual((c["stage"], c["condition"], c["truth"]), ("Review", None, "recorded"))
        self.assertEqual(depot.Item(item).derived_status(), "review")
        ev = floor.read_events(item / "events.jsonl")[-1]
        self.assertEqual(ev["data"]["note"], "Verifier used the wrong directory")
        self.assertEqual(ev["type"], "adjudicated")
        self.assertFalse(c["actions"]["recover"]["ok"])

    def test_recover_requires_existing_commit_rechecks_pointer_and_records_review(self):
        name, item, _ = self.blocked()
        with self.assertRaises(depot.IntentRefused):
            self.apply(name, "recover", run_id="run1", commit="none")
        _, commit = self.repo(item)
        with self.assertRaises(depot.IntentRefused):
            self.apply(name, "recover", run_id="run1", commit="stale")
        before = {str(p.relative_to(item)): p.read_bytes() for base in (item / "work", item / "runs")
                  for p in base.rglob("*") if p.is_file()}
        self.apply(name, "recover", run_id="run1", commit=commit)
        self.assertEqual(before, {str(p.relative_to(item)): p.read_bytes() for base in (item / "work", item / "runs")
                                  for p in base.rglob("*") if p.is_file()})
        self.assertEqual(self.card(name)["stage"], "Review")
        ev = floor.read_events(item / "events.jsonl")[-1]
        self.assertEqual(ev["type"], "recovered")
        self.assertEqual(ev["data"]["commit"], commit)
        self.assertEqual(depot.Item(item).derived_status(), "review")

    def test_record_routes_serve_only_contained_allowed_artifacts(self):
        name, item, _ = self.blocked()
        def get(path):
            handler = object.__new__(floor.FloorHandler)
            handler.path = path
            handler.server = mock.Mock(yard=self.root, server_address=("127.0.0.1", 8765))
            handler.headers = {"Host": "127.0.0.1:8765"}
            handler._send = mock.Mock()
            handler.do_GET()
            return handler._send.call_args.args
        self.assertEqual(get(f"/records/{name}/run1/report.md")[:2], (200, b"Existing work for review"))
        self.assertEqual(get(f"/records/{name}/run1/verify.json")[0], 200)
        for path in (f"/records/{name}/run1/diff.patch", f"/records/{name}/../waybill.json", f"/records/{name}/run1/missing"):
            self.assertEqual(get(path)[0], 404)
        (item / "runs/run1/report.md").unlink()
        (item / "runs/run1/report.md").symlink_to(item / "waybill.json")
        self.assertEqual(get(f"/records/{name}/run1/report.md")[0], 404)
