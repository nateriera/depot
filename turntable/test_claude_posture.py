"""S7 tests: the Claude adapter's shell posture (allowed-tools grant + prompts none). New file."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import depot  # noqa: E402
from test_depot import S2Case  # noqa: E402

GRANT = ["--allowedTools", "Bash,Read,Write,Edit,Grep,Glob"]
BASE = ["-p", "--output-format", "stream-json", "--verbose", "--permission-prompts", "none"]


class TestClaudeCommand(unittest.TestCase):
    def cmd(self, **wb):
        return depot.ClaudeAdapter().command(wb, Path("/w"), Path("/r"))[1:]  # drop the binary

    def test_exact_command_line_carries_the_grant_and_keeps_prompts_off(self):
        self.assertEqual(self.cmd(), BASE + GRANT)

    def test_a_model_pin_is_appended_after_the_grant(self):
        args = self.cmd(model="m-1")
        self.assertEqual(args, BASE + GRANT + ["--model", "m-1"])
        self.assertLess(args.index("--allowedTools"), args.index("--model"))

    def test_the_grant_names_the_local_work_tools_and_nothing_else(self):
        self.assertEqual(depot.ClaudeAdapter.allowed_tools, ("Bash", "Read", "Write", "Edit", "Grep", "Glob"))
        args = self.cmd()
        self.assertEqual(args[args.index("--allowedTools") + 1].split(","), list(depot.ClaudeAdapter.allowed_tools))
        for broader in ("--permission-mode", "--dangerously-skip-permissions", "bypassPermissions"):
            self.assertNotIn(broader, args)

    def test_builds_and_gates_get_the_same_posture(self):
        self.assertEqual(self.cmd(kind="build"), self.cmd(kind="gate"))

    def test_the_codex_adapter_is_not_given_the_grant(self):
        self.assertNotIn("--allowedTools", depot.CodexAdapter().command({}, Path("/w"), Path("/r")))


class TestClaudePostureRecorded(S2Case):
    def test_the_new_posture_string_reaches_the_dispatched_event_and_the_manifest(self):
        d = self.approved(adapter="claude")
        self.assertEqual(self.depot("dispatch", d.name).returncode, 0)
        posture = "allowed-tools+prompts-none"
        self.assertEqual(self.of_type(d, "dispatched")[0]["data"]["sandbox"], posture)
        m = json.loads((self.run_dir(d) / "manifest.json").read_text())
        self.assertEqual(m["sandbox"], posture)
        self.assertEqual(depot.ClaudeAdapter.sandbox, posture)

    def test_the_grant_reaches_the_process(self):
        d = self.approved(adapter="claude", model="m-1")
        self.depot("dispatch", d.name)
        args = json.loads((self.rec / "claude-args.json").read_text())
        self.assertEqual(args[args.index("--allowedTools") + 1], "Bash,Read,Write,Edit,Grep,Glob")
        self.assertEqual(args[-2:], ["--model", "m-1"])


if __name__ == "__main__":
    unittest.main()
