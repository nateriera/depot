"""S9 tests: the vault bridge. Capture filing, the approval gate for captured items, projection notes, dispatch
context, the context/ verification exclusion, and the bridge-disabled equivalence."""
import hashlib
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import depot  # noqa: E402
import floor  # noqa: E402
from test_depot import BRIEF, TurntableCase  # noqa: E402

OPS = Path(__file__).resolve().parent.parent / "ops"


class VaultCase(TurntableCase):
    def setUp(self):
        super().setUp()
        self.vault = self.tmp / "vault"
        self.inbox = self.vault / "Depot" / "Inbox"
        self.inbox.mkdir(parents=True)
        self.env["DEPOT_VAULT"] = str(self.vault)
        patcher = mock.patch.dict(os.environ, {k: self.env[k] for k in
                                               ("DEPOT_YARD", "DEPOT_CODEX", "DEPOT_HEARTBEAT_S", "DEPOT_VAULT")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logs = []

    def log(self, m):
        self.logs.append(m)

    def capture(self, name="idea.md", text="# Look into the Entra renewal\nA colleague asked about it on Friday.\n"):
        (self.inbox / name).write_bytes(text.encode())
        return text

    def scan(self):
        return depot.vault_capture_scan(False, self.log)

    def events(self, d):
        return [json.loads(l) for l in (d / "events.jsonl").read_text().splitlines()]

    def glog(self):
        return [json.loads(l) for l in (self.yard / "log" / "events.jsonl").read_text().splitlines()]

    def ledger(self):
        return json.loads((self.yard / "log" / "vault-captures.json").read_text())

    def shape(self, d, adapter=True, expected=True, test=True):
        if adapter:
            wb = json.loads((d / "waybill.json").read_text())
            wb["adapter"] = "codex"
            (d / "waybill.json").write_text(json.dumps(wb))
        hdr = {"expected_files": ["sentinel.py", "test_sentinel.py"] if expected else [],
               "test_command": f"{sys.executable} -m unittest" if test else ""}
        text = (d / "brief.md").read_text()
        (d / "brief.md").write_text("```depot\n" + json.dumps(hdr) + "\n```\n\n" + depot.strip_header(text))

    def poll_once(self, **env):
        with mock.patch.dict(os.environ, env), mock.patch.object(depot, "poll_log", lambda m: self.logs.append(m)):
            depot.poll_cycle(threading.Event(), {})


class TestCaptureFiling(VaultCase):
    def test_a_capture_becomes_a_queued_item_with_draft_defaults(self):
        (self.yard / "tracks.json").write_text(json.dumps({"inbox": {"timeout_s": 600}, "_default": {"quiet_after_s": 111}}))
        text = self.capture()
        filed = self.scan()
        self.assertEqual(filed, [("0001-look-into-the-entra-renewal", "idea.md")])
        d = self.yard / "items" / filed[0][0]
        wb = json.loads((d / "waybill.json").read_text())
        self.assertNotIn("adapter", wb)                       # nothing can dispatch until shaped
        self.assertEqual({k: wb[k] for k in ("id", "slug", "title", "track", "model", "links", "context", "status")},
                         {"id": "0001", "slug": "look-into-the-entra-renewal", "title": "Look into the Entra renewal",
                          "track": "inbox", "model": None, "links": [], "context": [], "status": "queued"})
        self.assertEqual((wb["timeout_s"], wb["quiet_after_s"]), (600, 111))   # the existing budget resolution
        self.assertEqual(wb["brief"], "items/0001-look-into-the-entra-renewal/brief.md")
        self.assertIn("created", wb)
        brief = (d / "brief.md").read_text()
        self.assertEqual(depot.parse_header(brief), {"expected_files": [], "test_command": ""})
        self.assertTrue(brief.endswith(text))                 # verbatim, complete, unedited
        self.assertEqual(depot.strip_header(brief), text)
        e = self.events(d)
        self.assertEqual([(x["type"], x["actor"], x["data"]) for x in e], [
            ("item_created", "turntable", {"title": "Look into the Entra renewal", "track": "inbox",
                                           "via": "vault-capture", "source": "idea.md"})])
        self.assertEqual(self.glog(), e)                      # both logs
        self.assertEqual(depot.Item(d).derived_status(), "queued")

    def test_budgets_fall_back_to_the_builtin_defaults(self):
        self.capture()
        d = self.yard / "items" / self.scan()[0][0]
        wb = json.loads((d / "waybill.json").read_text())
        self.assertEqual((wb["timeout_s"], wb["quiet_after_s"]), (900, 300))

    def test_title_and_slug_rules(self):
        self.capture("a.md", "###   A very long heading that keeps going well past the sensible slug length for a folder\nbody")
        self.capture("b.md", "\n\n   \n!!! ???\nsecond")
        self.capture("c.md", "#\n##\nreal first line")
        names = [n for n, _ in self.scan()]
        self.assertEqual(len(names), 3)
        slugs = {n.split("-", 1)[1] for n in names}
        self.assertTrue(all(len(s) <= 40 and s == s.strip("-") for s in slugs))
        self.assertIn("real-first-line", slugs)
        self.assertEqual(json.loads((self.yard / "items" / names[0] / "waybill.json").read_text())["title"],
                         "A very long heading that keeps going well past the sensible slug length for a folder")
        self.assertEqual(json.loads((self.yard / "items" / names[1] / "waybill.json").read_text())["title"], "!!! ???")
        self.assertIn("capture", slugs)                      # nothing sluggable: a stable fallback

    def test_ids_continue_the_yard_sequence(self):
        self.new_item("existing")
        self.capture()
        self.assertEqual(self.scan()[0][0].split("-")[0], "0002")

    def test_only_top_level_markdown_files_that_are_not_dotfiles(self):
        self.capture("real.md", "real")
        self.capture(".hidden.md", "hidden")
        (self.inbox / "notes.txt").write_text("text")
        (self.inbox / "sub").mkdir()
        (self.inbox / "sub" / "deep.md").write_text("deep")
        self.assertEqual([f for _, f in self.scan()], ["real.md"])
        self.assertTrue((self.inbox / ".hidden.md").exists() and (self.inbox / "sub" / "deep.md").exists())

    def test_receipt_stamps_and_moves_the_capture(self):
        text = self.capture()
        name = self.scan()[0][0]
        self.assertFalse((self.inbox / "idea.md").exists())
        done = (self.inbox / "Processed" / "idea.md").read_text()
        self.assertTrue(done.startswith(f"---\ndepot_item: {name}\ndepot_filed: "))
        head, _, rest = done.partition("\n---\n")
        self.assertRegex(head, r"depot_filed: 20\d\d-\d\d-\d\dT[\d:.]+Z$")
        self.assertEqual(rest, text)

    def test_an_existing_frontmatter_block_is_merged_not_doubled(self):
        self.capture("fm.md", "---\ntags: [idea]\n---\nBody line\n")
        name = self.scan()[0][0]
        done = (self.inbox / "Processed" / "fm.md").read_text()
        self.assertEqual(done.count("---\n"), 2)
        self.assertIn(f"depot_item: {name}", done)
        self.assertIn("tags: [idea]", done)

    def test_the_same_capture_never_files_twice(self):
        self.capture()
        self.assertEqual(len(self.scan()), 1)
        self.assertEqual(self.scan(), [])                                    # re-scan: nothing
        self.capture()                                                       # a re-sync drops the identical file back
        self.assertEqual(self.scan(), [])
        self.assertEqual(len(list((self.yard / "items").iterdir())), 1)
        self.assertEqual(sorted(p.name for p in (self.inbox / "Processed").iterdir()), ["idea-2.md", "idea.md"])
        self.assertEqual(list(self.ledger().values()), ["0001-look-into-the-entra-renewal"])
        key = next(iter(self.ledger()))
        self.assertEqual(key.split(":")[0], "idea.md")
        self.assertEqual(key.split(":")[1], hashlib.sha256(b"# Look into the Entra renewal\nA colleague asked about it on Friday.\n").hexdigest())

    def test_editing_a_capture_after_filing_does_not_refile_it(self):
        self.capture()
        self.scan()
        processed = self.inbox / "Processed" / "idea.md"
        processed.write_text(processed.read_text() + "\nA later thought.\n")   # operator keeps writing in the filed note
        self.assertEqual(self.scan(), [])
        self.assertEqual(len(list((self.yard / "items").iterdir())), 1)

    def test_a_malformed_capture_is_skipped_not_filed_not_ledgered_and_fixable(self):
        for body in ("", "\n   \n\t\n", "###\n#\n"):
            with self.subTest(body=repr(body)):
                self.capture("bad.md", body)
                self.assertEqual(self.scan(), [])
                self.assertFalse((self.yard / "log" / "vault-captures.json").exists())
                self.assertTrue((self.inbox / "bad.md").exists())
        self.assertTrue(any("no text" in m for m in self.logs))
        (self.inbox / "bad.md").write_bytes(b"\xff\xfe not utf-8")
        self.assertEqual(self.scan(), [])
        self.assertTrue(any("UTF-8" in m for m in self.logs))
        self.capture("bad.md", "now it has words")                             # fixing the note files it next cycle
        self.assertEqual(self.scan(), [("0001-now-it-has-words", "bad.md")])

    def test_a_failed_receipt_leaves_the_file_and_never_files_twice(self):
        self.capture()
        with mock.patch.object(depot, "move_capture_to_processed", side_effect=OSError("read-only")):
            filed = self.scan()
        self.assertEqual(len(filed), 1)
        self.assertTrue((self.inbox / "idea.md").exists())
        self.assertTrue(any("could not stamp and move" in m for m in self.logs))
        self.assertEqual(self.scan(), [])                                      # next cycle repairs the receipt only
        self.assertFalse((self.inbox / "idea.md").exists())
        self.assertTrue((self.inbox / "Processed" / "idea.md").exists())
        self.assertEqual(len(list((self.yard / "items").iterdir())), 1)

    def test_filing_is_committed_as_the_daemon_does(self):
        def git(*a):
            return subprocess.run(["git", *a], cwd=self.yard, capture_output=True, text=True, check=True).stdout
        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        (self.yard / ".gitignore").write_text("items/*/work/\nlog/poll.lock\nlog/poll.log\nlog/intents/\n")
        (self.yard / "log" / "events.jsonl").write_text("")
        git("add", "-A")
        git("commit", "-q", "-m", "seed")
        self.capture()
        name = depot.vault_capture_scan(True, self.log)[0][0]
        self.assertEqual(git("log", "-1", "--format=%s").strip(), f"depot: {name} created (vault capture)")
        files = git("show", "--name-only", "--format=", "HEAD").split()
        self.assertIn("log/vault-captures.json", files)
        self.assertIn(f"items/{name}/brief.md", files)
        self.assertEqual(git("status", "--porcelain").strip(), "")


class TestApprovalGate(VaultCase):
    def captured(self):
        self.capture()
        return self.yard / "items" / self.scan()[0][0]

    def test_approve_names_every_missing_piece_and_appends_nothing(self):
        d = self.captured()
        before = self.events(d)
        r = self.depot("approve", d.name)
        self.assertEqual(r.returncode, 2)
        for piece in ('no "adapter" key', "expected_files is empty", "test_command is empty"):
            self.assertIn(piece, r.stderr)
        self.assertEqual(self.events(d), before)
        self.assertEqual(self.glog(), before)

    def refusal(self, name, **shape):
        self.capture(name, f"# {name}\n")
        d = self.yard / "items" / next(n for n, f in self.scan() if f == name)
        self.shape(d, **shape)
        r = self.depot("approve", d.name)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(len(self.events(d)), 1)
        return r.stderr

    def test_each_piece_is_named_only_while_it_is_missing(self):
        err = self.refusal("a.md", adapter=True, expected=False, test=False)
        self.assertNotIn("adapter", err)
        self.assertIn("expected_files is empty", err)
        self.assertIn("test_command is empty", err)
        err = self.refusal("b.md", adapter=False, expected=True, test=False)
        self.assertIn('no "adapter" key', err)
        self.assertNotIn("expected_files", err)
        self.assertIn("test_command is empty", err)
        err = self.refusal("c.md", adapter=False, expected=True, test=True)
        self.assertIn('no "adapter" key', err)
        self.assertNotIn("expected_files", err)
        self.assertNotIn("test_command", err)
        err = self.refusal("d.md", adapter=True, expected=True, test=False)
        self.assertEqual(err.count("empty"), 1)

    def test_once_shaped_approval_and_dispatch_work_unmodified(self):
        d = self.captured()
        self.shape(d)
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        self.poll_once()
        self.assertEqual(self.types(d), ["item_created", "brief_approved", "dispatched", "run_finished", "claimed",
                                         "verified", "committed"])
        verify = json.loads((next((d / "runs").iterdir()) / "verify.json").read_text())
        self.assertEqual(verify["result"], "pass")

    def test_the_panel_approve_intent_is_refused_too(self):
        d = self.captured()
        status, body = floor.drop_intent(self.yard, {"kind": "approve", "item": d.name, "data": {}})
        self.assertEqual(status, 202)
        with mock.patch.object(depot, "poll_log", lambda m: None):
            depot.process_intents(False)
        out = floor.read_outcome(self.yard, body["id"])
        self.assertEqual(out["state"], "refused")
        self.assertIn("still needs shaping", out["reason"])
        self.assertEqual(self.types(d), ["item_created"])

    def test_an_unshaped_capture_approved_around_the_gate_is_still_not_dispatched(self):
        d = self.captured()
        depot.Item(d).emit("brief_approved", "reviewer", {"via": "connector"})  # a hand/connector-appended approval
        r = self.depot("dispatch", d.name)
        self.assertEqual(r.returncode, 2)
        self.assertIn("still needs shaping", r.stderr)
        self.assertNotIn("dispatched", self.types(d))
        self.assertFalse((d / "runs").exists())

    def test_non_capture_items_are_not_gated(self):
        d = self.new_item(adapter="codex")
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        wb = json.loads((d / "waybill.json").read_text())
        del wb["adapter"]
        (d / "waybill.json").write_text(json.dumps(wb))
        self.assertEqual(depot.capture_gate_missing(depot.Item(d)), [])


class TestProjection(VaultCase):
    def note(self, name):
        return (self.vault / "Depot" / "Items" / f"{name}.md").read_text(encoding="utf-8")

    def project(self):
        return depot.vault_project_all(self.log)

    def test_note_renders_frontmatter_header_headline_brief_and_wikilinks(self):
        d = self.new_item("survey", links=["Contacts/Example Contact.md", "Projects/Example Project", "bad|link.md", "../escape.md"])
        self.assertEqual(self.project(), [d.name])
        wb = json.loads((d / "waybill.json").read_text())
        created = wb["created"]
        self.assertEqual(self.note(d.name), f"""---
depot_id: "0001"
track: tr
adapter: codex
stage: Queued
truth: recorded
created: {created}
updated: {self.events(d)[-1]['ts']}
tags: [depot/item]
---
<!-- Generated by Depot — the record lives in the yard (items/0001-survey). Annotate the card, not this note. -->

# 0001 — T

card created

Brief: items/0001-survey/brief.md (yard)

## Links
- [[Contacts/Example Contact]]
- [[Projects/Example Project]]
""")

    def test_no_links_section_and_no_adapter_line_when_there_are_none(self):
        self.capture()
        name = self.scan()[0][0]
        self.project()
        text = self.note(name)
        self.assertNotIn("## Links", text)
        self.assertNotIn("adapter:", text)
        self.assertIn("track: inbox", text)

    def test_stage_and_truth_come_from_the_floors_derivation(self):
        d = self.new_item("built")
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        self.project()
        card = floor.card_for(self.yard, d.name, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
        text = self.note(d.name)
        self.assertIn(f"stage: {card['stage']}", text)
        self.assertIn(f"truth: {card['truth']}", text)
        self.assertEqual((card["stage"], card["truth"]), ("Review", "recorded"))   # newest fact: committed
        self.assertIn(card["trail"][-1]["text"], text)

    def test_regeneration_rewrites_only_what_changed_and_only_its_own_note(self):
        a, b = self.new_item("one"), self.new_item("two")
        self.assertEqual(sorted(self.project()), [a.name, b.name])
        self.assertEqual(self.project(), [])                                  # unchanged: nothing is rewritten
        before_b = self.note(b.name)
        mtime_b = (self.vault / "Depot" / "Items" / f"{b.name}.md").stat().st_mtime_ns
        self.assertEqual(self.depot("approve", a.name).returncode, 0)
        self.assertEqual(self.project(), [a.name])
        self.assertIn("stage: Briefed", self.note(a.name))
        self.assertEqual(self.note(b.name), before_b)
        self.assertEqual((self.vault / "Depot" / "Items" / f"{b.name}.md").stat().st_mtime_ns, mtime_b)

    def test_a_message_is_never_the_headline_it_matches_the_card(self):
        d = self.new_item("msg")
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        depot.Item(d).emit("message", "operator", {"text": "a note from operator"})
        self.project()
        card = floor.card_for(self.yard, d.name, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
        self.assertEqual(self.events(d)[-1]["type"], "message")
        self.assertEqual(card["last_event"]["type"], "committed")
        text = self.note(d.name)
        self.assertIn(f"\n{card['last_event']['text']}\n", text)         # the card's headline, word for word
        self.assertNotIn("a note from operator", text)

    def test_a_heartbeat_does_not_rewrite_the_note(self):
        d = self.new_item("hb")
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        self.project()
        depot.Item(d).emit("dispatched", "turntable", {"adapter": "codex", "run_id": "R"})
        self.project()
        before = self.note(d.name)
        depot.Item(d).emit("heartbeat", "turntable", {"since_last_adapter_event_s": 1, "elapsed_s": 30})
        self.assertEqual(self.project(), [])
        self.assertEqual(self.note(d.name), before)

    def test_done_items_notes_persist(self):
        d = self.new_item("done")
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        depot.Item(d).emit("reviewed", "reviewer", {"result": "pass"})
        self.project()
        self.assertIn("stage: Done", self.note(d.name))
        self.new_item("later")
        self.project()
        self.assertTrue((self.vault / "Depot" / "Items" / f"{d.name}.md").exists())

    def test_a_hand_edit_is_overwritten_by_that_items_regeneration_only(self):
        a, b = self.new_item("one"), self.new_item("two")
        self.project()
        pa, pb = (self.vault / "Depot" / "Items" / f"{n}.md" for n in (a.name, b.name))
        good_b = pb.read_text()
        pa.write_text("hand edited")
        self.assertEqual(self.project(), [a.name])                 # a's note is regenerated; b's is not touched
        self.assertIn("Generated by Depot", pa.read_text())
        self.assertEqual(pb.read_text(), good_b)
        self.assertEqual(sorted(p.name for p in pa.parent.iterdir()), [f"{a.name}.md", f"{b.name}.md"])

    def test_a_broken_item_does_not_stop_the_others(self):
        good = self.new_item("good")
        bad = self.new_item("bad")
        (bad / "events.jsonl").write_text('{"ts": "2026-11-01T00:00:00.000Z", "item": "x", "type": "dispatched", "actor": "t", "data": {}}\n')
        written = self.project()
        self.assertIn(good.name, written)
        self.assertTrue(any(bad.name in m and "failed" in m for m in self.logs))


class TestContext(VaultCase):
    def note(self, rel="Contacts/Example Contact.md", text="Example Contact leads the Example Project survey; her phone is 555-0100.\n"):
        p = self.vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return hashlib.sha256(text.encode()).hexdigest()

    def approved(self, context):
        d = self.new_item(context=context)
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        return d

    def test_context_is_copied_recorded_and_announced(self):
        sha = self.note()
        sha2 = self.note("Projects/Example Project.md", "Example Project governance project\n")
        d = self.approved(["Contacts/Example Contact.md", "Projects/Example Project.md"])
        r = self.depot("dispatch", d.name)
        self.assertEqual(r.returncode, 0, r.stderr)
        run = next((d / "runs").iterdir())
        self.assertEqual((d / "work" / "context" / "Example Contact.md").read_text(), "Example Contact leads the Example Project survey; her phone is 555-0100.\n")
        manifest = json.loads((run / "manifest.json").read_text())
        self.assertEqual(manifest["context"], [{"path": "Contacts/Example Contact.md", "sha256": sha},
                                               {"path": "Projects/Example Project.md", "sha256": sha2}])    # waybill order
        sent = (run / "brief.dispatched.md").read_text()
        self.assertIn("## Context from vault", sent)
        self.assertIn("- Contacts/Example Contact.md → context/Example Contact.md", sent)
        self.assertIn("- Projects/Example Project.md → context/Example Project.md", sent)

    def test_runs_without_context_record_an_empty_list_and_an_unchanged_brief(self):
        d = self.approved([])
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        run = next((d / "runs").iterdir())
        self.assertEqual(json.loads((run / "manifest.json").read_text())["context"], [])
        self.assertEqual((run / "brief.dispatched.md").read_text(), depot.strip_header(BRIEF.format(py=sys.executable)))
        self.assertFalse((d / "work" / "context").exists())

    def test_a_missing_note_refuses_the_dispatch_by_name(self):
        self.note()
        d = self.approved(["Contacts/Example Contact.md", "People/Nobody.md"])
        r = self.depot("dispatch", d.name)
        self.assertEqual(r.returncode, 2)
        self.assertIn("People/Nobody.md", r.stderr)
        self.assertNotIn("dispatched", self.types(d))
        self.assertFalse((d / "runs").exists())
        self.assertFalse((d / "work" / "context").exists())               # nothing half-delivered

    def test_a_missing_note_is_a_counted_refusal_in_the_poll_cycle(self):
        d = self.approved(["People/Nobody.md"])
        refusals = {}
        for _ in range(depot.REFUSAL_CAP):
            with mock.patch.object(depot, "poll_log", lambda m: self.logs.append(m)):
                depot.poll_cycle(threading.Event(), refusals)
        self.assertEqual(self.types(d)[-1], "blocked")
        self.assertIn("People/Nobody.md", self.events(d)[-1]["data"]["reason"])

    def test_unsafe_and_colliding_entries_are_refused(self):
        self.note()
        self.note("Other/Example Contact.md", "another\n")
        (self.tmp / "outside.md").write_text("secret")
        for entries, needle in ((["../outside.md"], "outside the vault"), (["/etc/passwd"], "outside the vault"),
                                (["Contacts/Example Contact.md", "Other/Example Contact.md"], "share the name"),
                                ([""], "bad context entry")):
            with self.subTest(entries=entries):
                d = self.approved(entries)
                r = self.depot("dispatch", d.name)
                self.assertEqual(r.returncode, 2)
                self.assertIn(needle, r.stderr)

    def test_context_without_a_vault_is_refused_not_skipped(self):
        d = self.approved(["Contacts/Example Contact.md"])
        env = {k: v for k, v in self.env.items() if k != "DEPOT_VAULT"}
        r = subprocess.run([sys.executable, depot.__file__, "dispatch", d.name], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("DEPOT_VAULT", r.stderr)

    def test_verification_excludes_context_and_still_passes_exactly(self):
        self.note()
        d = self.approved(["Contacts/Example Contact.md"])
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        run = next((d / "runs").iterdir())
        verify = json.loads((run / "verify.json").read_text())
        self.assertTrue((d / "work" / "context" / "Example Contact.md").exists())   # present in the work dir
        self.assertEqual((verify["result"], verify["discrepancies"], verify["changed_files"]),
                         ("pass", [], ["sentinel.py", "test_sentinel.py"]))
        self.assertNotIn("context", (run / "diff.patch").read_text())

    def test_the_exclusion_is_confined_to_the_top_level_context_directory(self):
        work = self.tmp / "w"
        work.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=work, check=True)
        for rel in ("context/a.md", "context/deep/b.md", "src/context/c.py", "contextual.py", "context.txt", "d.py"):
            p = work / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        self.assertEqual(depot.changed_files(work), {"src/context/c.py", "contextual.py", "context.txt", "d.py"})

    def test_stale_context_does_not_linger_into_a_run_that_lists_none(self):
        self.note()
        d = self.approved(["Contacts/Example Contact.md"])
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        self.assertTrue((d / "work" / "context").exists())
        wb = json.loads((d / "waybill.json").read_text())
        wb["context"] = []
        (d / "waybill.json").write_text(json.dumps(wb))
        import time
        time.sleep(1.1)  # run ids have one-second resolution
        depot.Item(d).emit("reopened", "operator", {"text": "again"})
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        self.depot("dispatch", d.name)                          # (verification may differ: the files already exist)
        self.assertFalse((d / "work" / "context").exists())
        last = sorted((d / "runs").iterdir())[-1]
        self.assertEqual(json.loads((last / "manifest.json").read_text())["context"], [])


class TestBridgeDisabled(TurntableCase):
    def snapshot(self):
        return {str(p.relative_to(self.tmp)): p.read_bytes() for p in sorted(self.tmp.rglob("*"))
                if p.is_file() and ".git" not in p.parts and "work" not in p.parts and "runs" not in p.parts}

    def test_without_depot_vault_nothing_in_the_vault_is_touched_and_the_cycle_is_unchanged(self):
        vault = self.tmp / "vault"
        (vault / "Depot" / "Inbox").mkdir(parents=True)
        (vault / "Depot" / "Inbox" / "idea.md").write_text("an idea")
        env = {k: v for k, v in self.env.items() if k != "DEPOT_VAULT"}
        d = self.new_item()
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        vault_before = {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}
        r = subprocess.run([sys.executable, depot.__file__, "poll", "--once"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.types(d)[:3], ["item_created", "brief_approved", "dispatched"])
        self.assertEqual({p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}, vault_before)
        self.assertEqual(sorted(p.name for p in (self.yard / "items").iterdir()), [d.name])   # nothing was filed
        self.assertFalse((self.yard / "log" / "vault-captures.json").exists())
        self.assertFalse((vault / "Depot" / "Items").exists())

    def test_a_missing_vault_directory_is_the_same_as_unset(self):
        with mock.patch.dict(os.environ, {"DEPOT_VAULT": str(self.tmp / "nope")}):
            self.assertIsNone(depot.vault_root())
            self.assertEqual(depot.vault_capture_scan(False, print), [])
            self.assertEqual(depot.vault_project_all(print), [])

    def test_vault_sync_needs_a_vault(self):
        # Remove DEPOT_VAULT from the subprocess environment explicitly: the daemon exports it (S9 service unit), so the
        # ambient environment cannot be assumed to lack it.
        env = {k: v for k, v in self.env.items() if k != "DEPOT_VAULT"}
        r = subprocess.run([sys.executable, depot.__file__, "vault-sync"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("DEPOT_VAULT", r.stderr)


class TestVaultSyncCommand(VaultCase):
    def test_prints_one_line_per_action_and_is_idempotent(self):
        self.capture()
        self.new_item("existing")
        r = self.depot("vault-sync")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(lines[0], "filed 0002-look-into-the-entra-renewal from idea.md")
        self.assertEqual(sorted(lines[1:]), ["projected 0001-existing", "projected 0002-look-into-the-entra-renewal"])
        self.assertTrue((self.vault / "Depot" / "Items" / "0001-existing.md").exists())
        r = self.depot("vault-sync")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "nothing to do"))

    def test_the_poll_cycle_files_before_it_dispatches_and_projects_after(self):
        d = self.new_item()
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        self.capture()
        self.poll_once()
        names = sorted(p.name for p in (self.yard / "items").iterdir())
        self.assertEqual(names, ["0001-thing", "0002-look-into-the-entra-renewal"])
        self.assertIn("dispatched", self.types(d))
        self.assertIn("filed 0002-look-into-the-entra-renewal", " ".join(self.logs))
        note = (self.vault / "Depot" / "Items" / "0001-thing.md").read_text()
        self.assertIn("stage: Review", note)                       # describes the state after this cycle's dispatch
        self.assertTrue((self.vault / "Depot" / "Items" / "0002-look-into-the-entra-renewal.md").exists())

    def test_a_bridge_failure_never_blocks_a_dispatch(self):
        d = self.new_item()
        self.assertEqual(self.depot("approve", d.name).returncode, 0)
        with mock.patch.object(depot, "vault_capture_scan", side_effect=RuntimeError("vault unreadable")), \
                mock.patch.object(depot, "vault_project_all", side_effect=RuntimeError("vault unreadable")):
            self.poll_once()
        self.assertIn("dispatched", self.types(d))
        self.assertTrue(any("crashed" in m and "vault unreadable" in m for m in self.logs))


class TestOps(unittest.TestCase):
    def test_units_use_explicit_configuration_and_vault_is_opt_in(self):
        poll = (OPS / "depot-poll.service").read_text()
        board = (OPS / "depot-floor.service").read_text()
        self.assertIn("EnvironmentFile=@CONFIG@", poll)
        self.assertIn("EnvironmentFile=@CONFIG@", board)
        self.assertNotIn("/home/", poll)
        self.assertNotIn("Environment=DEPOT_VAULT", poll + board)
        installer = (OPS / "install.py").read_text()
        self.assertIn("--vault", installer)
        self.assertIn("DEPOT_VAULT", (OPS / "README.md").read_text())


if __name__ == "__main__":
    unittest.main()
