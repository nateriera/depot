"""S10 tests: floor intake. The board-level `create` intent (landing, tracks, validation, shared id allocation,
never approving), its receipts on the projection, and the page's form draft and track-order preferences."""
import http.client
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import depot  # noqa: E402
import floor  # noqa: E402
from test_panel import Clock, PanelCase  # noqa: E402

PAGE = Path(floor.PAGE_DIR)
NODE = shutil.which("node") or "node"


def form(**over):
    base = {"title": "Tally the events", "track": "tally", "adapter": "codex",
            "body": "Build the sentinel.\n\n```py\nprint('hi')\n```\nUnicode: café — ok. No trailing newline.",
            "expected_files": ["sentinel.py", "test_sentinel.py"], "test_command": f"{sys.executable} -m unittest"}
    base.update(over)
    return base


class IntakeCase(PanelCase):
    def create(self, **over):
        status, body = floor.drop_intent(self.yard, {"kind": "create", "data": form(**over)}, self.clock)
        self.assertEqual(status, 202, body)
        return body["id"]

    def items(self):
        d = self.yard / "items"
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def tracks_text(self):
        p = self.yard / "tracks.json"
        return p.read_text() if p.exists() else None


class TestCreateLands(IntakeCase):
    def test_the_item_is_filed_with_the_waybill_brief_event_and_outcome(self):
        (self.yard / "tracks.json").write_text('{\n  "_default": {"timeout_s": 700, "quiet_after_s": 200}\n}\n')
        iid = self.create()
        self.assertEqual(self.process(), 1)
        self.assertEqual(self.items(), ["0001-tally-the-events"])
        d = self.yard / "items" / "0001-tally-the-events"
        wb = json.loads((d / "waybill.json").read_text())
        self.assertEqual({k: wb[k] for k in wb if k != "created"}, {
            "id": "0001", "slug": "tally-the-events", "title": "Tally the events", "track": "tally",
            "brief": "items/0001-tally-the-events/brief.md", "adapter": "codex", "model": None,
            "timeout_s": 700, "quiet_after_s": 200, "expected_files": ["sentinel.py", "test_sentinel.py"],
            "test_command": f"{sys.executable} -m unittest", "status": "queued"})
        e = self.events(d)
        self.assertEqual([(x["type"], x["actor"], x["data"]) for x in e],
                         [("item_created", "operator", {"via": "floor", "intent": iid, "track": "tally"})])
        self.assertEqual(self.glog(), e)
        out = self.outcome(iid)
        self.assertEqual((out["state"], out["item"], out["event"]), ("landed", "0001-tally-the-events",
                                                                    {"type": "item_created", "seq": 1}))
        self.assertNotIn("created", out)  # the commit hint is not part of the record

    def test_the_header_is_generated_from_the_fields_and_dispatch_reads_exactly_it(self):
        body = form()["body"]
        self.create()
        self.process()
        brief = (self.yard / "items" / "0001-tally-the-events" / "brief.md").read_text(encoding="utf-8")
        header = json.dumps({"expected_files": ["sentinel.py", "test_sentinel.py"],
                             "test_command": f"{sys.executable} -m unittest"})
        self.assertEqual(brief, f"```depot\n{header}\n```\n\n{body}")           # byte-for-byte: header, blank line, body
        self.assertEqual(depot.parse_header(brief), {"expected_files": ["sentinel.py", "test_sentinel.py"],
                                                     "test_command": f"{sys.executable} -m unittest"})
        self.assertEqual(depot.strip_header(brief), body)                       # the body reaches the agent verbatim
        wb = json.loads((self.yard / "items" / "0001-tally-the-events" / "waybill.json").read_text())
        self.assertEqual(depot.sync_waybill_from_brief(depot.Item(self.yard / "items" / "0001-tally-the-events"))
                         ["expected_files"], wb["expected_files"])              # waybill and brief say the same thing

    def test_the_waybill_has_the_same_shape_as_a_new_item(self):
        self.create()
        self.process()
        made = json.loads((self.yard / "items" / "0001-tally-the-events" / "waybill.json").read_text())
        self.assertEqual(self.depot("new-item", "--title", "T", "--slug", "other", "--track", "tally").returncode, 0)
        filed = json.loads((self.yard / "items" / "0002-other" / "waybill.json").read_text())
        self.assertEqual(set(made), set(filed))

    def test_optional_fields_land_when_given_and_are_absent_when_not(self):
        self.create(title="With extras", repo="https://example.com/r.git", branch="review/x", timeout_s=1200,
                    quiet_after_s=90.0, links=["Contacts/Example Contact.md", " "], context=["Projects/Example Project.md"])
        self.create(title="Plain")
        self.process()
        wb = json.loads((self.yard / "items" / "0001-with-extras" / "waybill.json").read_text())
        self.assertEqual((wb["repo"], wb["branch"], wb["timeout_s"], wb["quiet_after_s"], wb["links"], wb["context"]),
                         ("https://example.com/r.git", "review/x", 1200, 90, ["Contacts/Example Contact.md"],
                          ["Projects/Example Project.md"]))
        plain = json.loads((self.yard / "items" / "0002-plain" / "waybill.json").read_text())
        for k in ("repo", "branch", "links", "context"):
            self.assertNotIn(k, plain)

    def test_the_slug_comes_from_the_capture_slugifier(self):
        self.create(title="  Hello, World!!  —  a VERY long title that goes on and on past forty characters  ")
        self.process()
        self.assertEqual(self.items()[0], "0001-" + depot.slugify_capture(
            "Hello, World!! — a VERY long title that goes on and on past forty characters"))

    def test_creation_is_not_approval_and_nothing_dispatches_until_a_separate_approval(self):
        iid = self.create()
        self.poll_once()
        d = self.yard / "items" / "0001-tally-the-events"
        self.poll_once()
        self.assertEqual(self.types(d), ["item_created"])
        self.assertEqual(depot.Item(d).derived_status(), "queued")
        self.assertFalse((d / "runs").exists())
        self.assertEqual(self.outcome(iid)["state"], "landed")
        # a separate approval (the panel's, here) then dispatches it unmodified
        d_sentinel = self.drop("approve", d.name)
        self.poll_once()
        self.assertEqual(self.types(d)[:3], ["item_created", "brief_approved", "dispatched"])
        self.assertEqual(self.outcome(d_sentinel)["state"], "landed")
        verify = json.loads((next((d / "runs").iterdir()) / "verify.json").read_text())
        self.assertEqual(verify["result"], "pass")

    def test_a_represented_create_never_files_twice_and_a_lost_outcome_is_reconciled(self):
        iid = self.create()
        self.process()
        floor.outcome_path(self.yard, iid).unlink()
        receipts = floor.receipts_by_item(self.yard, floor.load_yard(self.yard))[None]
        self.assertEqual([r["state"] for r in receipts], ["reconciling"])
        self.assertEqual(self.process(), 1)
        out = self.outcome(iid)
        self.assertEqual((out["state"], out["item"], out["reconciled"]), ("landed", "0001-tally-the-events", True))
        self.assertEqual(self.items(), ["0001-tally-the-events"])
        self.assertEqual(len(self.glog()), 1)


