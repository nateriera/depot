"""S8 tests: the floor's write half. Intent intake (the one POST route), daemon-side intent processing
(message / approve / park / unpark / attach / remove_attachment), outcomes and reconciliation, attachment
delivery, and the floor projection of all of it. New file: earlier suites change only where S8's truth changes them."""
import base64
import hashlib
import http.client
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from html.parser import HTMLParser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import depot  # noqa: E402
import floor  # noqa: E402
import test_floor as tf  # noqa: E402
from test_depot import TurntableCase  # noqa: E402

T0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    """A strictly increasing clock, so intent order never depends on wall time."""
    def __init__(self):
        self.t = T0

    def __call__(self):
        self.t += timedelta(milliseconds=5)
        return self.t


def b64(raw):
    return base64.b64encode(raw).decode()


class PanelCase(TurntableCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {k: self.env[k] for k in ("DEPOT_YARD", "DEPOT_CODEX", "DEPOT_HEARTBEAT_S")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock()
        self.logs = []

    def drop(self, kind, item, **data):
        if kind == "approve" and "contract_fingerprint" not in data:
            try:
                data.update(depot.approval_data(depot.find_item(item)))
            except depot.DepotError:
                pass  # invalid briefs are tested at landing
        status, body = floor.drop_intent(self.yard, {"kind": kind, "item": item, "data": data}, self.clock)
        self.assertIn(status, (200, 202), body)
        return body["id"]

    def process(self):
        with mock.patch.object(depot, "poll_log", lambda m: self.logs.append(m)):
            return depot.process_intents(False)

    def outcome(self, iid):
        return floor.read_outcome(self.yard, iid)

    def events(self, d):
        return [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines()]

    def glog(self):
        return [json.loads(l) for l in (self.yard / "log" / "events.jsonl").read_text().splitlines()]

    def poll_once(self, **env):
        with mock.patch.dict(os.environ, env), mock.patch.object(depot, "poll_log", lambda m: self.logs.append(m)):
            depot.poll_cycle(threading.Event(), {})

    def asked_item(self, slug="ask"):
        """An item that has run, asked a question and is waiting on operator."""
        d = self.new_item(slug)
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        r = self.depot("dispatch", d.name, FAKE_MODE="question")
        self.assertEqual(r.returncode, 3, r.stderr)
        time.sleep(1.1)  # run ids have one-second resolution: the next dispatch needs its own second
        return d

    def card(self, d):
        return {c["name"]: c for c in floor.project(self.yard, datetime.now(timezone.utc))["cards"]}[d.name]


class TestRecoveredReview(PanelCase):
    def recovered(self, remote=False):
        from test_depot import make_remote, git_run
        destination = make_remote(self.tmp) if remote else None
        if remote:
            # An older destination branch exists; recovery must still rebuild from current main.
            git_run(destination, "update-ref", "refs/heads/review/recovered", "main")
            seed = self.tmp / "seed"
            (seed / "base-update.txt").write_text("landed base update")
            git_run(seed, "add", ".")
            git_run(seed, "commit", "-qm", "advance base")
            git_run(seed, "push", "-q", str(destination), "main")
        d = self.new_item(**({"repo": str(destination), "branch": "review/recovered"} if remote else {}))
        item = depot.Item(d)
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        item.emit("dispatched", "turntable", {"run_id": "old-run", "adapter": "codex"})
        item.emit("run_finished", "turntable", {"exit_code": 0})
        item.emit("verified", "turntable", {"result": "tests_failed", "discrepancies": []})
        work = d / "work" if remote else d / "work/repo"
        if remote:
            git_run(d, "clone", "-q", str(destination), "work")
            git_run(work, "checkout", "-q", "-b", "review/recovered")
        else:
            work.mkdir(parents=True)
            git_run(work, "init", "-q")
        (work / "unverified.txt").write_text("Recovered output must not ride the re-run")
        git_run(work, "add", ".")
        git_run(work, "commit", "-qm", "unverified work")
        commit = git_run(work, "rev-parse", "HEAD")
        iid = self.drop("recover", d.name, run_id="old-run", commit=commit)
        self.process()
        self.assertEqual(self.outcome(iid)["state"], "landed")
        return d, commit, destination

    def test_recovered_review_approval_runs_fresh_and_supersedes_only_after_verified_commit(self):
        from test_depot import git_run
        for remote in (False, True):
            with self.subTest(remote=remote):
                d, recovered, destination = self.recovered(remote)
                c = self.card(d)
                self.assertEqual(c["stage"], "Review")
                self.assertTrue(c["actions"]["approve"]["ok"])
                self.assertFalse(c["actions"]["review"]["ok"])
                self.assertIsNone(c["recovered_work"]["superseded_by"])
                self.assertFalse(any(e["type"] == "dispatched" for e in self.events(d)[-1:]))
                iid = self.drop("review", d.name, result="pass", note="Cannot accept recovery", commit=recovered)
                self.process()
                self.assertEqual(self.outcome(iid)["state"], "refused")
                self.assertEqual(self.depot("review", d.name, "pass", "Cannot accept", "--commit", recovered).returncode, 2)
                approval = self.drop("approve", d.name)
                self.process()
                self.assertEqual(self.outcome(approval)["state"], "landed")
                self.assertEqual(self.card(d)["stage"], "Briefed")
                self.assertIsNone(self.card(d)["recovered_work"]["superseded_by"])
                original = depot.run_adapter

                def check_fresh(adapter, cmd, text, run_dir, item, timeout_s, cwd):
                    self.assertFalse((cwd / "unverified.txt").exists())
                    self.assertEqual(git_run(d / c["recovered_work"]["directory"], "rev-parse", "HEAD"), recovered)
                    if remote:
                        self.assertEqual(git_run(cwd, "rev-parse", "HEAD"), git_run(destination, "rev-parse", "main"))
                    self.assertFalse(self.card(d)["actions"]["review"]["ok"])
                    self.assertIsNone(self.card(d)["recovered_work"]["superseded_by"])
                    return original(adapter, cmd, text, run_dir, item, timeout_s, cwd)

                with mock.patch.object(depot, "run_adapter", check_fresh):
                    self.poll_once()
                c = self.card(d)
                self.assertEqual(c["stage"], "Review")
                self.assertTrue(c["actions"]["review"]["ok"])
                self.assertNotEqual(c["review_commit"], recovered)
                self.assertEqual(c["recovered_work"]["superseded_by"], c["review_commit"])
                self.assertEqual(c["recovered_work"]["commit"], recovered)
                self.assertEqual(git_run(d / c["recovered_work"]["directory"], "rev-parse", "HEAD"), recovered)
                old = self.drop("review", d.name, result="pass", note="Old pointer", commit=recovered)
                self.process()
                self.assertEqual(self.outcome(old)["state"], "refused")
                new = self.drop("review", d.name, result="pass", note="Verified replacement", commit=c["review_commit"])
                self.process()
                self.assertEqual(self.outcome(new)["state"], "landed")
                self.assertEqual(self.card(d)["stage"], "Done")

    def test_failed_rerun_keeps_recovery_unsuperseded_and_unacceptable(self):
        d, recovered, _ = self.recovered()
        self.drop("approve", d.name)
        self.process()
        self.poll_once(FAKE_MODE="fail")
        c = self.card(d)
        self.assertFalse(c["actions"]["review"]["ok"])
        self.assertEqual(c["recovered_work"]["commit"], recovered)
        self.assertIsNone(c["recovered_work"]["superseded_by"])
        self.assertTrue(c["actions"]["approve"]["ok"])
        self.assertIsNone(c["blocked_details"]["recovery"])  # not the old workspace HEAD
        time.sleep(1.1)  # run ids have second resolution
        self.drop("approve", d.name)
        self.process()
        self.poll_once()
        c = self.card(d)
        self.assertTrue(c["actions"]["review"]["ok"])
        self.assertEqual(c["recovered_work"]["superseded_by"], c["review_commit"])

    def test_recovered_rerun_obeys_live_and_parked_guards(self):
        d, _, _ = self.recovered()
        item = depot.Item(d)
        item.emit("parked", "operator", {"reason": "hold"})
        self.assertFalse(self.card(d)["actions"]["approve"]["ok"])
        item.emit("unparked", "operator", {})
        item.emit("dispatched", "turntable", {"run_id": "live-run"})
        self.assertFalse(self.card(d)["actions"]["approve"]["ok"])

    def test_shipped_recovery_controls_and_copy(self):
        import subprocess
        script = Path(__file__).resolve().parent.parent / "floor" / "test_blocked.cjs"
        for scenario in ("0021", "recovered", "superseded"):
            with self.subTest(scenario=scenario):
                r = subprocess.run(["node", str(script), scenario], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestApprovalContract(PanelCase):
    def test_shipped_panel_displays_contract_and_submits_its_revision(self):
        import subprocess
        script = Path(__file__).resolve().parent.parent / "floor" / "test_blocked.cjs"
        result = subprocess.run(["node", str(script), "contract"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_panel_contract_contains_all_current_inputs(self):
        d = self.new_item(repo="https://example.com/destination", branch="review/contract")
        c = self.card(d)["contract"]
        self.assertEqual(c["brief"], (d / "brief.md").read_text())
        self.assertEqual(c["expected_files"], ["sentinel.py", "test_sentinel.py"])
        self.assertEqual(c["test_command"], sys.executable + " -m unittest")
        self.assertEqual((c["repo"], c["branch"]), ("https://example.com/destination", "review/contract"))
        self.assertEqual(c["fingerprint"], depot.approval_data(depot.Item(d))["contract_fingerprint"])

    def test_stale_panel_approval_cannot_authorize_revised_contract(self):
        d = self.new_item()
        iid = self.drop("approve", d.name)
        with open(d / "brief.md", "a") as f:
            f.write("Changed after the panel was displayed.\n")
        self.process()
        self.assertEqual(self.outcome(iid)["state"], "refused")
        self.assertIn("task contract changed", self.outcome(iid)["reason"])
        self.assertNotIn("brief_approved", self.types(d))
        fresh = self.drop("approve", d.name)
        self.poll_once()
        self.assertEqual(self.outcome(fresh)["state"], "landed")
        self.assertEqual(self.types(d).count("dispatched"), 1)

    def test_changed_briefed_contract_can_be_reapproved_in_panel(self):
        d = self.new_item()
        self.depot("approve", d.name)
        self.assertFalse(self.card(d)["actions"]["approve"]["ok"])
        with open(d / "brief.md", "a") as f:
            f.write("Revised before dispatch.\n")
        self.assertTrue(self.card(d)["actions"]["approve"]["ok"])
        fresh = self.drop("approve", d.name)
        self.poll_once()
        self.assertEqual(self.outcome(fresh)["state"], "landed")
        self.assertEqual(self.types(d).count("dispatched"), 1)

    def test_unbound_panel_intent_requires_fresh_approval(self):
        d = self.new_item()
        _, body = floor.drop_intent(self.yard, {"kind": "approve", "item": d.name, "data": {}}, self.clock)
        self.process()
        self.assertEqual(self.outcome(body["id"])["state"], "refused")
        self.assertIn("no fingerprint", self.outcome(body["id"])["reason"])
        self.assertNotIn("brief_approved", self.types(d))


class TestIntake(tf.YardCase):
    """POST /intents: shape validation, the size cap, the same-origin guard, and nothing else writable."""

    def setUp(self):
        super().setUp()
        self.srv = floor.make_server(self.root, 0, Clock())
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.port = self.srv.server_address[1]

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def post(self, body, path="/intents", headers=None, raw=False, omit=()):
        h = {"Content-Type": "application/json", "Host": f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        no_length = "Content-Length" in omit
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        try:
            conn.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
            payload = body if raw else json.dumps(body).encode()
            if "Content-Length" not in h and not no_length:
                h["Content-Length"] = str(len(payload))
            for k, v in h.items():
                conn.putheader(k, v)
            conn.endheaders()
            if h.get("Content-Length") == str(len(payload)):
                conn.send(payload)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, (json.loads(data) if data[:1] == b"{" else data)
        finally:
            conn.close()

    def intents(self):
        d = self.root / "log" / "intents"
        return sorted(p for p in d.glob("*")) if d.is_dir() else []

    def test_accepts_an_intent_and_writes_one_file(self):
        status, body = self.post({"kind": "message", "item": "0001-thing", "data": {"text": "hello"}})
        self.assertEqual(status, 202)
        files = self.intents()
        self.assertEqual([p.name for p in files], [body["id"] + ".json"])
        saved = json.loads(files[0].read_text())
        self.assertEqual({k: saved[k] for k in ("id", "item", "kind", "data")},
                         {"id": body["id"], "item": "0001-thing", "kind": "message", "data": {"text": "hello"}})
        self.assertRegex(saved["ts"], r"^2026-11-01T12:00:00\.\d{3}Z$")  # the server's clock, not the client's

    def test_the_server_clock_wins_over_a_client_ts(self):
        _, body = self.post({"kind": "approve", "item": "0001-thing", "ts": "1999-01-01T00:00:00.000Z", "data": {}})
        saved = json.loads((self.root / "log" / "intents" / f"{body['id']}.json").read_text())
        self.assertTrue(saved["ts"].startswith("2026-11-01"))

    def test_shape_validation_by_kind(self):
        bad = [
            ({"kind": "delete", "item": "0001-thing", "data": {}}, "unknown kind"),
            ({"kind": "message", "item": "../etc", "data": {"text": "x"}}, "item"),
            ({"kind": "message", "item": "0001-thing", "data": {}}, "required"),
            ({"kind": "message", "item": "0001-thing", "data": {"text": 5}}, "string"),
            ({"kind": "message", "item": "0001-thing", "data": {"text": "x", "extra": 1}}, "unexpected"),
            ({"kind": "approve", "item": "0001-thing", "data": {"text": "x"}}, "unexpected"),
            ({"kind": "park", "item": "0001-thing", "data": {}}, "required"),
            ({"kind": "message", "item": "0001-thing", "id": "not-a-uuid", "data": {"text": "x"}}, "uuid"),
            ({"kind": "message", "item": "0001-thing", "data": {"text": "x" * (floor.MAX_TEXT_CHARS + 1)}}, "characters"),
            ({"kind": "attach", "item": "0001-thing", "data": {"name": "a.txt", "content_b64": "***"}}, "base64"),
            ({"kind": "attach", "item": "0001-thing", "data": {"name": "..", "content_b64": b64(b"x")}}, "filename"),
            (["not", "an", "object"], "object"),
        ]
        for body, needle in bad:
            with self.subTest(body=str(body)[:60]):
                status, resp = self.post(body)
                self.assertEqual(status, 400)
                self.assertIn(needle, resp["error"])
        self.assertEqual(self.intents(), [])  # nothing was written for any of them

    def test_malformed_json_is_400(self):
        status, resp = self.post(b"{not json", raw=True)
        self.assertEqual(status, 400)
        self.assertIn("JSON", resp["error"])
        self.assertEqual(self.intents(), [])

    def test_traversal_filename_is_reduced_to_a_safe_basename(self):
        for given, safe in (("../../etc/passwd", "passwd"), ("a\\b\\c.txt", "c.txt"), (".hidden", "hidden"),
                            ("my file (1).txt", "my_file__1_.txt")):
            with self.subTest(given=given):
                status, body = self.post({"kind": "attach", "item": "0001-thing",
                                          "data": {"name": given, "content_b64": b64(b"x")}})
                self.assertEqual(status, 202)
                saved = json.loads((self.root / "log" / "intents" / f"{body['id']}.json").read_text())
                self.assertEqual(saved["data"]["name"], safe)

    def test_oversize_request_is_413_and_not_read(self):
        status, resp = self.post(b"", headers={"Content-Length": str(floor.MAX_REQUEST_BYTES + 1)}, raw=True)
        self.assertEqual(status, 413)
        self.assertEqual(self.intents(), [])

    def test_oversize_attachment_is_400(self):
        big = b64(b"x" * (floor.MAX_ATTACHMENT_BYTES + 1))
        status, resp = self.post({"kind": "attach", "item": "0001-thing", "data": {"name": "big.bin", "content_b64": big}})
        self.assertEqual(status, 400)
        self.assertIn("limit", resp["error"])
        ok = b64(b"x" * floor.MAX_ATTACHMENT_BYTES)
        self.assertEqual(self.post({"kind": "attach", "item": "0001-thing",
                                    "data": {"name": "ok.bin", "content_b64": ok}})[0], 202)

    def test_content_type_and_length_are_required(self):
        body = {"kind": "approve", "item": "0001-thing", "data": {}}
        self.assertEqual(self.post(body, headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.post(body, headers={"Content-Type": None})[0], 415)
        self.assertEqual(self.post(body, omit=("Content-Length",))[0], 411)

    def test_foreign_origin_and_foreign_host_are_refused(self):
        body = {"kind": "approve", "item": "0001-thing", "data": {}}
        self.assertEqual(self.post(body, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.post(body, headers={"Host": "evil.example"})[0], 403)  # DNS rebinding
        self.assertEqual(self.post(body, headers={"Origin": f"http://127.0.0.1:{self.port}"})[0], 202)
        self.assertEqual(self.intents().__len__(), 1)

    def test_every_other_write_is_still_refused(self):
        for path in ("/cards.json", "/floor.js", "/", "/intents/x", "/nope"):
            with self.subTest(path=path):
                self.assertEqual(self.post({"kind": "approve", "item": "0001-thing", "data": {}}, path=path)[0], 405)
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            with self.subTest(method=method):
                conn = http.client.HTTPConnection("127.0.0.1", self.port)
                conn.request(method, "/intents", body=b"{}" if method != "OPTIONS" else None)
                resp = conn.getresponse()
                resp.read()
                conn.close()
                self.assertEqual(resp.status, 405)
        self.assertEqual(self.intents(), [])

    def test_intake_writes_only_under_log_intents(self):
        before = self.yard_hashes()
        self.post({"kind": "message", "item": "0001-thing", "data": {"text": "x"}})
        after = self.yard_hashes()
        added = set(after) - set(before)
        self.assertEqual(len(added), 1)
        self.assertTrue(next(iter(added)).startswith("log/intents/"))
        self.assertEqual({k: v for k, v in before.items()}, {k: after[k] for k in before})

    def test_a_represented_intent_is_a_duplicate_not_a_second_file(self):
        body = {"id": "11111111-2222-3333-4444-555555555555", "kind": "message", "item": "0001-thing",
                "data": {"text": "same"}}
        self.assertEqual(self.post(body)[0], 202)
        status, resp = self.post(body)
        self.assertEqual((status, resp.get("duplicate")), (200, True))
        self.assertEqual(len(self.intents()), 1)
        clash = {**body, "data": {"text": "different"}}
        self.assertEqual(self.post(clash)[0], 409)
        self.assertEqual(json.loads(self.intents()[0].read_text())["data"], {"text": "same"})

    def test_retry_of_a_failed_intent_reopens_it_with_the_same_id(self):
        body = {"id": "11111111-2222-3333-4444-555555555555", "kind": "message", "item": "0001-thing",
                "data": {"text": "retry me"}}
        self.post(body)
        floor.write_atomic(floor.outcome_path(self.root, body["id"]),
                           json.dumps({"intent": body["id"], "state": "failed", "reason": "disk"}))
        status, resp = self.post(body)
        self.assertEqual((status, resp.get("retried")), (200, True))
        self.assertIsNone(floor.read_outcome(self.root, body["id"]))
        # a refusal is final: re-presenting it does not reopen it
        floor.write_atomic(floor.outcome_path(self.root, body["id"]),
                           json.dumps({"intent": body["id"], "state": "refused", "reason": "no"}))
        self.assertTrue(self.post(body)[1].get("duplicate"))
        self.assertEqual(floor.read_outcome(self.root, body["id"])["state"], "refused")


class TestMessageAndApprove(PanelCase):
    def test_message_lands_with_via_floor_and_the_intent_id(self):
        d = self.new_item()
        iid = self.drop("message", d.name, text="a note")
        self.assertEqual(self.process(), 1)
        e = self.events(d)[-1]
        self.assertEqual((e["type"], e["actor"], e["data"]),
                         ("message", "operator", {"text": "a note", "via": "floor", "intent": iid}))
        self.assertEqual(self.glog()[-1], e)
        out = self.outcome(iid)
        self.assertEqual((out["state"], out["event"]), ("landed", {"type": "message", "seq": len(self.glog())}))

    def test_message_answering_a_question_links_it(self):
        d = self.asked_item()
        rid = [e for e in self.events(d) if e["type"] == "input_required"][-1]["data"]["run_id"]
        iid = self.drop("message", d.name, text="call it foo.py")
        self.process()
        data = self.events(d)[-1]["data"]
        self.assertEqual((data["answers_input_required"], data["intent"]), (rid, iid))

    def test_empty_message_is_refused_with_a_reason(self):
        d = self.new_item()
        iid = self.drop("message", d.name, text="   ")
        self.assertEqual(self.process(), 0)
        out = self.outcome(iid)
        self.assertEqual(out["state"], "refused")
        self.assertIn("non-empty", out["reason"])
        self.assertEqual(self.types(d), ["item_created"])

    def test_unknown_item_is_refused(self):
        iid = self.drop("message", "0099-ghost", text="x")
        self.process()
        self.assertIn("no such item", self.outcome(iid)["reason"])

    def test_approve_lands_and_the_same_cycle_dispatches_it(self):
        d = self.new_item()
        iid = self.drop("approve", d.name)
        self.poll_once()  # one cycle: intents first, then dispatch decisions
        types = self.types(d)
        self.assertEqual(types[:2], ["item_created", "brief_approved"])
        self.assertIn("dispatched", types)
        e = self.events(d)[1]
        self.assertEqual(e["actor"], "operator")
        self.assertEqual(e["data"], {"directed_by": "operator", "via": "floor", "intent": iid,
                                     **depot.approval_data(depot.Item(d))})
        self.assertEqual(self.outcome(iid)["state"], "landed")
        self.assertEqual(json.loads((d / "runs").iterdir().__next__().joinpath("verify.json").read_text())["result"], "pass")

    def test_approve_after_an_answer_dispatches_with_the_answer_in_the_addendum(self):
        d = self.asked_item()
        self.drop("message", d.name, text="call it foo.py")
        self.drop("approve", d.name)
        self.poll_once()
        sent = sorted((d / "runs").iterdir())[-1].joinpath("brief.dispatched.md").read_text()
        self.assertIn("Addendum", sent)
        self.assertIn("call it foo.py", sent)

    def test_approve_is_refused_while_a_message_intent_is_still_pending(self):
        d = self.asked_item()
        self.drop("message", d.name, text="the answer")           # never processed: still pending
        approve = self.drop("approve", d.name)
        intent = json.loads(floor.intent_path(self.yard, approve).read_text())
        fl = floor
        reason = depot.unlanded_message_block(fl, d.name, intent)
        self.assertIn("has not landed yet", reason)
        before = self.types(d)
        # the real path: processing the pair lands the message first, so the approve goes through
        self.assertEqual(self.process(), 2)
        self.assertEqual(self.types(d)[len(before):], ["message", "brief_approved"])

    def test_approve_is_refused_behind_a_refused_message(self):
        d = self.asked_item()
        msg = self.drop("message", d.name, text="  ")
        self.process()
        self.assertEqual(self.outcome(msg)["state"], "refused")
        approvals = self.types(d).count("brief_approved")
        approve = self.drop("approve", d.name)
        self.process()
        out = self.outcome(approve)
        self.assertEqual(out["state"], "refused")
        self.assertIn("never dispatches around an unlanded answer", out["reason"])
        self.assertEqual(self.types(d).count("brief_approved"), approvals)

    def test_approve_is_refused_behind_a_failed_message_even_when_nothing_landed(self):
        d = self.asked_item()
        msg = self.drop("message", d.name, text="the answer")
        with mock.patch.object(depot.Item, "emit", side_effect=OSError("disk full")):
            self.process()
        self.assertEqual(self.outcome(msg)["state"], "failed")
        approve = self.drop("approve", d.name)
        self.process()
        out = self.outcome(approve)
        self.assertEqual(out["state"], "refused")
        self.assertIn("failed", out["reason"])
        self.assertEqual(self.types(d).count("brief_approved"), 1)   # only the original approval
        # retry (same id re-presented after the failure) lands the message; then a fresh approve goes through
        floor.drop_intent(self.yard, json.loads(floor.intent_path(self.yard, msg).read_text()), self.clock)
        self.process()
        self.assertEqual(self.outcome(msg)["state"], "landed")

    def test_approve_refusals_state_the_table_reason(self):
        d = self.new_item()
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        iid = self.drop("approve", d.name)             # already Briefed
        self.process()
        out = self.outcome(iid)
        self.assertEqual(out["state"], "refused")
        self.assertIn("Already approved", out["reason"])
        self.assertEqual(self.types(d), ["item_created", "brief_approved"])

    def test_approve_with_an_invalid_brief_is_refused(self):
        d = self.new_item(brief="no depot block here")
        iid = self.drop("approve", d.name)
        self.process()
        self.assertEqual(self.outcome(iid)["state"], "refused")
        self.assertEqual(self.types(d), ["item_created"])


class TestParkAndUnpark(PanelCase):
    def test_park_requires_a_reason(self):
        d = self.new_item()
        iid = self.drop("park", d.name, reason="   ")
        self.process()
        out = self.outcome(iid)
        self.assertEqual(out["state"], "refused")
        self.assertIn("reason", out["reason"])
        self.assertEqual(self.types(d), ["item_created"])

    def test_park_lands_with_the_reason_and_the_card_reads_parked(self):
        d = self.new_item()
        iid = self.drop("park", d.name, reason="waits on the vault bridge")
        self.process()
        e = self.events(d)[-1]
        self.assertEqual((e["type"], e["data"]),
                         ("parked", {"reason": "waits on the vault bridge", "via": "floor", "intent": iid}))
        c = self.card(d)
        self.assertEqual((c["stage"], c["condition"], c["condition_reason"], c["parked"]),
                         ("Queued", "parked", "waits on the vault bridge", "waits on the vault bridge"))
        self.assertFalse(c["actions"]["approve"]["ok"])
        self.assertIn("unpark it first", c["actions"]["approve"]["why"])

    def test_park_source_states(self):
        queued = self.new_item("queued")
        asked = self.asked_item("asked")
        briefed = self.new_item("briefed")
        self.assertEqual(self.depot("approve", briefed.name).returncode, 0)
        ok = [self.drop("park", d.name, reason="r") for d in (queued, asked, briefed)]
        self.process()
        self.assertEqual([self.outcome(i)["state"] for i in ok], ["landed"] * 3)
        again = self.drop("park", queued.name, reason="again")
        self.process()
        self.assertEqual(self.outcome(again)["reason"], "Already parked.")

    def test_approve_on_a_parked_card_is_refused_until_unparked(self):
        d = self.new_item()
        self.drop("park", d.name, reason="hold")
        approve = self.drop("approve", d.name)
        self.process()
        self.assertEqual(self.outcome(approve)["state"], "refused")
        self.drop("unpark", d.name)
        self.process()
        self.assertIsNone(self.card(d)["parked"])
        fresh = self.drop("approve", d.name)
        self.process()
        self.assertEqual(self.outcome(fresh)["state"], "landed")

    def test_unpark_only_when_parked(self):
        d = self.new_item()
        iid = self.drop("unpark", d.name)
        self.process()
        out = self.outcome(iid)
        self.assertEqual((out["state"], out["reason"]), ("refused", "This card is not parked."))

    def test_unpark_dispatches_nothing_and_approves_nothing(self):
        for make in (self.new_item, self.asked_item):
            with self.subTest(make=make.__name__):
                d = make("u-" + make.__name__.replace("_", "-"))
                before = self.types(d)
                self.drop("park", d.name, reason="hold")
                self.drop("unpark", d.name)
                self.poll_once()                       # a whole cycle, with an unpark and nothing else
                added = self.types(d)[len(before):]
                self.assertEqual(added, ["parked", "unparked"])
                self.assertNotIn("dispatched", added)
                self.assertNotIn("brief_approved", added)
                e = self.events(d)[-1]
                self.assertEqual(e["data"]["via"], "floor")
                c = self.card(d)
                self.assertIsNone(c["parked"])
                self.assertIsNone(c["condition"])

    def test_park_is_refused_on_a_card_with_no_open_decision(self):
        d = self.new_item()
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)   # Building -> Review after verify
        iid = self.drop("park", d.name, reason="late")
        self.process()
        self.assertEqual(self.outcome(iid)["state"], "refused")


class TestAttachments(PanelCase):
    def test_attach_lands_the_file_and_the_event(self):
        d = self.new_item()
        raw = b"col1,col2\n1,2\n"
        iid = self.drop("attach", d.name, name="data.csv", content_b64=b64(raw))
        self.process()
        self.assertEqual((d / "files" / "data.csv").read_bytes(), raw)
        e = self.events(d)[-1]
        self.assertEqual((e["type"], e["data"]), ("attached", {
            "file": "data.csv", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "via": "floor", "intent": iid}))
        self.assertEqual(self.outcome(iid)["state"], "landed")
        self.assertEqual([(a["file"], a["state"]) for a in self.card(d)["attachments"]], [("data.csv", "attached")])

    def test_duplicate_name_is_refused_until_removed(self):
        d = self.new_item()
        self.drop("attach", d.name, name="a.txt", content_b64=b64(b"one"))
        again = self.drop("attach", d.name, name="a.txt", content_b64=b64(b"two"))
        self.process()
        self.assertIn("already attached", self.outcome(again)["reason"])
        self.assertEqual((d / "files" / "a.txt").read_bytes(), b"one")
        self.drop("remove_attachment", d.name, name="a.txt")
        self.drop("attach", d.name, name="a.txt", content_b64=b64(b"two"))
        self.process()
        self.assertEqual((d / "files" / "a.txt").read_bytes(), b"two")

    def test_a_hand_written_intent_with_a_traversal_name_is_refused(self):
        d = self.new_item()
        iid = "11111111-2222-3333-4444-555555555555"
        (self.yard / "log" / "intents").mkdir(parents=True)
        floor.intent_path(self.yard, iid).write_text(json.dumps({
            "id": iid, "ts": "2026-11-01T12:00:00.000Z", "item": d.name, "kind": "attach",
            "data": {"name": "../../escape.txt", "content_b64": b64(b"x")}}))
        self.process()
        self.assertEqual(self.outcome(iid)["state"], "refused")
        self.assertFalse((self.yard / "escape.txt").exists())
        self.assertFalse((d / "files").exists())

    def test_remove_before_delivery_works(self):
        d = self.new_item()
        self.drop("attach", d.name, name="a.txt", content_b64=b64(b"one"))
        rm = self.drop("remove_attachment", d.name, name="a.txt")
        self.process()
        out = self.outcome(rm)
        self.assertEqual((out["state"], out["event"]["type"]), ("landed", "attachment_removed"))
        self.assertFalse((d / "files" / "a.txt").exists())
        self.assertEqual(self.card(d)["attachments"], [])
        self.assertEqual(self.events(d)[-1]["data"], {"file": "a.txt", "via": "floor", "intent": rm})

    def test_remove_after_delivery_is_refused_with_the_reason(self):
        d = self.new_item()
        self.drop("attach", d.name, name="a.txt", content_b64=b64(b"one"))
        self.drop("approve", d.name)
        self.poll_once()                                   # attach lands, approve lands, the run is dispatched
        self.assertEqual([(a["file"], a["state"]) for a in self.card(d)["attachments"]], [("a.txt", "delivered")])
        rm = self.drop("remove_attachment", d.name, name="a.txt")
        self.process()
        out = self.outcome(rm)
        self.assertEqual(out["state"], "refused")
        self.assertIn("already been delivered", out["reason"])
        self.assertTrue((d / "files" / "a.txt").exists())

    def test_remove_of_an_unknown_attachment_is_refused(self):
        d = self.new_item()
        iid = self.drop("remove_attachment", d.name, name="ghost.txt")
        self.process()
        self.assertIn("no such attachment", self.outcome(iid)["reason"])

    def test_delivered_attachments_ride_the_dispatch_and_do_not_pollute_the_verified_set(self):
        d = self.new_item()
        raw = b"input data\n"
        self.drop("attach", d.name, name="input.txt", content_b64=b64(raw))
        self.drop("approve", d.name)
        self.poll_once()
        run = next((d / "runs").iterdir())
        self.assertEqual((d / "work" / depot.ATTACH_DIR / "input.txt").read_bytes(), raw)   # it rode in
        verify = json.loads((run / "verify.json").read_text())
        self.assertEqual(verify["result"], "pass")
        self.assertEqual(verify["discrepancies"], [])
        self.assertEqual(verify["changed_files"], ["sentinel.py", "test_sentinel.py"])        # exact set, still exact
        self.assertNotIn(depot.ATTACH_DIR, (run / "diff.patch").read_text())
        sent = (run / "brief.dispatched.md").read_text()
        self.assertIn("## Attachments from operator", sent)
        self.assertIn(f"{depot.ATTACH_DIR}/input.txt", sent)
        self.assertIn(hashlib.sha256(raw).hexdigest(), sent)
        dispatched = next(e for e in self.events(d) if e["type"] == "dispatched")
        self.assertEqual(dispatched["data"]["attachments"], [{"file": "input.txt", "sha256": hashlib.sha256(raw).hexdigest()}])

    def test_an_item_without_attachments_dispatches_exactly_as_before(self):
        d = self.new_item()
        self.drop("approve", d.name)
        self.poll_once()
        dispatched = next(e for e in self.events(d) if e["type"] == "dispatched")
        self.assertNotIn("attachments", dispatched["data"])
        self.assertFalse((d / "work" / depot.ATTACH_DIR).exists())
        self.assertNotIn("Attachments", (next((d / "runs").iterdir()) / "brief.dispatched.md").read_text())

    def test_a_tampered_attachment_is_refused_at_dispatch(self):
        d = self.new_item()
        self.drop("attach", d.name, name="input.txt", content_b64=b64(b"good"))
        self.process()
        (d / "files" / "input.txt").write_bytes(b"tampered")
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        r = self.depot("dispatch", d.name)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("sha256", r.stderr)
        self.assertNotIn("dispatched", self.types(d))


class TestIdempotenceAndReconcile(PanelCase):
    def test_a_represented_intent_never_lands_twice(self):
        d = self.new_item()
        body = {"id": "11111111-2222-3333-4444-555555555555", "kind": "message", "item": d.name, "data": {"text": "once"}}
        self.assertEqual(floor.drop_intent(self.yard, body, self.clock)[0], 202)
        self.process()
        self.assertEqual(floor.drop_intent(self.yard, body, self.clock)[1].get("duplicate"), True)
        self.process()
        self.process()
        self.assertEqual(self.types(d), ["item_created", "message"])
        self.assertEqual(sum(1 for e in self.glog() if e["type"] == "message"), 1)

    def test_event_committed_but_outcome_missing_is_reconciled_not_repeated(self):
        d = self.new_item()
        iid = self.drop("message", d.name, text="landed but unrecorded")
        self.process()
        floor.outcome_path(self.yard, iid).unlink()           # the daemon "died" before writing the outcome
        receipts = floor.receipts_by_item(self.yard, floor.load_yard(self.yard))[d.name]
        self.assertEqual([r["state"] for r in receipts], ["reconciling"])   # the panel's "reconciling" window
        self.assertEqual(self.process(), 1)                                  # reconciled counts as landed
        out = self.outcome(iid)
        self.assertEqual((out["state"], out["event"]["type"], out["reconciled"]), ("landed", "message", True))
        self.assertEqual(out["event"]["seq"], len(self.glog()))
        self.assertEqual(self.types(d), ["item_created", "message"])         # still exactly one event
        self.assertEqual([r["state"] for r in floor.receipts_by_item(self.yard, floor.load_yard(self.yard))[d.name]],
                         ["landed"])

    def test_reconcile_covers_an_attach_whose_outcome_was_lost(self):
        d = self.new_item()
        iid = self.drop("attach", d.name, name="a.txt", content_b64=b64(b"one"))
        self.process()
        floor.outcome_path(self.yard, iid).unlink()
        self.process()
        self.assertEqual(self.outcome(iid)["state"], "landed")
        self.assertEqual(self.types(d).count("attached"), 1)

    def test_intents_land_in_timestamp_order(self):
        d = self.new_item()
        self.drop("message", d.name, text="first")
        self.drop("message", d.name, text="second")
        self.drop("message", d.name, text="third")
        self.process()
        self.assertEqual([e["data"]["text"] for e in self.events(d) if e["type"] == "message"],
                         ["first", "second", "third"])

    def test_an_unreadable_intent_is_refused_not_a_crash(self):
        d = self.new_item()
        iid = "11111111-2222-3333-4444-555555555555"
        (self.yard / "log" / "intents").mkdir(parents=True)
        floor.intent_path(self.yard, iid).write_text("{ not json")
        good = self.drop("message", d.name, text="still processed")
        self.process()
        self.assertEqual(self.outcome(iid)["state"], "refused")
        self.assertEqual(self.outcome(good)["state"], "landed")

    def test_infrastructure_failure_is_failed_and_distinct_from_refused(self):
        d = self.new_item()
        iid = self.drop("attach", d.name, name="a.txt", content_b64=b64(b"x"))
        real = os.replace

        def replace(src, dst):
            if "files" in Path(dst).parts:
                raise OSError("disk full")
            return real(src, dst)
        with mock.patch.object(depot.os, "replace", replace):
            self.process()
        out = self.outcome(iid)
        self.assertEqual(out["state"], "failed")
        self.assertIn("disk full", out["reason"])
        self.assertEqual(self.types(d), ["item_created"])
        rec = floor.receipts_by_item(self.yard, floor.load_yard(self.yard))[d.name][0]
        self.assertEqual((rec["state"], "disk full" in rec["reason"]), ("failed", True))

    def test_records_commit_follows_the_daemon_path_and_the_yard_stays_clean(self):
        subprocess_run = __import__("subprocess").run
        def git(*a):
            return subprocess_run(["git", *a], cwd=self.yard, capture_output=True, text=True, check=True).stdout
        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (self.yard / ".gitignore").write_text("items/*/work/\n__pycache__/\nlog/poll.lock\nlog/poll.log\nlog/intents/\n")
        d = self.new_item()
        git("add", "-A")
        git("commit", "-q", "-m", "seed")
        self.drop("message", d.name, text="note")
        self.drop("attach", d.name, name="a.txt", content_b64=b64(b"x"))
        with mock.patch.object(depot, "poll_log", lambda m: self.logs.append(m)):
            self.assertEqual(depot.process_intents(True), 2)
        self.assertEqual(git("status", "--porcelain").strip(), "")            # intent files are ignored; records committed
        self.assertEqual(git("log", "-1", "--format=%s").strip(), "depot: floor intents (2 landed)")
        self.assertIn("items/0001-thing/files/a.txt", git("show", "--name-only", "--format=", "HEAD"))


class TestFloorDerivation(tf.YardCase):
    def parked_card(self, extra, **kw):
        return self.card(self.item([tf.E(10, "item_created", "operator")] + extra, **kw))

    def test_item_reopening_and_approval_preserve_park_until_explicit_unpark(self):
        events = [tf.E(9, "reviewed", "reviewer", result="pass"),
                  tf.E(8, "parked", "operator", reason="hold it"),
                  tf.E(7, "reopened", "operator", text="another pass"),
                  tf.E(6, "brief_approved", "operator"),
                  tf.E(5, "message", "operator", text="still held")]
        c = self.parked_card(events)
        self.assertEqual((c["stage"], c["parked"]), ("Briefed", "hold it"))
        self.assertFalse(c["actions"]["approve"]["ok"])
        self.assertTrue(c["actions"]["unpark"]["ok"])
        c = self.parked_card(events + [tf.E(4, "unparked", "operator")], slug="released")
        self.assertEqual((c["stage"], c["parked"]), ("Briefed", None))

    def test_parked_then_unparked_clears_the_condition(self):
        c = self.parked_card([tf.E(5, "parked", "operator", reason="hold it", via="floor")])
        self.assertEqual((c["stage"], c["condition"], c["condition_reason"], c["parked"]),
                         ("Queued", "parked", "hold it", "hold it"))
        self.assertEqual(c["last_event"]["text"], "parked: hold it")
        c = self.parked_card([tf.E(5, "parked", "operator", reason="hold it"), tf.E(3, "unparked", "operator", via="floor")], slug="b")
        self.assertEqual((c["stage"], c["condition"], c["parked"]), ("Queued", None, None))
        self.assertEqual(c["last_event"]["text"], "unparked: hold released, nothing dispatched")
        self.assertEqual(c["truth"], "recorded")

    def test_unparked_changes_no_stage(self):
        base = tf.lifecycle() + tf.ran() + [tf.E(60, "input_required", "codex", question="which file?", run_id="R")]
        c = self.card(self.item(base + [tf.E(30, "parked", "operator", reason="hmm"), tf.E(20, "unparked", "operator")], slug="aw"))
        self.assertEqual((c["stage"], c["parked"], c["condition"]), ("Awaiting operator", None, None))

    def test_attachment_states_attached_then_delivered_and_removal(self):
        a = tf.E(50, "attached", "operator", file="a.txt", bytes=3, sha256="x", via="floor")
        b = tf.E(49, "attached", "operator", file="b.txt", bytes=4, sha256="y", via="floor")
        c = self.card(self.item([tf.E(60, "item_created", "operator"), a, b], slug="att"))
        self.assertEqual([(x["file"], x["state"]) for x in c["attachments"]], [("a.txt", "attached"), ("b.txt", "attached")])
        removed = tf.E(48, "attachment_removed", "operator", file="b.txt")
        disp = [tf.E(47, "brief_approved", "operator"), tf.E(46, "dispatched", adapter="codex")]
        c = self.card(self.item([tf.E(60, "item_created", "operator"), a, b, removed] + disp, slug="att2"))
        self.assertEqual([(x["file"], x["state"]) for x in c["attachments"]], [("a.txt", "delivered")])
        late = tf.E(10, "attached", "operator", file="c.txt", bytes=1, sha256="z")
        c = self.card(self.item([tf.E(60, "item_created", "operator"), a] + disp + [late], slug="att3"))
        self.assertEqual({x["file"]: x["state"] for x in c["attachments"]}, {"a.txt": "delivered", "c.txt": "attached"})
        self.assertEqual(c["trail"][-1]["text"], "attached c.txt (1 bytes)")

    def test_actions_table(self):
        queued = self.card(self.item([tf.E(5, "item_created", "operator")], slug="q"))
        self.assertTrue(queued["actions"]["approve"]["ok"] and queued["actions"]["park"]["ok"])
        self.assertFalse(queued["actions"]["unpark"]["ok"])
        self.assertFalse(queued["actions"]["answer"]["ok"])
        self.assertIn("annotation", queued["actions"]["answer"]["why"])
        briefed = self.card(self.item([tf.E(5, "item_created", "operator"), tf.E(4, "brief_approved", "operator")], slug="b"))
        self.assertFalse(briefed["actions"]["approve"]["ok"])
        live = self.card(self.item(tf.lifecycle(), slug="l"))
        self.assertIn("already live", live["actions"]["approve"]["why"])
        self.assertFalse(live["actions"]["park"]["ok"])
        ask = self.card(self.item(tf.lifecycle() + tf.ran() +
                                  [tf.E(60, "input_required", "codex", question="which?", run_id="R")], slug="a"))
        self.assertTrue(ask["actions"]["answer"]["ok"] and ask["actions"]["approve"]["ok"] and ask["actions"]["park"]["ok"])
        done = self.card(self.item(tf.lifecycle() + tf.ran() + tf.verified_pass() + [tf.E(80, "reviewed", "reviewer", result="pass")],
                                   slug="d"))
        self.assertIn("cannot be dispatched from its current stage", done["actions"]["approve"]["why"])
        self.assertFalse(done["actions"]["park"]["ok"])
        blocked = self.card(self.item(tf.lifecycle() + tf.ran() +
                                      [tf.E(60, "verified", result="tests_failed", discrepancies=[], test_exit_code=1)], slug="bl"))
        self.assertTrue(blocked["actions"]["approve"]["ok"])      # a blocked build can be re-dispatched

    def test_posture_live_and_next_are_distinct(self):
        c = self.card(self.item(tf.lifecycle(adapter="claude") + tf.ran() + tf.verified_pass(), slug="p", adapter="claude"))
        live = c["posture"]["live"]
        self.assertEqual((live["adapter"], live["running"]), ("claude", False))
        nxt = c["posture"]["next"]
        self.assertEqual((nxt["adapter"], nxt["sandbox"]), ("claude", "allowed-tools+prompts-none"))
        self.assertEqual(nxt["tools"], ["Bash", "Read", "Write", "Edit", "Grep", "Glob"])
        fresh = self.card(self.item([tf.E(5, "item_created", "operator")], slug="n", adapter="codex"))
        self.assertIsNone(fresh["posture"]["live"])
        self.assertEqual(fresh["posture"]["next"]["sandbox"], "workspace-write")

    def test_receipts_for_every_state(self):
        name = self.item([tf.E(5, "item_created", "operator"),
                          tf.E(4, "message", "operator", text="seen", intent="aaaaaaaa-0000-0000-0000-000000000003")], slug="r")
        d = self.root / "log" / "intents"
        d.mkdir(parents=True)

        def put(n, kind, outcome=None, **data):
            iid = f"aaaaaaaa-0000-0000-0000-00000000000{n}"
            (d / f"{iid}.json").write_text(json.dumps({"id": iid, "ts": f"2026-11-01T12:00:0{n}.000Z", "item": name,
                                                      "kind": kind, "data": data}))
            if outcome:
                floor.write_atomic(floor.outcome_path(self.root, iid), json.dumps({"intent": iid, **outcome}))
        put(1, "message", text="pending one")
        put(2, "park", {"state": "refused", "reason": "nope"}, reason="r")
        put(3, "message", text="seen")                                  # the event is in the log, no outcome yet
        put(4, "approve", {"state": "failed", "reason": "disk"})
        put(5, "attach", {"state": "landed", "event": {"type": "attached", "seq": 9}}, name="a.txt", content_b64="eA==")
        rs = self.card(name)["receipts"]
        self.assertEqual([(r["kind"], r["state"]) for r in rs], [
            ("message", "pending"), ("park", "refused"), ("message", "reconciling"), ("approve", "failed"),
            ("attach", "landed")])
        self.assertEqual(rs[1]["reason"], "nope")
        self.assertEqual(rs[4]["event"], {"type": "attached", "seq": 9})
        self.assertEqual(rs[4]["summary"], "a.txt")
        self.assertNotIn("content_b64", json.dumps(rs))                  # file content never rides the projection

    def test_projection_without_an_intents_dir_has_empty_receipts_and_creates_nothing(self):
        name = self.item([tf.E(5, "item_created", "operator")])
        before = self.yard_hashes()
        self.assertEqual(self.card(name)["receipts"], [])
        self.assertEqual(before, self.yard_hashes())
        self.assertFalse((self.root / "log" / "intents").exists())


class TestEndToEndFromTheFloorServer(PanelCase):
    """The 0010 circuit in miniature: park, unpark, answer, approve - all over HTTP, landed by the daemon."""

    def test_park_unpark_answer_approve_to_verified_pass(self):
        d = self.asked_item()
        srv = floor.make_server(self.yard, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        port = srv.server_address[1]

        def post(kind, **data):
            if kind == "approve":
                data.update(depot.approval_data(depot.Item(d)))
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/intents", body=json.dumps({"kind": kind, "item": d.name, "data": data}),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()
            self.assertEqual(resp.status, 202, body)
            return body["id"]

        # Park and unpark must land in that order (unpark needs a parked card), and two
        # intents posted in the same millisecond tie on timestamp, so the poll's
        # (ts, id) sort can reverse them. Land each before posting the next.
        ids = [post("park", reason="thinking")]
        self.assertEqual([r["state"] for r in self.card(d)["receipts"]], ["pending"])   # dashed, not in log
        self.poll_once()
        self.assertEqual([r["state"] for r in self.card(d)["receipts"]], ["landed"])
        ids += [post("unpark")]
        self.assertEqual([r["state"] for r in self.card(d)["receipts"]], ["landed", "pending"])
        self.poll_once()
        self.assertEqual([r["state"] for r in self.card(d)["receipts"]], ["landed", "landed"])
        ids += [post("message", text="call it foo.py")]
        self.poll_once()
        self.assertEqual(self.card(d)["receipts"][-1]["state"], "landed")
        self.assertNotIn("dispatched", self.types(d)[-4:])                  # nothing ran on answer alone
        ids += [post("approve")]
        self.poll_once()
        self.assertEqual(json.loads(sorted((d / "runs").iterdir())[-1].joinpath("verify.json").read_text())["result"], "pass")
        receipts = self.card(d)["receipts"]
        self.assertEqual([(r["kind"], r["state"]) for r in receipts],
                         [("park", "landed"), ("unpark", "landed"), ("message", "landed"), ("approve", "landed")])
        self.assertEqual([r["id"] for r in receipts], ids)
        self.assertTrue(all(r["event"]["seq"] > 0 for r in receipts))


class TestPanelUsability(unittest.TestCase):
    """Exercise the shipped DOM behavior; geometry and native focus still need browser verification."""
    def run_behavior(self, behavior):
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available")
        result = subprocess.run([node, str(Path(floor.PAGE_DIR) / "test_blocked.cjs"),
                                 "usability", behavior], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_review_controls_explain_empty_note_and_pending_decision(self):
        self.run_behavior("review-reasons")

    def test_recovery_controls_explain_missing_note_and_pending_request(self):
        self.run_behavior("recovery-reasons")

    def test_condition_blocked_guidance_and_question_precede_contract(self):
        self.run_behavior("context-order")

    def test_non_card_focus_survives_updates_and_panel_returns_focus(self):
        self.run_behavior("focus")

    def test_active_and_tabled_track_controls_explain_pending_requests(self):
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available")
        result = subprocess.run([node, str(Path(floor.PAGE_DIR) / "test_track_lifecycle.cjs"),
                                 "pending-reasons"], capture_output=True, text=True)


class TestInterruptedDocumentation(unittest.TestCase):
    def test_legend_lists_interrupted_as_a_column_and_stuck_item(self):
        class LegendText(HTMLParser):
            in_legend = False

            def __init__(self):
                super().__init__()
                self.text = []

            def handle_starttag(self, tag, attrs):
                if tag == "footer" and "legend" in dict(attrs).get("class", "").split():
                    self.in_legend = True

            def handle_endtag(self, tag):
                if tag == "footer":
                    self.in_legend = False

            def handle_data(self, data):
                if self.in_legend:
                    self.text.append(data)

        legend = LegendText()
        legend.feed((floor.PAGE_DIR / "index.html").read_text())
        text = " ".join(legend.text).lower()
        for label in ("blocked", "parked", "stalled?", "interrupted", "column", "stuck"):
            self.assertIn(label, text)

    def test_spec_section_8_columns_match_the_floor_including_interrupted(self):
        spec = (floor.PAGE_DIR.parent / "docs" / "spec-v1-20261030.md").read_text()
        section = spec.split("## 8.", 1)[1].split("## 9.", 1)[0]
        columns = next(line for line in section.splitlines() if "Columns =" in line)
        self.assertIn("Interrupted", columns)
        self.assertIn(" → ".join(floor.STAGES), columns.replace("**", ""))


class TestInterruptedColumn(tf.YardCase):
    def test_stranded_card_renders_in_interrupted_column_and_counts_in_stuck(self):
        interrupted = self.item(tf.lifecycle() + [tf.E(70, "run_interrupted", run_id="run1",
                                                      reason="Completion not recorded")])
        live = self.item(tf.lifecycle(dispatched=1), slug="live")
        projection = floor.project(self.root, tf.NOW)
        script = r"""
const assert = require('node:assert/strict');
const projection = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.attrs = {}; this._text = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this._text = ''; }
  set textContent(v) { this._text = v; this.children = []; }
  get textContent() { return this._text + this.children.map(n => n.textContent).join(' '); }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener() {}
  querySelectorAll(selector) { return all(this, n => hasClass(n, selector.slice(1))); }
}
function all(root, pred) { return root.children.flatMap(n => [...(pred(n) ? [n] : []), ...all(n, pred)]); }
function hasClass(n, name) { return (n.className || '').split(' ').includes(name); }
const ids = Object.fromEntries(['board','banner','panel','announce','theme','readouts','archive','intake','new-work','subhead']
  .map(id => [id, new Element('div')]));
global.document = {
  getElementById: id => ids[id], createElement: tag => new Element(tag),
  documentElement: { setAttribute() {}, removeAttribute() {} }, addEventListener() {}, activeElement: null
};
global.window = { localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} } };
global.setTimeout = () => 1; global.clearTimeout = () => {};
global.fetch = async url => {
  assert.equal(url, '/cards.json');
  return { ok: true, json: async () => projection };
};
require(process.argv[1]);
setImmediate(() => {
  assert.equal(ids.banner.hidden, true, ids.banner.textContent);
  const stations = all(ids.board, n => hasClass(n, 'station'));
  const column = stations.findIndex(n => n.children[0].textContent === 'Interrupted');
  assert.ok(column >= 0, 'Interrupted column is visible');
  assert.equal(stations[column].children[1].textContent, '1');
  const lane = all(ids.board, n => hasClass(n, 'lane'))[0];
  const zones = lane.children.filter(n => hasClass(n, 'zone'));
  const cards = all(zones[column], n => hasClass(n, 'card'));
  assert.deepEqual(cards.map(n => n.dataset.name), [process.argv[2]]);
  assert.ok(cards[0].textContent.includes('interrupted'));
  const building = stations.findIndex(n => n.children[0].textContent === 'Building');
  assert.deepEqual(all(zones[building], n => hasClass(n, 'card')).map(n => n.dataset.name), [process.argv[3]]);
  const stuck = ids.readouts.children.find(n => n.children[0].textContent === 'Stuck');
  assert.equal(stuck.children[1].textContent, '1');
});
"""
        result = subprocess.run(["node", "-e", script, str(floor.PAGE_DIR / "floor.js"), interrupted, live],
                                input=json.dumps(projection), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class TestApproveWarningPage(unittest.TestCase):
    """Page-content check (as in S10's intake statics): the third approve-warning state is in floor.js, gated on an
    open question with no draft and no pending message, and it is shown in the existing warning element."""
    def test_an_open_question_with_no_draft_and_nothing_pending_warns_that_approval_dispatches_unanswered(self):
        js = (Path(floor.PAGE_DIR) / "floor.js").read_text()
        self.assertIn("const unanswered = hasQuestion && blankDraft && !msgPending;", js)
        self.assertIn("P.approveWarn.hidden = !(unsent || msgPending || unanswered);", js)
        self.assertIn(
            "No answer has landed for this question. Approval will dispatch the brief unanswered — "
            "the agent will likely ask the same question again.", js)
        self.assertIn('"Your answer has not landed: it is only a draft. Approval will dispatch without it."', js)
        self.assertIn('"callout warn"', js)                       # existing warning styling; no new colors
        self.assertNotIn("innerHTML", js)




class TestTruthRendering(tf.YardCase):
    def render(self, event, label, glyph, checker):
        import subprocess
        name = self.item(tf.lifecycle() + tf.ran() + tf.verified_pass() + [event])
        card = self.card(name)
        script = Path(__file__).resolve().parent.parent / "floor" / "test_blocked.cjs"
        result = subprocess.run(
            ["node", str(script), "truth"], text=True, capture_output=True,
            input=json.dumps({"card": card, "expected": {"label": label, "glyph": glyph, "checker": checker}}))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_failed_mechanical_checks_render_checked_failed(self):
        for result in ("discrepancies", "tests_failed", "workspace_changed", "commit_failed"):
            with self.subTest(result=result):
                self.render(tf.E(5, "verified", result=result, test_exit_code=1,
                                 discrepancies=[{"path": "unexpected.py", "kind": "extra"}]),
                            "Checked · failed", "✕", "turntable (mechanical)")

    def test_failed_and_held_reviews_render_with_reviewer(self):
        for actor in ("operator", "reviewer"):
            for result in ("fail", "hold"):
                with self.subTest(actor=actor, result=result):
                    self.render(tf.E(5, "reviewed", actor, result=result),
                                "Checked · failed", "✕", "operator" if actor == "operator" else "reviewer")

    def test_failed_gate_renders_checked_failed(self):
        self.render(tf.E(5, "gate_result", "claude", result="fail", gate="0002-sentinel", criteria=[]),
                    "Checked · failed", "✕", "sentinel (claude)")

    def test_passing_checks_still_render_verified(self):
        for event, checker in ((tf.E(5, "verified", result="pass"), "turntable (mechanical)"),
                               (tf.E(5, "reviewed", "operator", result="pass"), "operator"),
                               (tf.E(5, "gate_result", "claude", result="pass", gate="0002-sentinel"),
                                "sentinel (claude)")):
            with self.subTest(type=event["type"]):
                self.render(event, "verified", "✓", checker)

    def test_evidence_legend_lists_fourth_truth_mark(self):
        from html.parser import HTMLParser

        class Legend(HTMLParser):
            def __init__(self):
                super().__init__()
                self.in_legend = False
                self.text = ""

            def handle_starttag(self, tag, attrs):
                if tag == "footer" and dict(attrs).get("aria-label") == "Evidence legend":
                    self.in_legend = True

            def handle_endtag(self, tag):
                if tag == "footer":
                    self.in_legend = False

            def handle_data(self, data):
                if self.in_legend:
                    self.text += data

        legend = Legend()
        legend.feed((Path(__file__).resolve().parent.parent / "floor" / "index.html").read_text())
        for label in ("◇ Claimed", "■ Recorded", "✓ Verified", "✕ Checked · failed"):
            self.assertIn(label, " ".join(legend.text.split()))

class TestMinorFloorBatch(unittest.TestCase):
    def run_behavior(self, behavior):
        import subprocess
        result = subprocess.run(["node", str(floor.PAGE_DIR / "test_minor.cjs"), behavior], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_receipt_decision_summaries(self):
        cases = [
            ("review", {"result": "fail", "note": "Try again", "commit": "abcdef123"}, "Reject · Try again · abcdef1"),
            ("review", {"result": "pass", "note": "Looks good", "commit": "abcdef123"}, "Accept · Looks good · abcdef1"),
            ("recover", {"run_id": "run1", "commit": "abcdef123"}, "Recover · abcdef1 · run1"),
            ("adjudicate", {"run_id": "run1", "note": "Checked"}, "Adjudicate · Checked · run1"),
            ("requeue_interrupted", {"run_id": "run1", "note": "Stopped"}, "Re-queue · Stopped · run1"),
        ]
        for kind, data, expected in cases:
            with self.subTest(kind=kind, data=data):
                self.assertEqual(floor.summarize_intent({"kind": kind, "data": data}), expected)

    def test_outcome_changes_announced_across_card_track_and_board(self):
        self.run_behavior("announcements")

    def test_card_semantics_condition_age_and_path_provenance(self):
        self.run_behavior("card")

    def test_whole_yard_readouts_and_empty_archive(self):
        self.run_behavior("readouts")

    def test_disabled_approval_loses_primary_style_and_grant_copy(self):
        self.run_behavior("approval")

    def test_each_decision_clears_only_its_unchanged_draft_when_landed(self):
        for kind in ("review", "adjudicate", "recover", "requeue_interrupted"):
            with self.subTest(kind=kind):
                self.run_behavior("draft-" + kind)

    def test_evidence_links_adjudication_label_and_zero_done_toggle(self):
        self.run_behavior("polish")

    def test_landed_decision_clears_draft_after_reload(self):
        self.run_behavior("reload")

    def test_decision_cleanup_works_with_storage_blocked(self):
        self.run_behavior("draft-storage")

    def test_park_and_unpark_copy_toggle_hidden_state(self):
        self.run_behavior("hidden")


if __name__ == "__main__":
    unittest.main()
