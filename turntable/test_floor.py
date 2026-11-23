import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import floor  # noqa: E402

DEPOT = str(Path(__file__).resolve().parent / "depot.py")
NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)


def E(mins_ago, type_, actor="turntable", **data):
    """One event, `mins_ago` minutes before NOW."""
    return {"ts": floor.iso_z(NOW - timedelta(minutes=mins_ago)), "type": type_, "actor": actor, "data": data}


def lifecycle(created=100, approved=99, dispatched=98, **kw):
    return [E(created, "item_created", "operator"), E(approved, "brief_approved", "operator"),
            E(dispatched, "dispatched", adapter=kw.get("adapter", "codex"))]


def ran(finished=90):
    return [E(finished, "run_finished", exit_code=0, elapsed_s=30, timed_out=False),
            E(finished, "claimed", "codex", report="r.md")]


def verified_pass(m=89):
    return [E(m, "verified", result="pass", discrepancies=[], test_exit_code=0)]


class YardCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        (self.root / "log").mkdir()
        (self.root / "log" / "events.jsonl").write_text("")
        self.n = 0

    def item(self, events, slug="thing", track="t", glob=(), **wb):
        self.n += 1
        name = f"{self.n:04d}-{slug}"
        d = self.root / "items" / name
        d.mkdir(parents=True)
        base = {"id": f"{self.n:04d}", "slug": slug, "title": f"Title {slug}", "track": track,
                "adapter": "codex", "quiet_after_s": 120}
        base.update(wb)
        (d / "waybill.json").write_text(json.dumps(base))
        (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
        if glob:
            with open(self.root / "log" / "events.jsonl", "a") as f:
                for e in glob:
                    f.write(json.dumps({**e, "item": name}) + "\n")
        return name

    def cards(self, now=NOW):
        return {c["name"]: c for c in floor.project(self.root, now)["cards"]}

    def card(self, name, now=NOW):
        return self.cards(now)[name]

    def yard_hashes(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.root.rglob("*")) if p.is_file()}


class TestStageRows(YardCase):
    def test_queued(self):
        c = self.card(self.item([E(10, "item_created", "operator")]))
        self.assertEqual((c["stage"], c["hands"], c["dwell_s"]), ("Queued", "—", 600))

    def test_briefed(self):
        c = self.card(self.item(lifecycle()[:2]))
        self.assertEqual((c["stage"], c["hands"], c["dwell_s"]), ("Briefed", "Turntable", 99 * 60))

    def test_building_build_kind_names_the_adapter(self):
        c = self.card(self.item(lifecycle(adapter="codex") + [E(1, "heartbeat")]))
        self.assertEqual((c["stage"], c["hands"], c["kind"]), ("Building", "Codex", "build"))
        self.assertEqual(c["dwell_s"], 98 * 60)  # dwell counts from dispatched; heartbeat is not a stage change

    def test_gating_gate_kind(self):
        c = self.card(self.item(lifecycle(adapter="claude") + [E(1, "heartbeat")], kind="gate", target="0001-x",
                                adapter="claude"))
        self.assertEqual((c["stage"], c["hands"], c["kind"]), ("Gating", "Claude (gate)", "gate"))

    def test_run_finished_and_claimed_without_verified_is_building_turntable(self):
        c = self.card(self.item(lifecycle() + ran(90)))
        self.assertEqual((c["stage"], c["hands"]), ("Building", "Turntable"))
        self.assertEqual(c["dwell_s"], 98 * 60)  # still the same stage; the clock did not restart

    def test_verified_pass_is_review_cinder(self):
        c = self.card(self.item(lifecycle() + ran() + verified_pass(89) + [E(89, "committed", hash="a" * 40)]))
        self.assertEqual((c["stage"], c["hands"], c["dwell_s"], c["condition"]), ("Review", "reviewer", 89 * 60, None))

    def test_pushed_changes_no_stage(self):
        base = lifecycle() + ran() + verified_pass(89)
        before = self.card(self.item(base, slug="a"))
        after = self.card(self.item(base + [E(88, "committed", hash="a" * 40),
                                            E(88, "pushed", branch="b", remote_head="b" * 40)], slug="b"))
        self.assertEqual((before["stage"], before["dwell_s"]), (after["stage"], after["dwell_s"]))

    def test_reviewed_pass_is_done(self):
        c = self.card(self.item(lifecycle() + ran() + verified_pass(89) + [E(30, "reviewed", "reviewer", result="pass")]))
        self.assertEqual((c["stage"], c["hands"], c["dwell_s"], c["condition"]), ("Done", "—", 30 * 60, None))

    def test_reviewed_not_pass_is_review_blocked(self):
        c = self.card(self.item(lifecycle() + ran() + verified_pass(89)
                                + [E(30, "reviewed", "reviewer", result="defects", reason="two defects found")]))
        self.assertEqual((c["stage"], c["hands"], c["condition"]), ("Review", "reviewer", "blocked"))
        self.assertIn("two defects found", c["condition_reason"])
        self.assertEqual(c["dwell_s"], 89 * 60)  # stage did not change

    def test_input_required_is_awaiting_operator_then_superseded(self):
        base = lifecycle() + [E(50, "run_finished", exit_code=0, elapsed_s=5, timed_out=False),
                              E(50, "claimed", "codex", report="r.md"),
                              E(50, "input_required", "codex", question="Which name do you want?")]
        c = self.card(self.item(base, slug="q"))
        self.assertEqual((c["stage"], c["hands"], c["dwell_s"]), ("Awaiting operator", "operator", 50 * 60))
        self.assertIn("Which name", c["last_event"]["text"])
        again = self.card(self.item(base + [E(10, "dispatched", adapter="codex")], slug="r"))
        self.assertEqual((again["stage"], again["hands"]), ("Building", "Codex"))
        self.assertEqual(again["dwell_s"], 10 * 60)

    def test_heartbeat_after_input_required_does_not_supersede(self):
        base = lifecycle() + [E(50, "run_finished", exit_code=0, elapsed_s=5, timed_out=False),
                              E(50, "input_required", "codex", question="?"), E(5, "heartbeat")]
        self.assertEqual(self.card(self.item(base))["stage"], "Awaiting operator")