class TestTracks(IntakeCase):
    def test_a_new_track_adds_exactly_one_entry_with_default_budgets(self):
        (self.yard / "tracks.json").write_text('{\n  "_default": {"timeout_s": 700, "quiet_after_s": 200}\n}\n')
        self.create(track="brand new")
        self.process()
        self.assertEqual(self.tracks_text(), '{\n  "_default": {"timeout_s": 700, "quiet_after_s": 200},\n'
                                             '  "brand new": {"timeout_s": 700, "quiet_after_s": 200}\n}\n')
        wb = json.loads((self.yard / "items" / "0001-tally-the-events" / "waybill.json").read_text())
        self.assertEqual((wb["track"], wb["timeout_s"], wb["quiet_after_s"]), ("brand new", 700, 200))

    def test_a_new_track_without_a_tracks_file_uses_the_builtin_defaults(self):
        self.create(track="fresh")
        self.process()
        self.assertEqual(json.loads(self.tracks_text()), {"fresh": {"timeout_s": 900, "quiet_after_s": 300}})

    def test_an_existing_track_changes_nothing_in_tracks_json(self):
        (self.yard / "tracks.json").write_text('{\n  "_default": {"timeout_s": 700, "quiet_after_s": 200}\n}\n')
        before = self.tracks_text()
        self.new_item("seed")                                      # carries track "tr", which has no entry of its own
        self.create(track="tr")
        self.create(track="tr", title="Second")
        self.process()
        self.assertEqual(self.tracks_text(), before)

    def test_a_track_with_its_own_entry_keeps_its_budgets(self):
        (self.yard / "tracks.json").write_text(
            '{\n  "_default": {"timeout_s": 700, "quiet_after_s": 200},\n  "slow": {"timeout_s": 3600}\n}\n')
        before = self.tracks_text()
        self.create(track="slow")
        self.process()
        wb = json.loads((self.yard / "items" / "0001-tally-the-events" / "waybill.json").read_text())
        self.assertEqual((wb["timeout_s"], wb["quiet_after_s"]), (3600, 200))
        self.assertEqual(self.tracks_text(), before)

    def test_explicit_budgets_override_the_track(self):
        self.create(track="t2", timeout_s=60, quiet_after_s=15)
        self.process()
        wb = json.loads((self.yard / "items" / "0001-tally-the-events" / "waybill.json").read_text())
        self.assertEqual((wb["timeout_s"], wb["quiet_after_s"]), (60, 15))

    def test_a_refused_create_never_begins_a_track(self):
        self.create(track="ghost", test_command="")
        self.process()
        self.assertIsNone(self.tracks_text())
        self.assertEqual(self.items(), [])

    def test_the_second_item_on_a_new_track_adds_nothing(self):
        self.create(track="fresh")
        self.process()
        first = self.tracks_text()
        self.create(track="fresh", title="Again")
        self.process()
        self.assertEqual(self.tracks_text(), first)


