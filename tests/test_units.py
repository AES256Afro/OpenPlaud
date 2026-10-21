"""Unit tests for the parts that keep data safe: paths, names, moves, parsing, escaping, origins, sessions."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

DATA = tempfile.mkdtemp()
os.environ.setdefault("OPENPLAUD_DATA", DATA)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openplaud_core as core  # noqa: E402
import openplaud_notes as nt  # noqa: E402
import openplaud_ui as ui  # noqa: E402
import plaud_ai as pa  # noqa: E402


class Paths(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()

    def test_inside_keeps_to_the_notes_folder(self):
        self.assertEqual(nt.inside(self.root, ""), self.root)
        self.assertEqual(nt.inside(self.root, "Work/Clients"), self.root / "Work" / "Clients")
        for bad in ["../etc", "Work/../..", ".hidden", "Work/.git", "/etc/passwd", "a//b", "a/./b", "Work\\..\\x"]:
            self.assertIsNone(nt.inside(self.root, bad), bad)

    def test_long_note_names_are_allowed(self):
        name = "2026-10-20-" + "x" * 150 + "-summary.md"
        self.assertIsNotNone(nt.inside(self.root, name))

    def test_unique_path_never_reuses_a_name(self):
        a = self.root / "n.md"
        names = []
        for i in range(3):
            p = pa.unique_path(a)
            p.write_text(str(i))
            names.append(p.name)
        self.assertEqual(names, ["n.md", "n-2.md", "n-3.md"])
        self.assertEqual(a.read_text(), "0")

    def test_move_never_replaces_and_updates_the_record(self):
        (self.root / "Work").mkdir()
        (self.root / "Work" / "n.md").write_text("already here")
        note = self.root / "n.md"
        note.write_text("moving")
        pa.save_state({"processed": {"r1": {"file": str(note)}}})
        moved = nt.move_note(self.root, note, self.root / "Work")
        self.assertEqual(moved.name, "n-2.md")
        self.assertEqual((self.root / "Work" / "n.md").read_text(), "already here")
        self.assertEqual(moved.read_text(), "moving")
        self.assertEqual(pa.load_state()["processed"]["r1"]["file"], str(moved))


class Parsing(unittest.TestCase):
    NOTE = "# Weekly sync\n\n- Date: 2026-10-20\n- Plaud file ID: `of_abc123`\n\n## Summary\nThe **patch** window moves.\nTo Thursday.\n\n## Action items\n- [ ] Sam: share the list\n- [x] Priya: book the room\n"

    def test_summary_title_actions_id(self):
        self.assertEqual(nt.note_title(self.NOTE, "x"), "Weekly sync")
        self.assertEqual(nt.note_summary(self.NOTE), "The patch window moves. To Thursday.")
        self.assertEqual(nt.note_actions(self.NOTE), [{"done": False, "text": "Sam: share the list"}, {"done": True, "text": "Priya: book the room"}])
        self.assertEqual(nt.plaud_id(self.NOTE), "of_abc123")

    def test_markdown_escapes_html(self):
        out = ui.render_markdown("# <script>alert(1)</script>\n- [ ] <img src=x onerror=alert(1)>\n**<b>bold</b>**")
        self.assertNotIn("<script>", out)
        self.assertNotIn("<img", out)
        self.assertIn("&lt;script&gt;", out)

    def test_transcript_segments_format(self):
        self.assertEqual(pa.format_segment({"start_time": 65000, "end_time": 70000, "speaker": "Sam", "content": " hi "}),
                         "[01:05 - 01:10] Sam: hi")


class Web(unittest.TestCase):
    def test_same_origin(self):
        self.assertTrue(core.same_origin({"Sec-Fetch-Site": "same-origin"}))
        self.assertFalse(core.same_origin({"Sec-Fetch-Site": "cross-site"}))
        self.assertFalse(core.same_origin({"Sec-Fetch-Site": "same-site"}))
        self.assertTrue(core.same_origin({"Origin": "https://box.ts.net:8795", "Host": "127.0.0.1:8795", "X-Forwarded-Host": "box.ts.net:8795"}))
        self.assertFalse(core.same_origin({"Origin": "https://evil.example", "Host": "box.ts.net:8795"}))
        self.assertFalse(core.same_origin({"Origin": "null", "Host": "box.ts.net:8795"}))
        self.assertTrue(core.same_origin({}))

    def test_sessions(self):
        pw = core.Passwords("secret", b"salt:")
        token = pw.make()
        self.assertTrue(pw.valid(token))
        self.assertFalse(pw.valid(token + "0"))
        self.assertFalse(pw.valid(""))
        self.assertFalse(pw.valid(f"{int(time.time()) - 5}.{token.split('.')[1]}"))
        self.assertFalse(core.Passwords("other", b"salt:").valid(token))
        self.assertTrue(pw.check("secret"))
        self.assertFalse(pw.check("Secret"))
        self.assertTrue(core.Passwords("", b"salt:").valid(""))
        self.assertFalse(core.Passwords("", b"salt:").check(""))

    def test_safe_back(self):
        self.assertEqual(core.safe_back("/?folder=Work"), "/?folder=Work")
        for bad in ["//evil.example", "https://evil.example", "javascript:alert(1)", "", "\\\\evil"]:
            self.assertEqual(core.safe_back(bad), "/", bad)


if __name__ == "__main__":
    unittest.main()