class TestBlocked(YardCase):
    def test_verified_not_pass_blocks_in_place(self):
        for result, extra in (("tests_failed", {"test_exit_code": 1}),
                              ("discrepancies", {"discrepancies": [{"path": "__pycache__/x.pyc", "kind": "extra"}]})):
            with self.subTest(result=result):
                data = {"result": result, "discrepancies": [], "test_exit_code": 0, **extra}
                c = self.card(self.item(lifecycle() + ran() + [E(89, "verified", **data)], slug=result))
                self.assertEqual((c["stage"], c["hands"], c["condition"]), ("Building", "Turntable", "blocked"))
                self.assertIn(result, c["condition_reason"])

    def test_push_failed_blocks_in_place_with_error(self):
        c = self.card(self.item(lifecycle() + ran() + verified_pass(89) + [
            E(88, "committed", hash="a" * 40), E(88, "push_failed", branch="b", error="remote: rejected\nmore")]))
        self.assertEqual((c["stage"], c["hands"], c["condition"]), ("Review", "Turntable", "blocked"))
        self.assertEqual(c["condition_reason"], "push failed: remote: rejected")

    def test_failed_gate_blocks_the_target_not_the_gate(self):
        gate_ev = E(20, "gate_result", "claude", result="fail", gate="0002-g", run_id="r",
                    criteria=["CRITERION 1: PASS — a", "CRITERION 2: FAIL — b"])
        target = self.item(lifecycle() + ran() + verified_pass(89) + [gate_ev], slug="target")
        gate = self.item(lifecycle(adapter="claude") + ran(21), slug="g", kind="gate", target=target, adapter="claude")
        cards = self.cards()
        self.assertEqual((cards[target]["stage"], cards[target]["condition"]), ("Review", "blocked"))
        self.assertIn("0002-g", cards[target]["condition_reason"])
        self.assertEqual((cards[gate]["stage"], cards[gate]["condition"]), ("Done", None))
        self.assertEqual(cards[gate]["last_event"]["text"], "gate_result: fail (1/2)")

    def test_passed_gate_leaves_target_unblocked(self):
        gate_ev = E(20, "gate_result", "claude", result="pass", gate="0002-g", run_id="r",
                    criteria=["CRITERION 1: PASS — a"])
        target = self.item(lifecycle() + ran() + verified_pass(89) + [gate_ev], slug="target")
        c = self.card(target)
        self.assertEqual((c["stage"], c["condition"], c["dwell_s"]), ("Review", None, 89 * 60))

    def test_blocked_clears_on_new_dispatch(self):
        c = self.card(self.item(lifecycle() + ran() + [E(89, "verified", result="tests_failed", discrepancies=[],
                                                          test_exit_code=1), E(1, "dispatched", adapter="codex")]))
        self.assertEqual((c["stage"], c["condition"]), ("Building", None))