class TestValidation(IntakeCase):
    def refused(self, **over):
        iid = self.create(**over)
        self.assertEqual(self.process(), 0)
        out = self.outcome(iid)
        self.assertEqual(out["state"], "refused", out)
        self.assertTrue(out["reason"].startswith("nothing filed: "))
        return out["reason"]

    def test_each_failure_is_named_and_nothing_is_written(self):
        cases = [
            (dict(title="   "), "title is required"),
            (dict(track=""), "track is required"),
            (dict(track="_default"), "not a usable name"),
            (dict(track="bad/track"), "not a usable name"),
            (dict(track="x" * 61), "not a usable name"),
            (dict(adapter="gpt"), "adapter must be one of claude, codex"),
            (dict(adapter="gate"), "adapter must be one of"),
            (dict(adapter=""), "adapter must be one of"),
            (dict(body="  \n "), "brief body is required"),
            (dict(expected_files=[]), "expected files is required"),
            (dict(expected_files=["", "  "]), "expected files is required"),
            (dict(expected_files=["/etc/passwd"]), "is not a relative path"),
            (dict(expected_files=["a/../../b"]), "climbs out with .."),
            (dict(expected_files=["C:\\x"]), "is not a relative path"),
            (dict(test_command=""), "test command is required"),
            (dict(test_command="a\nb"), "single line"),
            (dict(test_command="python3 -c 'unterminated"), "cannot be split"),
            (dict(repo="https://example.com/r.git", branch="main"), "not main"),
            (dict(repo="https://example.com/r.git", branch="master"), "not master"),
            (dict(repo="https://example.com/r.git"), "repo and branch go together"),
            (dict(branch="review/x"), "repo and branch go together"),
            (dict(timeout_s=0), "timeout_s must be a positive number"),
            (dict(quiet_after_s=-5), "quiet_after_s must be a positive number"),
            (dict(links=["../Secrets.md"]), "links entry"),
            (dict(context=["/abs/note.md"]), "context entry"),
        ]
        for over, needle in cases:
            with self.subTest(over=str(over)[:50]):
                before = self.yard_snapshot()
                reason = self.refused(**over)
                self.assertIn(needle, reason)
                self.assertEqual(self.items(), [])
                self.assertIsNone(self.tracks_text())
                self.assertEqual(self.yard_snapshot(), before)          # no item, no event, no track

    def test_every_failure_is_named_together(self):
        reason = self.refused(title="", track="", adapter="gpt", body="", expected_files=[], test_command="")
        for needle in ("title is required", "track is required", "adapter must be one of", "brief body is required",
                       "expected files is required", "test command is required"):
            self.assertIn(needle, reason)
        self.assertIn("(6)", reason)
        reason = self.refused(expected_files=["../x", "/y"], branch="main", repo="r")
        self.assertIn("expected file '../x'", reason)
        self.assertIn("expected file '/y'", reason)
        self.assertIn("not main", reason)

    def yard_snapshot(self):
        return {str(p.relative_to(self.yard)): p.read_bytes() for p in sorted(self.yard.rglob("*"))
                if p.is_file() and "intents" not in p.parts and "poll.log" not in p.name}

    def test_gate_items_are_not_creatable(self):
        # the form's payload has no `kind`/`target`: the shape check refuses them at the door
        with self.assertRaises(floor.IntentInvalid):
            floor.validate_intent({"kind": "create", "data": {**form(), "kind": "gate"}})

    def test_an_item_on_a_create_intent_is_shape_invalid(self):
        with self.assertRaises(floor.IntentInvalid):
            floor.validate_intent({"kind": "create", "item": "0001-x", "data": form()})
        for bad in ({"title": 5}, {"expected_files": "a.py"}, {"expected_files": [1]}, {"timeout_s": True},
                    {"timeout_s": "10"}, {"body": "x" * (floor.MAX_BRIEF_BODY_CHARS + 1)}, {"surprise": 1}):
            with self.subTest(bad=str(bad)[:40]):
                with self.assertRaises(floor.IntentInvalid):
                    floor.validate_intent({"kind": "create", "data": form(**bad)})
        data = form()
        del data["test_command"]
        with self.assertRaises(floor.IntentInvalid):
            floor.validate_intent({"kind": "create", "data": data})


