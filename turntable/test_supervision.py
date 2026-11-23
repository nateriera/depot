"""S5 tests: supervision marks from adapter evidence, track budgets, and the daemon refusal cap.
New file: the S1-S4 suites are untouched."""
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import depot  # noqa: E402
import floor  # noqa: E402
import test_floor as tf  # noqa: E402
from test_depot import TurntableCase  # noqa: E402

E, NOW = tf.E, tf.NOW
REPO = Path(__file__).resolve().parent.parent


def hb(mins_ago, since, elapsed):
    return E(mins_ago, "heartbeat", since_last_adapter_event_s=since, elapsed_s=elapsed)


class TestAdapterQuiet(tf.YardCase):
    def test_stalled_when_adapter_is_quiet_though_heartbeats_are_fresh(self):
        # The gap S5 closes: the turntable heartbeated 6s ago, but the adapter has said nothing for 400s.
        events = tf.lifecycle(dispatched=10) + [hb(0.1, 400, 500)]
        c = self.card(self.item(events, quiet_after_s=300, timeout_s=900))
        self.assertEqual(c["condition"], "stalled?")
        self.assertEqual(c["last_adapter_event_age_s"], 406)
        self.assertEqual(c["elapsed_s"], 506)

    def test_no_mark_while_the_adapter_is_speaking_inside_the_budget(self):
        events = tf.lifecycle(dispatched=10) + [hb(0.5, 20, 570)]
        c = self.card(self.item(events, quiet_after_s=300, timeout_s=900))
        self.assertEqual((c["condition"], c["condition_reason"]), (None, None))
        self.assertEqual((c["last_adapter_event_age_s"], c["elapsed_s"], c["timeout_s"]), (50, 600, 900))

    def test_old_heartbeats_alone_no_longer_decide(self):
        # Pre-S5 a 5-minute-old heartbeat raised the mark; now only the adapter's own silence does.
        events = tf.lifecycle(dispatched=30) + [hb(5, 2, 1500)]
        c = self.card(self.item(events, quiet_after_s=420, timeout_s=3600))
        self.assertEqual(c["last_adapter_event_age_s"], 5 * 60 + 2)
        self.assertIsNone(c["condition"])

    def test_evidence_carries_age_elapsed_and_budget(self):
        events = tf.lifecycle(dispatched=8) + [hb(0, 192, 400)]
        c = self.card(self.item(events, quiet_after_s=45, timeout_s=600))
        self.assertEqual(c["condition_reason"],
                         "no adapter event for 3m 12s · elapsed 6m 40s of 10m budget · quiet limit 45s")

    def test_no_heartbeat_yet_falls_back_to_the_dispatch_age(self):
        c = self.card(self.item(tf.lifecycle(dispatched=2), quiet_after_s=60, timeout_s=600))
        self.assertEqual((c["condition"], c["last_adapter_event_age_s"], c["elapsed_s"]), ("stalled?", 120, 120))
        self.assertIn("no adapter event for 2m · elapsed 2m of 10m budget", c["condition_reason"])
        young = self.card(self.item(tf.lifecycle(dispatched=0.5), quiet_after_s=60, slug="young"))
        self.assertIsNone(young["condition"])

    def test_asked_suppresses_stalled(self):
        events = tf.lifecycle(dispatched=30) + [hb(5, 900, 1500)] + tf.ran(4) + [
            E(3, "input_required", "codex", question="Which?", run_id="r")]
        c = self.card(self.item(events, quiet_after_s=60))
        self.assertEqual((c["stage"], c["condition"]), ("Awaiting operator", None))
        self.assertIsNone(c["last_adapter_event_age_s"])

    def test_blocked_outranks_stalled(self):
        events = tf.lifecycle(dispatched=30) + [hb(0, 900, 1500), E(0, "blocked", reason="refused")]
        c = self.card(self.item(events, quiet_after_s=60))
        self.assertEqual((c["condition"], c["condition_reason"]), ("blocked", "refused"))

    def test_a_timed_out_run_is_finished_and_carries_no_mark(self):
        events = tf.lifecycle(dispatched=30) + [hb(10, 600, 900),
                                                E(9, "run_finished", exit_code=-9, elapsed_s=900, timed_out=True),
                                                E(9, "claimed", "codex", report="r.md")]
        c = self.card(self.item(events, quiet_after_s=60))
        self.assertEqual((c["stage"], c["condition"]), ("Building", None))
        self.assertIsNone(c["last_adapter_event_age_s"])

    def test_non_live_cards_have_null_live_numbers_but_a_budget(self):
        c = self.card(self.item(tf.lifecycle() + tf.ran() + tf.verified_pass(), timeout_s=600))
        self.assertEqual((c["last_adapter_event_age_s"], c["elapsed_s"], c["timeout_s"]), (None, None, 600))

    def test_heartbeats_stay_out_of_the_trail_and_move_no_stage(self):
        c = self.card(self.item(tf.lifecycle(dispatched=10) + [hb(0, 5, 100)]))
        self.assertEqual((c["stage"], c["dwell_s"]), ("Building", 600))  # a heartbeat restarts no clock
        self.assertNotIn("heartbeat", [t["type"] for t in c["trail"]])
        self.assertEqual(c["heartbeats"], 1)

    def test_no_verdict_language_in_marks_or_page(self):
        c = self.card(self.item(tf.lifecycle(dispatched=8) + [hb(0, 192, 400)], quiet_after_s=45))
        for word in ("hung", "dead", "failed"):
            self.assertNotIn(word, c["condition_reason"].lower())
        page = (REPO / "floor" / "floor.js").read_text().lower()
        for word in ("hung", "dead"):
            self.assertNotIn(word, page)

    def test_projection_still_writes_nothing(self):
        self.item(tf.lifecycle(dispatched=8) + [hb(0, 192, 400)])
        before = self.yard_hashes()
        floor.cards_json(self.root, NOW)
        self.assertEqual(self.yard_hashes(), before)