class TestGateCompletion(YardCase):
    def test_gate_done_via_targets_trail(self):
        gate_ev = E(20, "gate_result", "claude", result="pass", gate="0002-g", run_id="r",
                    criteria=["CRITERION 1: PASS — a", "CRITERION 2: PASS — b"])
        self.item(lifecycle() + ran() + verified_pass(89) + [gate_ev], slug="target")
        g = self.item(lifecycle(adapter="claude") + ran(21), slug="g", kind="gate", adapter="claude")
        c = self.card(g)
        self.assertEqual((c["stage"], c["hands"], c["dwell_s"]), ("Done", "—", 20 * 60))
        self.assertEqual((c["truth"], c["checker"]), ("verified", "g (claude)"))
        self.assertEqual(c["last_event"]["text"], "gate_result: pass (2/2)")

    def test_gate_done_via_global_log_only(self):
        gate_ev = E(20, "gate_result", "claude", result="pass", gate="0001-g", run_id="r", criteria=[])
        g = self.item(lifecycle(adapter="claude") + ran(21), slug="g", kind="gate", adapter="claude", glob=[gate_ev])
        self.assertEqual(self.card(g)["stage"], "Done")

    def test_gate_without_gate_result_is_not_done(self):
        g = self.item(lifecycle(adapter="claude") + ran(21), slug="g", kind="gate", adapter="claude")
        self.assertEqual(self.card(g)["stage"], "Building")

    def test_unparseable_gate_is_done_and_recorded_not_verified(self):
        gate_ev = E(20, "gate_result", "claude", result="unparseable", gate="0002-g", run_id="r", report="r.md")
        target = self.item(lifecycle() + ran() + verified_pass(89) + [gate_ev], slug="target")
        g = self.item(lifecycle(adapter="claude") + ran(21), slug="g", kind="gate", adapter="claude")
        cards = self.cards()
        self.assertEqual(cards[g]["stage"], "Done")
        self.assertEqual((cards[g]["truth"], cards[g]["checker"]), ("recorded", None))
        self.assertEqual(cards[target]["condition"], "blocked")


class TestConditions(YardCase):
    def test_stalled_when_newest_heartbeat_is_older_than_quiet_after(self):
        base = lifecycle(dispatched=30)
        fresh = self.card(self.item(base + [E(1, "heartbeat")], slug="fresh"))
        stale = self.card(self.item(base + [E(5, "heartbeat")], slug="stale"))  # 5m > 120s
        self.assertEqual((fresh["condition"], fresh["condition_reason"]), (None, None))
        self.assertEqual(stale["condition"], "stalled?")
        self.assertIn("5m", stale["condition_reason"])

    def test_stalled_uses_dispatched_when_there_is_no_heartbeat(self):
        c = self.card(self.item(lifecycle(dispatched=10)))
        self.assertEqual(c["condition"], "stalled?")
        c2 = self.card(self.item(lifecycle(dispatched=1), slug="young"))
        self.assertIsNone(c2["condition"])

    def test_stalled_respects_the_waybills_quiet_after_s(self):
        c = self.card(self.item(lifecycle(dispatched=10), quiet_after_s=3600))
        self.assertIsNone(c["condition"])

    def test_gating_can_stall(self):
        c = self.card(self.item(lifecycle(dispatched=10, adapter="claude"), kind="gate", adapter="claude"))
        self.assertEqual((c["stage"], c["condition"]), ("Gating", "stalled?"))

    def test_other_stages_never_stall(self):
        c = self.card(self.item(lifecycle() + ran() + verified_pass(89)))
        self.assertEqual((c["stage"], c["condition"]), ("Review", None))

    def test_blocked_outranks_stalled(self):
        c = self.card(self.item(lifecycle() + ran() + [E(89, "verified", result="tests_failed", discrepancies=[],
                                                          test_exit_code=1)]))
        self.assertEqual(c["condition"], "blocked")

    def test_parked_row_is_supported(self):
        c = self.card(self.item([E(10, "item_created", "operator"), E(5, "parked", "operator", reason="waits on fictional layout")]))
        self.assertEqual((c["stage"], c["condition"], c["condition_reason"]), ("Queued", "parked", "waits on fictional layout"))

    def test_no_parked_without_a_parked_event(self):
        self.assertIsNone(self.card(self.item([E(10, "item_created", "operator")]))["condition"])