class TestSharedIdAllocation(IntakeCase):
    def test_a_capture_and_an_intake_in_one_cycle_get_distinct_sequential_ids(self):
        vault = self.tmp / "vault"
        (vault / "Depot" / "Inbox").mkdir(parents=True)
        (vault / "Depot" / "Inbox" / "idea.md").write_text("# A captured idea\nbody")
        self.new_item("existing")                                   # 0001
        self.create(title="From the floor")
        with mock.patch.dict(os.environ, {"DEPOT_VAULT": str(vault)}):
            self.poll_once()
        names = self.items()
        self.assertEqual(len(names), 3)
        self.assertEqual([n.split("-")[0] for n in names], ["0001", "0002", "0003"])
        self.assertEqual(sorted(n.split("-", 1)[1] for n in names[1:]), ["a-captured-idea", "from-the-floor"])
        self.assertEqual(len({n.split("-")[0] for n in names}), 3)

    def test_two_creates_in_one_cycle_are_sequential(self):
        self.create(title="One")
        self.create(title="Two")
        self.process()
        self.assertEqual(self.items(), ["0001-one", "0002-two"])


class TestRecordsCommit(IntakeCase):
    def test_one_records_commit_per_landed_create_including_a_new_track(self):
        def git(*a):
            return subprocess.run(["git", *a], cwd=self.yard, capture_output=True, text=True, check=True).stdout
        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (self.yard / ".gitignore").write_text("items/*/work/\nlog/poll.lock\nlog/poll.log\nlog/intents/\n")
        (self.yard / "log" / "events.jsonl").write_text("")
        git("add", "-A")
        git("commit", "-q", "-m", "seed")
        self.create(title="First", track="newtrack")
        self.create(title="Second", track="newtrack")
        with mock.patch.object(depot, "poll_log", lambda m: self.logs.append(m)):
            self.assertEqual(depot.process_intents(True), 2)
        log = git("log", "--format=%s").splitlines()
        self.assertEqual(log[:2], ["depot: 0002-second created (floor)", "depot: 0001-first created (floor)"])
        self.assertEqual(git("status", "--porcelain").strip(), "")
        files = git("show", "--name-only", "--format=", "HEAD~1").split()
        self.assertIn("tracks.json", files)
        self.assertIn("items/0001-first/brief.md", files)
        self.assertNotIn("tracks.json", git("show", "--name-only", "--format=", "HEAD").split())   # no second entry


