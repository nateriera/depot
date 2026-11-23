"""Exercise the shipped floor in a DOM stand-in; no live-yard dogfood or localhost permission needed."""
import subprocess
import unittest
from pathlib import Path

import floor
from test_intake import NODE


class TestLifecyclePage(unittest.TestCase):
    def run_page(self, scenario):
        r = subprocess.run([NODE, str(floor.PAGE_DIR / "test_track_lifecycle.cjs"), scenario], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_done_preference_persistence_per_track_corruption_and_blocked_storage(self):
        self.run_page("prefs")

    def test_lane_menu_drops_intent_renders_receipts_and_arranges_without_writing(self):
        self.run_page("menu")

    def test_shelf_membership_count_and_resume_intent(self):
        self.run_page("shelf")

    def test_archive_includes_every_card_ignores_hide_done_and_only_reopens(self):
        self.run_page("archive")

    def test_done_fold_count_show_and_persistence_across_refresh(self):
        self.run_page("fold")

    def test_intake_explains_paused_tabled_ended_before_submission(self):
        self.run_page("intake")


if __name__ == "__main__":
    unittest.main()