class TestTruth(YardCase):
    def last(self, extra, slug="x", base=None):
        c = self.card(self.item((base or lifecycle() + ran() + verified_pass(89)) + extra, slug=slug))
        return c["truth"], c["checker"], c["last_event"]["type"]

    def test_claimed(self):
        self.assertEqual(self.last([], base=lifecycle() + ran()), ("claimed", None, "claimed"))

    def test_recorded_types(self):
        for i, (events, typ) in enumerate([
                ([E(10, "item_created", "operator")], "item_created"),
                (lifecycle()[:2], "brief_approved"),
                (lifecycle(), "dispatched"),
                (lifecycle() + [E(1, "heartbeat")], "heartbeat"),
                (lifecycle() + [E(5, "run_finished", exit_code=0, elapsed_s=1, timed_out=False)], "run_finished"),
                (lifecycle() + ran() + verified_pass() + [E(5, "committed", hash="a" * 40)], "committed"),
                (lifecycle() + ran() + verified_pass() + [E(5, "pushed", branch="b", remote_head="b" * 40)], "pushed")]):
            with self.subTest(type=typ):
                self.assertEqual(self.last([], slug=f"r{i}", base=events), ("recorded", None, typ))

    def test_verified_mechanical(self):
        self.assertEqual(self.last([]), ("verified", "turntable (mechanical)", "verified"))

    def test_failed_check_truth_on_card_and_trail(self):
        for i, (event, checker) in enumerate([
                (E(5, "verified", result="discrepancies"), "turntable (mechanical)"),
                (E(5, "reviewed", "operator", result="fail"), "operator"),
                (E(5, "reviewed", "reviewer", result="hold"), "reviewer"),
                (E(5, "gate_result", "claude", result="fail", gate="0002-sentinel"), "sentinel (claude)")]):
            with self.subTest(event=event):
                card = self.card(self.item(lifecycle() + ran() + verified_pass() + [event], slug=f"failed-{i}"))
                self.assertEqual((card["truth"], card["checker"]), ("checked_failed", checker))
                self.assertEqual((card["trail"][-1]["truth"], card["trail"][-1]["checker"]),
                                 ("checked_failed", checker))

    def test_gate_result_checker_is_gate_slug_and_adapter(self):
        ev = E(5, "gate_result", "claude", result="pass", gate="0002-gate-sentinel", run_id="r", criteria=[])
        self.assertEqual(self.last([ev]), ("verified", "gate-sentinel (claude)", "gate_result"))

    def test_reviewed_checker_is_cinder(self):
        self.assertEqual(self.last([E(5, "reviewed", "reviewer", result="pass")]), ("verified", "reviewer", "reviewed"))

    def test_checker_not_supplied_when_the_data_cannot_name_it(self):
        no_gate = E(5, "gate_result", "claude", result="pass", criteria=[])
        no_actor = E(5, "gate_result", "", result="pass", gate="0002-g", criteria=[])
        for i, ev in enumerate((no_gate, no_actor)):
            truth, checker, _ = self.last([ev], slug=f"n{i}")
            self.assertEqual((truth, checker), ("verified", "Checker: not supplied"))
        self.assertEqual(floor.NO_CHECKER, "Checker: not supplied")