class TestBoardReceiptsAndRoute(IntakeCase):
    def test_a_landed_create_reads_filed_on_the_board_receipts(self):
        self.create()
        board = floor.project(self.yard, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
        self.assertEqual([(r["kind"], r["state"], r["summary"]) for r in board["board_receipts"]],
                         [("create", "pending", "Tally the events")])
        self.process()
        r = floor.project(self.yard, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))["board_receipts"][0]
        self.assertEqual((r["state"], r["filed"], r["event"]["type"]), ("landed", "0001-tally-the-events", "item_created"))
        card = next(c for c in floor.project(self.yard, __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc))["cards"])
        self.assertEqual((card["stage"], card["receipts"]), ("Queued", []))      # the card carries no create receipt

    def test_a_refused_create_reads_its_reasons_and_no_card_appears(self):
        self.create(test_command="")
        self.process()
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        board = floor.project(self.yard, now)
        r = board["board_receipts"][0]
        self.assertEqual((r["state"], r["filed"]), ("refused", None))
        self.assertIn("test command is required", r["reason"])
        self.assertEqual(board["cards"], [])

    def test_post_intents_accepts_a_create_and_refuses_a_malformed_one(self):
        srv = floor.make_server(self.yard, 0, Clock())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        port = srv.server_address[1]

        def post(body):
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("POST", "/intents", body=json.dumps(body), headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            out = json.loads(resp.read())
            conn.close()
            return resp.status, out
        status, out = post({"kind": "create", "data": form()})
        self.assertEqual(status, 202)
        saved = json.loads(floor.intent_path(self.yard, out["id"]).read_text())
        self.assertEqual((saved["kind"], saved["item"], saved["data"]["title"]), ("create", None, "Tally the events"))
        self.assertEqual(post({"kind": "create", "item": "0001-x", "data": form()})[0], 400)
        self.assertEqual(post({"kind": "create", "data": {"title": "only"}})[0], 400)
        self.assertEqual(len(list((self.yard / "log" / "intents").glob("*.json"))), 1)
        again = post({"id": out["id"], "kind": "create", "data": form()})
        self.assertEqual((again[0], again[1].get("duplicate")), (200, True))


def node(script):
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, cwd=PAGE)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout)