class TestFloorBudgetFallback(tf.YardCase):
    def bare(self, events, **wb):
        """An item whose waybill lacks timeout_s / quiet_after_s entirely (filed by hand)."""
        name = self.item(events, **wb)
        p = self.root / "items" / name / "waybill.json"
        w = json.loads(p.read_text())
        w.pop("timeout_s", None)
        w.pop("quiet_after_s", None)
        p.write_text(json.dumps(w))
        return name

    def test_builtin_defaults_when_the_waybill_and_tracks_say_nothing(self):
        c = self.card(self.bare(tf.lifecycle(dispatched=4)))  # 4m < the built-in 300s
        self.assertEqual((c["timeout_s"], c["condition"]), (900, None))
        c = self.card(self.bare(tf.lifecycle(dispatched=6), slug="old"))
        self.assertEqual(c["condition"], "stalled?")
        self.assertIn("quiet limit 5m", c["condition_reason"])

    def test_tracks_json_default_then_track_entry(self):
        (self.root / "tracks.json").write_text(json.dumps({
            "_default": {"timeout_s": 600, "quiet_after_s": 60}, "t": {"quiet_after_s": 3600}}))
        c = self.card(self.bare(tf.lifecycle(dispatched=2)))  # track t: quiet 3600, timeout from _default
        self.assertEqual((c["timeout_s"], c["condition"]), (600, None))
        other = self.card(self.bare(tf.lifecycle(dispatched=2), slug="o", track="elsewhere"))
        self.assertEqual((other["timeout_s"], other["condition"]), (600, "stalled?"))

    def test_the_waybill_beats_tracks_json(self):
        (self.root / "tracks.json").write_text(json.dumps({"_default": {"timeout_s": 600, "quiet_after_s": 60}}))
        c = self.card(self.item(tf.lifecycle(dispatched=2), timeout_s=1200, quiet_after_s=3600))
        self.assertEqual((c["timeout_s"], c["condition"]), (1200, None))

    def test_a_corrupt_tracks_json_does_not_break_the_floor(self):
        (self.root / "tracks.json").write_text("{not json")
        c = self.card(self.bare(tf.lifecycle(dispatched=2)))
        self.assertEqual(c["timeout_s"], 900)


class TestTrackBudgets(TurntableCase):
    def file(self, track="tr", slug="x", *flags):
        r = self.depot("new-item", "--title", "T", "--slug", slug, "--track", track, *flags)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads((self.yard / "items" / r.stdout.strip() / "waybill.json").read_text())

    def tracks(self, data):
        (self.yard / "tracks.json").write_text(json.dumps(data))

    def test_builtin_defaults_without_a_tracks_file(self):
        wb = self.file()
        self.assertEqual((wb["timeout_s"], wb["quiet_after_s"]), (900, 300))

    def test_default_entry_applies_to_an_unlisted_track(self):
        self.tracks({"_default": {"timeout_s": 700, "quiet_after_s": 70}})
        wb = self.file()
        self.assertEqual((wb["timeout_s"], wb["quiet_after_s"]), (700, 70))

    def test_track_entry_beats_default_field_by_field(self):
        self.tracks({"_default": {"timeout_s": 700, "quiet_after_s": 70}, "tr": {"quiet_after_s": 45}})
        wb = self.file()
        self.assertEqual((wb["timeout_s"], wb["quiet_after_s"]), (700, 45))

    def test_explicit_values_beat_everything(self):
        self.tracks({"_default": {"timeout_s": 700, "quiet_after_s": 70}, "tr": {"timeout_s": 800, "quiet_after_s": 80}})
        wb = self.file("tr", "y", "--timeout-s", "600", "--quiet-after-s", "45")
        self.assertEqual((wb["timeout_s"], wb["quiet_after_s"]), (600, 45))

    def test_a_bad_tracks_file_refuses_and_leaves_no_item(self):
        self.tracks({"_default": {"timeout_s": -5}})
        r = self.depot("new-item", "--title", "T", "--slug", "x", "--track", "tr")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("tracks.json", r.stderr)
        self.assertFalse((self.yard / "items").exists() and any((self.yard / "items").iterdir()))

    def test_the_seeded_tracks_file_has_only_the_default_entry(self):
        # The seed contract, against this test's own yard (never the live tracks.json, which grows as tracks are filed):
        # a yard seeded with only `_default` gives an unlisted track the built-in budgets, and filing through the CLI
        # leaves the seeded file untouched (the floor-intent path is what adds track entries).
        seed = {"_default": {"timeout_s": 900, "quiet_after_s": 300}}
        self.tracks(seed)
        wb = self.file("tr", "seeded")
        self.assertEqual((wb["timeout_s"], wb["quiet_after_s"]), (900, 300))
        self.assertEqual(json.loads((self.yard / "tracks.json").read_text()), seed)


