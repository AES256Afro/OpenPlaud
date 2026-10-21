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
        moved, _ = nt.move_note(self.root, note, self.root / "Work")
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


class Engine(unittest.TestCase):
    def test_http_json_retries_overloads_and_honors_retry_after(self):
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        calls = []

        class Flaky(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                calls.append(1)
                if len(calls) < 3:
                    self.send_response(503 if len(calls) == 1 else 429)
                    if len(calls) == 2:
                        self.send_header("Retry-After", "7")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = _json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = HTTPServer(("127.0.0.1", 0), Flaky)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        waits = []
        out = pa.http_json(f"http://127.0.0.1:{server.server_port}/", {"x": 1}, {"Content-Type": "application/json"}, 5, sleep=waits.append)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(waits, [2, 7])
        server.shutdown()

    def test_http_json_does_not_retry_a_bad_request(self):
        import threading
        import urllib.error
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Bad(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.send_response(400)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = HTTPServer(("127.0.0.1", 0), Bad)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        waits = []
        with self.assertRaises(urllib.error.HTTPError):
            pa.http_json(f"http://127.0.0.1:{server.server_port}/", {}, {}, 5, sleep=waits.append)
        self.assertEqual(waits, [])
        server.shutdown()

    def test_claude_cost(self):
        self.assertAlmostEqual(pa.claude_cost("claude-haiku-5-5", 12_000, 2_000), (12_000 * 0.10 + 2_000 * 0.50) / 1e6)
        self.assertAlmostEqual(pa.claude_cost("claude-haiku-5-5", 150_000, 1_000), (150_000 * 0.50 + 1_000 * 2.50) / 1e6)
        self.assertAlmostEqual(pa.claude_cost("claude-opus-5-5", 1_000_000, 0), 4.0)
        self.assertIsNone(pa.claude_cost("some-other-model", 10, 10))

    def test_backend_for_spec(self):
        cfg = pa.load_config()
        self.assertEqual(pa.backend_for(cfg, "local:qwen3:8b").cfg["model"], "qwen3:8b")
        self.assertEqual(cfg["local"]["model"], pa.load_config()["local"]["model"])
        with self.assertRaises(ValueError):
            pa.backend_for(cfg, "nope:x")

    def test_failures_count_and_clear(self):
        pa.save_state({"processed": {}})
        self.assertEqual(pa.record_failure("r9", RuntimeError("boom")), 1)
        self.assertEqual(pa.record_failure("r9", RuntimeError("boom again")), 2)
        state = pa.load_state()
        self.assertEqual(state["failed"]["r9"]["count"], 2)
        self.assertIn("boom again", state["failed"]["r9"]["error"])

    def test_damaged_state_is_set_aside_not_deleted(self):
        pa.STATE_PATH.write_text("{not json")
        self.assertIn("processed", pa.load_state())
        self.assertTrue(any(p.name.startswith(pa.STATE_PATH.name + ".damaged-") for p in pa.STATE_PATH.parent.iterdir()))


class Health(unittest.TestCase):
    def _token(self, payload):
        import base64
        import json as _json
        part = base64.urlsafe_b64encode(_json.dumps(payload).encode()).decode().rstrip("=")
        path = Path(tempfile.mkdtemp()) / "tokens.json"
        path.write_text(_json.dumps({"refresh_token": f"h.{part}.s"}))
        return path

    def test_reads_only_the_end_date(self):
        ok = core.plaud_health(self._token({"exp": int(time.time()) + 20 * 86400}))
        self.assertFalse(ok["warn"])
        self.assertGreater(ok["days"], 19)
        self.assertTrue(core.plaud_health(self._token({"exp": int(time.time()) + 3600}))["warn"])
        self.assertEqual(core.plaud_health(self._token({}))["until"], None)
        self.assertIsNone(core.plaud_health(Path(tempfile.mkdtemp()) / "missing.json"))


class SweepOne(unittest.TestCase):
    """Regression tests for the first bug and security sweep."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()

    def test_hosts_and_back_paths(self):
        for ok in ["127.0.0.1:8795", "bigbox.tail6233fa.ts.net", "bigbox:8795", "[::1]:8795", "localhost"]:
            self.assertTrue(core.host_allowed({"Host": ok}), ok)
        self.assertFalse(core.host_allowed({"Host": "evil.example:8795"}))
        self.assertFalse(core.host_allowed({"Host": "127.0.0.1", "X-Forwarded-Host": "evil.example"}))
        self.assertTrue(core.host_allowed({"Host": "notes.example.org"}, "notes.example.org"))
        for bad in ["/\\evil.com", "/x\r\nSet-Cookie: a=b", "/x\nb"]:
            self.assertEqual(core.safe_back(bad), "/", repr(bad))
        self.assertEqual(core.safe_back("/notes/Work/a%20b.md?x=1#notes"), "/notes/Work/a%20b.md?x=1#notes")

    def test_state_edits_never_lose_each_other(self):
        import threading
        pa.save_state({"processed": {}})

        def bump(i):
            with pa.editing_state() as s:
                s.setdefault("n", []).append(i)
        threads = [threading.Thread(target=bump, args=(i,)) for i in range(40)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(pa.load_state()["n"]), list(range(40)))

    def test_new_notes_and_moves_never_replace(self):
        a = self.root / "n.md"
        a.write_text("mine")
        self.assertEqual(pa.write_new(a, "new").name, "n-2.md")
        self.assertEqual(a.read_text(), "mine")
        with self.assertRaises(ValueError):
            nt.move_note(self.root, a, self.root)

    def test_ticks_keep_line_endings(self):
        note = self.root / "crlf.md"
        note.write_bytes(b"# T\r\n\r\n- [ ] Sam: call\r\n- [ ] Priya: book\r\n")
        self.assertTrue(nt.set_action(note, 2, "Sam: call", True))
        self.assertEqual(note.read_bytes(), b"# T\r\n\r\n- [x] Sam: call\r\n- [ ] Priya: book\r\n")

    def test_names(self):
        self.assertIsNotNone(nt.inside(self.root, "Café/réunion.md"))
        self.assertFalse(nt.FOLDER_NAME.fullmatch("x.md"))
        self.assertTrue(nt.FOLDER_NAME.fullmatch("Clients 2026"))
        (self.root / "x.md").mkdir()
        self.assertEqual(nt.all_notes(self.root), [])

    def test_plaud_gets_no_secrets_and_urls_hide_passwords(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        os.environ["CLAUDE_ROUTINE_TOKEN"] = "tok"
        os.environ["OPENPLAUD_AUTH_URL_FILE"] = "/tmp/x"
        env = pa.plaud_env()
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("CLAUDE_ROUTINE_TOKEN", env)
        self.assertIn("OPENPLAUD_AUTH_URL_FILE", env)
        backend = pa.LocalBackend(dict(pa.load_config()["local"], base_url="http://user:pw@box:11434/?key=s", model="m"))
        self.assertNotIn("pw", backend.name)
        self.assertNotIn("key=s", backend.name)

    def test_unpriced_models_dont_slip_past_the_cap(self):
        cfg = pa.load_config()
        self.assertFalse(pa.priced(pa.backend_for(cfg, "chatgpt:gpt-x")))
        claude = object.__new__(pa.ClaudeBackend)  # no SDK needed to ask about prices
        claude.cfg = {"model": "claude-haiku-5-5"}
        self.assertTrue(pa.priced(claude))
        claude.cfg = {"model": "claude-something-new"}
        self.assertFalse(pa.priced(claude))
        self.assertTrue(pa.priced(pa.backend_for(cfg, "local:m")))

    def test_claude_answers_that_cant_be_read_are_never_retried(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import claude_routine as cr
        replies = [(200, b"not json"), (500, b"{}"), (429, b"{}"), (200, b'{"type": "routine_fire", "claude_code_session_url": "javascript:x"}')]

        class Odd(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                status, body = replies.pop(0)
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = HTTPServer(("127.0.0.1", 0), Odd)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        old = cr.normalize_url
        cr.normalize_url = lambda url: f"http://127.0.0.1:{server.server_port}/fire"
        try:
            outcomes = []
            for _ in range(4):
                with self.assertRaises(cr.RoutineError) as ctx:
                    cr.fire("x", "t", "hi")
                outcomes.append(ctx.exception.retryable)
        finally:
            cr.normalize_url = old
            server.shutdown()
        self.assertEqual(outcomes, [False, False, True, False])


class SweepTwo(unittest.TestCase):
    def test_a_damaged_state_is_recovered_and_stays_recovered(self):
        pa.save_state({"processed": {"r1": {"file": "x"}}})
        pa.save_state({"processed": {"r1": {"file": "x"}, "r2": {"file": "y"}}})
        pa.STATE_PATH.write_text("{half")
        self.assertIn("r1", pa.load_state()["processed"])
        self.assertIn("r1", pa.load_state()["processed"])  # the second load reads the recovered file
        pa.save_state(pa.load_state())
        pa.save_state(pa.load_state())
        self.assertIn("r1", pa.load_state()["processed"])

    def test_ticks_keep_file_permissions_and_leave_no_temp_files(self):
        root = Path(tempfile.mkdtemp())
        note = root / "n.md"
        note.write_text("- [ ] Sam: call\n")
        note.chmod(0o644)
        self.assertTrue(nt.set_action(note, 0, "Sam: call", True))
        self.assertEqual(note.stat().st_mode & 0o777, 0o644)
        self.assertEqual([p.name for p in root.iterdir()], ["n.md"])

    def test_urls_never_show_credentials(self):
        self.assertEqual(pa.redact_url("http://user:s3cret@10.0.0.2:11434/v1?key=abc"), "http://10.0.0.2:11434/v1")
        os.environ["PLAUD_CLIENT_SECRET"] = "plaud-own"
        os.environ["OLLAMA_URL"] = "http://u:p@box:11434"
        env = pa.plaud_env()
        self.assertIn("PLAUD_CLIENT_SECRET", env)
        self.assertNotIn("OLLAMA_URL", env)

    def test_more_real_host_names(self):
        for ok in ["mac-mini.local:8795", "box.tail1.ts.net.", "notes.example.org:443"]:
            self.assertTrue(core.host_allowed({"Host": ok}, "notes.example.org:443,other.example"), ok)
        self.assertFalse(core.host_allowed({"Host": "evil.example"}, "notes.example.org"))

    def test_odd_unicode_names(self):
        root = Path(tempfile.mkdtemp()).resolve()
        self.assertIsNotNone(nt.inside(root, "été.md"))
        self.assertIsNone(nt.inside(root, "Workㅤ"))
        self.assertIsNone(nt.inside(root, "‮evil.md"))

    def test_dangling_links_count_as_taken_names(self):
        root = Path(tempfile.mkdtemp())
        (root / "a.md").symlink_to(root / "missing")
        self.assertEqual(pa.write_new(root / "a.md", "x").name, "a-2.md")

    def test_the_right_password_never_waits_behind_wrong_guesses(self):
        import threading
        pw = core.Passwords("secret", b"salt:")
        threading.Thread(target=pw.check, args=("wrong",)).start()
        time.sleep(0.05)
        start = time.time()
        self.assertTrue(pw.check("secret"))
        self.assertLess(time.time() - start, 0.5)


class SweepThree(unittest.TestCase):
    def test_passwords_in_model_addresses_never_reach_messages(self):
        for url in ["https://user:s3cret@llm.example/api/chat", "user:s3cret@llm.example/api/chat"]:
            with self.assertRaises(Exception) as ctx:
                pa.http_json(url, {}, {}, 2, attempts=1)
            self.assertNotIn("s3cret", str(ctx.exception))