class TestPagePreferences(unittest.TestCase):
    """The pure helpers in floor.js, run under node with a stand-in store (no DOM)."""

    def run_js(self, body):
        return node(f"const f = require('./floor.js'); {body}")

    def test_default_order_is_the_projection_order(self):
        self.assertEqual(self.run_js("console.log(JSON.stringify(f.orderTracks(['b','a','c'], null)))"), ["b", "a", "c"])

    def test_a_saved_order_wins_new_tracks_append_and_stale_names_are_ignored(self):
        out = self.run_js("console.log(JSON.stringify(f.orderTracks(['a','b','c','d'], ['c','gone','a'])))")
        self.assertEqual(out, ["c", "a", "b", "d"])        # saved first (stale 'gone' dropped), then projection order

    def test_moving_a_track_swaps_it_with_its_neighbour_and_stops_at_the_ends(self):
        self.assertEqual(self.run_js("console.log(JSON.stringify(f.moveTrack(['a','b','c'], 'b', -1)))"), ["b", "a", "c"])
        self.assertEqual(self.run_js("console.log(JSON.stringify(f.moveTrack(['a','b','c'], 'b', 1)))"), ["a", "c", "b"])
        self.assertEqual(self.run_js("console.log(JSON.stringify(f.moveTrack(['a','b','c'], 'a', -1)))"), ["a", "b", "c"])
        self.assertEqual(self.run_js("console.log(JSON.stringify(f.moveTrack(['a','b','c'], 'c', 1)))"), ["a", "b", "c"])
        self.assertEqual(self.run_js("console.log(JSON.stringify(f.moveTrack(['a','b'], 'zzz', 1)))"), ["a", "b"])

    def test_order_persists_through_a_store_and_reloads(self):
        out = self.run_js("""
          const mem = {}; const store = {get: k => (k in mem ? mem[k] : null), set: (k, v) => { mem[k] = v; }};
          const prefs = f.trackOrderPrefs(store);
          prefs.move(['a','b','c'], 'c', -1);
          const reloaded = f.trackOrderPrefs(store);                  // a new page load reads the same store
          console.log(JSON.stringify({saved: mem['depot.trackorder'], order: reloaded.order(['a','b','c','d'])}));""")
        self.assertEqual(out, {"saved": '["a","c","b"]', "order": ["a", "c", "b", "d"]})

    def test_blocked_storage_falls_back_to_default_and_the_controls_still_work_for_the_session(self):
        out = self.run_js("""
          const broken = {get() { throw new Error('blocked'); }, set() { throw new Error('blocked'); }};
          const prefs = f.trackOrderPrefs(broken);
          const first = prefs.order(['a','b','c']);
          prefs.move(['a','b','c'], 'c', -1);
          console.log(JSON.stringify({first, after: prefs.order(['a','b','c'])}));""")
        self.assertEqual(out, {"first": ["a", "b", "c"], "after": ["a", "c", "b"]})

    def test_a_corrupt_saved_order_is_ignored(self):
        out = self.run_js("""
          const store = {get: () => '{not json', set() {}};
          console.log(JSON.stringify(f.trackOrderPrefs(store).order(['a','b'])));""")
        self.assertEqual(out, ["a", "b"])

    def test_the_form_draft_persists_and_clears_only_on_a_landed_create(self):
        out = self.run_js("""
          const mem = {}; const store = {get: k => (k in mem ? mem[k] : null), set: (k, v) => { mem[k] = v; }, del: k => { delete mem[k]; }};
          const d = f.intakeDraft(store);
          d.save({title: 'half typed', track: 'x'});
          const survived = f.intakeDraft(store).load();                 // an accidental card click / refresh
          d.submitted('intent-1');
          const resolvingRefused = d.resolve([{id: 'intent-1', state: 'refused', reason: 'nope'}]);
          const stillThere = d.load();
          const resolvingPending = d.resolve([{id: 'intent-1', state: 'pending'}]);
          const landed = d.resolve([{id: 'intent-1', state: 'landed', filed: '0014-x'}]);
          console.log(JSON.stringify({survived, resolvingRefused, stillThere, resolvingPending, landed, after: d.load(),
            pending: f.intakeDraft(store).pendingId()}));""")
        self.assertEqual(out["survived"], {"title": "half typed", "track": "x"})
        self.assertEqual(out["resolvingRefused"], {"state": "refused", "reason": "nope"})
        self.assertEqual(out["stillThere"], {"title": "half typed", "track": "x"})       # a refusal keeps the draft
        self.assertEqual(out["resolvingPending"], None)
        self.assertEqual(out["landed"], {"state": "landed", "filed": "0014-x"})
        self.assertIsNone(out["after"])                                                  # cleared on landed
        self.assertIsNone(out["pending"])

    def test_the_form_draft_degrades_without_storage(self):
        out = self.run_js("""
          const broken = {get() { throw new Error('blocked'); }, set() { throw new Error('blocked'); }, del() { throw new Error('blocked'); }};
          const d = f.intakeDraft(broken);
          d.save({title: 'in memory'});
          console.log(JSON.stringify(d.load()));""")
        self.assertEqual(out, {"title": "in memory"})


class TestPageStatics(unittest.TestCase):
    def test_the_page_has_the_form_the_receipts_and_the_arrange_controls(self):
        js, html = (PAGE / "floor.js").read_text(), (PAGE / "index.html").read_text()
        self.assertIn("New work", js)
        self.assertIn("File as a new item in Queued — not approved, nothing runs until approval.", js)
        self.assertIn("board_receipts", js)
        self.assertIn("depot.trackorder", js)
        self.assertIn('"Move track " + word', js)
        self.assertIn('"earlier"', js)
        self.assertIn("display order in this browser only", js)
        self.assertEqual(js.count("fetch("), 2)                         # still one write call site: POST /intents
        self.assertIn('"/intents"', js)
        self.assertNotIn("innerHTML", js)
        self.assertIn("intake", html)                                    # the board-level region exists in the page

    def test_the_server_side_never_stores_track_order(self):
        src = (Path(floor.__file__)).read_text() + (Path(depot.__file__)).read_text()
        self.assertNotIn("trackorder", src)


if __name__ == "__main__":
    unittest.main()