class TestRefusalCap(TurntableCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {k: self.env[k] for k in ("DEPOT_YARD", "DEPOT_CODEX", "DEPOT_HEARTBEAT_S")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.refusals = {}

    def cycle(self, n=1, mode=None):
        env = {"FAKE_MODE": mode} if mode else {}
        with mock.patch.dict(os.environ, env):
            for _ in range(n):
                with mock.patch.object(depot, "poll_log", lambda m: self.polled.append(m)):
                    depot.poll_cycle(threading.Event(), self.refusals)

    polled = None

    def run(self, result=None):
        self.polled = []
        return super().run(result)

    def approved_nope(self):
        d = self.new_item(adapter="nope")
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        return d

    def events(self, d):
        return [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines()]

    def blocked(self, d):
        return [e for e in self.events(d) if e["type"] == "blocked"]

    def attempts(self):
        return [m for m in self.polled if m.startswith("dispatching ")]

    def test_three_refusals_then_exactly_one_blocked_then_silence(self):
        d = self.approved_nope()
        self.cycle(2)
        self.assertEqual(self.blocked(d), [])
        self.cycle(1)
        b = self.blocked(d)
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0]["actor"], "turntable")
        self.assertTrue(b[0]["data"]["reason"].startswith("dispatch refused 3 times: "))
        self.assertIn("nope", b[0]["data"]["reason"])
        self.assertEqual(len(self.attempts()), 3)
        self.cycle(3)
        self.assertEqual(len(self.attempts()), 3)       # the 4th cycle on makes no attempt
        self.assertEqual(len(self.blocked(d)), 1)       # and no second blocked
        self.assertEqual([m for m in self.polled if "refused (" in m].__len__(), 3)

    def test_a_restart_forgives_the_count_but_the_block_stays_and_is_not_repeated(self):
        d = self.approved_nope()
        self.cycle(3)
        self.refusals.clear()                           # a daemon restart
        self.cycle(2)
        self.assertEqual(len(self.attempts()), 3)
        self.assertEqual(len(self.blocked(d)), 1)

    def test_a_new_approval_supersedes_the_block_and_dispatch_proceeds(self):
        d = self.approved_nope()
        self.cycle(3)
        wb = json.loads((d / "waybill.json").read_text())
        wb["adapter"] = "codex"                         # the repair
        (d / "waybill.json").write_text(json.dumps(wb))
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        self.cycle(1)
        types = [e["type"] for e in self.events(d)]
        self.assertEqual(types.count("dispatched"), 1)
        self.assertIn("verified", types)
        self.assertEqual(types.count("blocked"), 1)
        self.assertEqual(self.depot("status").stdout.split("stage=")[1].split()[0], "verified")

    def test_any_new_event_on_the_item_resets_the_count(self):
        d = self.approved_nope()
        self.cycle(2)
        self.assertEqual(self.depot("message", d.name, "a note").returncode, 0)
        self.cycle(2)                                    # 2 fresh refusals after the message: still under the cap
        self.assertEqual(self.blocked(d), [])
        self.cycle(1)
        self.assertEqual(len(self.blocked(d)), 1)

    def test_a_failed_run_is_an_outcome_never_a_refusal(self):
        d = self.new_item()
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        self.cycle(1, mode="fail")
        self.assertEqual(self.refusals, {})
        self.cycle(4, mode="fail")
        types = [e["type"] for e in self.events(d)]
        self.assertEqual(types.count("dispatched"), 1)  # never retried
        self.assertNotIn("blocked", types)


if __name__ == "__main__":
    unittest.main()