class TestLastEventAndShape(YardCase):
    def test_last_event_fields_and_texts(self):
        c = self.card(self.item(lifecycle() + ran() + verified_pass(89)))
        self.assertEqual(set(c["last_event"]), {"type", "actor", "ts", "text"})
        self.assertEqual(c["last_event"]["text"], "tests re-run passed")
        self.assertEqual(c["last_event"]["actor"], "turntable")

    def test_card_fields_and_ordering(self):
        self.item(lifecycle(), slug="b", track="zeta")
        self.item(lifecycle(), slug="a", track="alpha")
        out = floor.project(self.root, NOW)
        self.assertEqual(out["stages"], ["Queued", "Briefed", "Building", "Gating", "Review", "Awaiting operator", "Interrupted", "Done"])
        self.assertEqual([t["id"] for t in out["tracks"]], ["alpha", "zeta"])
        c = out["cards"][0]
        for k in ("id", "slug", "title", "track", "kind", "stage", "hands", "dwell_s", "last_event", "truth",
                  "condition", "condition_reason", "checker", "trail"):
            self.assertIn(k, c)

    def test_trail_lists_the_path_without_heartbeats(self):
        c = self.card(self.item(lifecycle() + [E(60, "heartbeat"), E(59, "heartbeat")] + ran() + verified_pass()))
        self.assertEqual([t["type"] for t in c["trail"]],
                         ["item_created", "brief_approved", "dispatched", "run_finished", "claimed", "verified"])
        self.assertEqual(c["heartbeats"], 2)


class TestPurityAndFailure(YardCase):
    def test_projection_is_a_pure_function_of_files_and_now(self):
        self.item(lifecycle() + ran() + verified_pass())
        self.item(lifecycle(dispatched=10), slug="s")
        a = floor.cards_json(self.root, NOW)
        b = floor.cards_json(self.root, NOW)
        self.assertEqual(a, b)
        later = floor.cards_json(self.root, NOW + timedelta(hours=1))
        self.assertNotEqual(a, later)  # only `now` moved

    def test_projection_does_not_touch_the_yard(self):
        self.item(lifecycle() + ran() + verified_pass())
        before = self.yard_hashes()
        floor.project(self.root, NOW)
        self.assertEqual(before, self.yard_hashes())

    def test_now_is_required_to_be_aware(self):
        with self.assertRaises(ValueError):
            floor.project(self.root, datetime(2026, 1, 1))

    def test_unknown_event_type_raises(self):
        self.item([E(10, "item_created", "operator"), E(5, "teleported")])
        with self.assertRaises(floor.FloorError):
            floor.project(self.root, NOW)

    def test_impossible_trails_raise(self):
        cases = {
            "empty": [],
            "no_created": [E(5, "brief_approved", "operator")],
            "dispatch_unapproved": [E(9, "item_created", "operator"), E(5, "dispatched", adapter="codex")],
            "finished_undispatched": [E(9, "item_created", "operator"), E(5, "run_finished")],
            "verified_unrun": lifecycle() + [E(5, "verified", result="pass")],
            "committed_unverified": lifecycle() + ran() + [E(5, "committed", hash="a")],
        }
        for label, events in cases.items():
            with self.subTest(label):
                yard = YardCase()
                yard.setUp()
                try:
                    yard.item(events)
                    with self.assertRaises(floor.FloorError):
                        floor.project(yard.root, NOW)
                finally:
                    yard._td.cleanup()


class TestServer(YardCase):
    def setUp(self):
        super().setUp()
        self.item(lifecycle() + ran() + verified_pass())
        self.srv = floor.make_server(self.root, 0, now_fn=lambda: NOW)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return r.status, r.read(), r.headers

    def status_of(self, method, path="/cards.json", headers=None):
        req = urllib.request.Request(self.base + path, method=method, headers=headers or {},
                                     data=b"x" if method in ("POST", "PUT", "PATCH") else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    def test_binds_localhost_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")

    def test_get_page_script_and_cards(self):
        s, body, _ = self.get("/")
        self.assertEqual(s, 200)
        self.assertIn(b"Stage map", body)
        s, body, _ = self.get("/floor.js")
        self.assertEqual(s, 200)
        s, body, h = self.get("/cards.json")
        self.assertEqual(s, 200)
        self.assertIn("application/json", h["Content-Type"])
        self.assertEqual(json.loads(body)["cards"][0]["stage"], "Review")

    def test_cards_json_is_byte_stable_for_the_same_now(self):
        self.assertEqual(self.get("/cards.json")[1], self.get("/cards.json")[1])
        self.assertEqual(self.get("/cards.json")[1], floor.cards_json(self.root, NOW))

    def test_get_foreign_host_is_refused_on_every_path(self):
        for path in ("/cards.json", "/", "/floor.js",
                     "/records/0001-thing/run-1/verify.json", "/records/0001-thing/run-1/report.md",
                     "/missing"):
            with self.subTest(path=path):
                self.assertEqual(self.status_of("GET", path, {"Host": "evil.example"}), 403)

    def test_get_foreign_origin_is_refused_on_every_path(self):
        for path in ("/cards.json", "/", "/floor.js",
                     "/records/0001-thing/run-1/verify.json", "/records/0001-thing/run-1/report.md",
                     "/missing"):
            with self.subTest(path=path):
                self.assertEqual(self.status_of("GET", path, {"Origin": "https://evil.example"}), 403)

    def test_get_local_host_and_origin_serve_unchanged(self):
        port = self.srv.server_address[1]
        for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
            for origin in (None, f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                with self.subTest(host=host, origin=origin):
                    headers = {"Host": host}
                    if origin is not None:
                        headers["Origin"] = origin
                    req = urllib.request.Request(self.base + "/cards.json", headers=headers)
                    with urllib.request.urlopen(req) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.read(), floor.cards_json(self.root, NOW))

    def test_foreign_headers_are_refused_by_other_handlers(self):
        for method, path in (("POST", "/intents"), ("POST", "/missing"),
                             *((m, "/cards.json") for m in ("PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"))):
            for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"}):
                with self.subTest(method=method, path=path, headers=headers):
                    self.assertEqual(self.status_of(method, path, headers), 403)

    def test_every_non_get_is_refused_405(self):
        for m in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            with self.subTest(method=m):
                self.assertEqual(self.status_of(m), 405)

    def test_unknown_path_404(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/../etc/passwd")
        self.assertEqual(cm.exception.code, 404)
        cm.exception.close()

    def test_serving_leaves_the_yard_byte_identical(self):
        before = self.yard_hashes()
        for p in ("/", "/floor.js", "/cards.json", "/cards.json"):
            self.get(p)
        self.status_of("POST")
        self.assertEqual(before, self.yard_hashes())

    def test_bad_yard_state_is_a_500_with_the_reason(self):
        self.item([E(10, "item_created", "operator"), E(5, "teleported")], slug="bad")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/cards.json")
        self.assertEqual(cm.exception.code, 500)
        self.assertIn("teleported", json.loads(cm.exception.read())["error"])
        cm.exception.close()


class TestPageWritesOnlyIntents(unittest.TestCase):
    """S8: the page gained a write panel, so the old blanket "no mutation affordances" test is narrowed to what is
    still true: the page can send exactly one thing, an intent to POST /intents; it cannot move or edit the board."""
    def test_the_only_write_the_page_can_make_is_post_intents(self):
        page = Path(floor.PAGE_DIR)
        html, js = (page / "index.html").read_text(), (page / "floor.js").read_text()
        for needle in ("<form", "draggable", "ondrag", "contenteditable"):
            self.assertNotIn(needle, html)
        for needle in ("PUT", "DELETE", "PATCH", "dragstart", "innerHTML", "XMLHttpRequest"):
            self.assertNotIn(needle, js)
        self.assertEqual(js.count("method:"), 1)                  # one write call site...
        self.assertIn('fetch("/intents", {\n        method: "POST"', js)  # ...and it is POST /intents
        self.assertEqual(js.count("fetch("), 2)                   # /cards.json and /intents, nothing else

    def test_dark_mode_ships_with_a_persisted_toggle_and_a_system_default(self):
        page = Path(floor.PAGE_DIR)
        html, js = (page / "index.html").read_text(), (page / "floor.js").read_text()
        self.assertIn("prefers-color-scheme: dark", html)
        self.assertIn(':root[data-theme="dark"]', html)
        self.assertIn('data-theme', js)
        self.assertIn("depot.theme", js)

    def test_page_is_driven_by_the_json_not_hardcoded_cards(self):
        js = (Path(floor.PAGE_DIR) / "floor.js").read_text()
        self.assertIn('fetch("/cards.json"', js)
        self.assertNotIn("sample-data", (Path(floor.PAGE_DIR) / "index.html").read_text())


class TestFloorCommand(YardCase):
    def test_cli_serves_localhost_and_writes_nothing(self):
        self.item(lifecycle() + ran() + verified_pass())
        before = self.yard_hashes()
        env = {**os.environ, "DEPOT_YARD": str(self.root), "DEPOT_FLOOR_PORT": "0"}
        proc = subprocess.Popen([sys.executable, DEPOT, "floor"], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline()
            self.assertIn("http://127.0.0.1:", line)
            url = line.split()[1]
            with urllib.request.urlopen(url + "cards.json") as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(json.loads(r.read())["cards"][0]["id"], "0001")
            req = urllib.request.Request(url + "cards.json", method="POST", data=b"{}")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code, 405)
            cm.exception.close()
        finally:
            proc.terminate()
            proc.communicate(timeout=10)
        self.assertEqual(before, self.yard_hashes())  # no logs, no files in the yard


class TestDemoYard(unittest.TestCase):
    """The demo yard's answer key must be what the projection actually yields."""

    def test_seed_design_matches_the_answer_key(self):
        import demo_yard
        with tempfile.TemporaryDirectory() as td:
            demo_yard.build(Path(td) / "demo")
            cards = {c["id"]: c for c in floor.project(Path(td) / "demo", demo_yard.DEMO_NOW)["cards"]}
        self.assertEqual(sorted(i for i, c in cards.items() if c["stage"] == "Awaiting operator"), ["6001", "6008"])
        self.assertEqual((cards["6009"]["stage"], cards["6009"]["hands"]), ("Review", "reviewer"))
        stuck = sorted(i for i, c in cards.items() if c["condition"] in ("blocked", "stalled?"))
        self.assertEqual(stuck, ["6004", "6009", "6012"])
        self.assertEqual((cards["6004"]["condition"], cards["6004"]["stage"]), ("stalled?", "Gating"))
        self.assertIn("palette", cards["6009"]["condition_reason"])
        self.assertEqual(cards["6012"]["condition_reason"], "verification: tests_failed")
        self.assertEqual((cards["6006"]["condition"], cards["6006"]["stage"]), ("parked", "Queued"))
        self.assertEqual(cards["6005"]["stage"], "Briefed")
        self.assertEqual(cards["6011"]["stage"], "Queued")
        s = cards["6003"]
        self.assertEqual((s["stage"], s["truth"], s["checker"]), ("Done", "verified", "reviewer"))
        types = [t["type"] for t in s["trail"]]
        self.assertLess(types.index("gate_result"), types.index("reviewed"))
        self.assertEqual(cards["6007"]["truth"], "claimed")
        self.assertEqual((cards["6002"]["truth"], cards["6002"]["condition"]), ("recorded", None))
        self.assertEqual(max(sum(1 for c in cards.values() if (c["track"], c["stage"]) == key)
                             for key in {(c["track"], c["stage"]) for c in cards.values()}), 1)

    def test_refuses_a_non_demo_directory(self):
        import demo_yard
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "precious.txt").write_text("x")
            with self.assertRaises(SystemExit):
                demo_yard.build(td)
            self.assertTrue((Path(td) / "precious.txt").exists())





class TestConditionAges(YardCase):
    def test_block_park_and_stall_use_their_own_clocks(self):
        events = lifecycle(created=600, approved=590, dispatched=480) + ran(finished=470)
        name = self.item(events + [E(312, "blocked", reason="Waiting"), E(10, "message", "operator", text="Still waiting")])
        self.assertEqual((self.card(name)["dwell_s"], self.card(name)["condition_age_s"]), (28800, 18720))
        parked = self.item([E(500, "item_created"), E(120, "parked", reason="Hold"), E(20, "message", text="Note")])
        self.assertEqual(self.card(parked)["condition_age_s"], 7200)
        stalled = self.item(lifecycle(created=600, approved=590, dispatched=480) + [E(221, "heartbeat", since_last_adapter_event_s=0)])
        self.assertEqual(self.card(stalled)["condition_age_s"], 13260)

    def test_repeated_block_and_release_reset_condition_clock(self):
        name = self.item([E(500, "item_created"), E(300, "blocked", reason="Wait"), E(120, "blocked", reason="Wait")])
        self.assertEqual(self.card(name)["condition_age_s"], 7200)
        released = self.item([E(500, "item_created"), E(300, "parked", reason="Wait"), E(120, "unparked")])
        self.assertIsNone(self.card(released)["condition_age_s"])


if __name__ == "__main__":
    unittest.main()
