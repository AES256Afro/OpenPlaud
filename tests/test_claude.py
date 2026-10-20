"""Send to Claude, against a fake fire endpoint. Run: python3 -m unittest discover tests"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DATA = tempfile.mkdtemp()
os.environ.update({"OPENPLAUD_DATA": DATA, "OPENPLAUD_SYNC_EVERY": "0",
                   "CLAUDE_ROUTINE_URL": "trig_01TESTTESTTEST", "CLAUDE_ROUTINE_TOKEN": "sk-ant-oat01-test"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude_routine as cr  # noqa: E402
import openplaud_web as web  # noqa: E402
import plaud_ai as pa  # noqa: E402


class Fake(BaseHTTPRequestHandler):
    calls: list = []
    mode = "ok"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.calls.append({"auth": self.headers["Authorization"], "version": self.headers["anthropic-version"], "text": body["text"]})
        if Fake.mode == "slow":
            time.sleep(2)
        status, payload, headers = {
            "ok": (200, {"type": "routine_fire", "claude_code_session_id": "session_01X", "claude_code_session_url": "https://claude.ai/code/session_01X"}, {}),
            "429": (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "limit"}}, {"Retry-After": "120"}),
            "401": (401, {"type": "error", "error": {"type": "authentication_error", "message": "bad token"}}, {}),
        }.get(Fake.mode, (200, {}, {}))
        data = json.dumps(payload).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


server = HTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=server.serve_forever, daemon=True).start()
FAKE_URL = f"http://127.0.0.1:{server.server_port}/v1/claude_code/routines/trig_01TESTTESTTEST/fire"
real_normalize = cr.normalize_url
cr.normalize_url = lambda value: FAKE_URL


def rec(rid="rec1", minutes_ago=5):
    start = (datetime.now() - timedelta(minutes=minutes_ago)).astimezone().astimezone(tz=None)
    return {"id": rid, "name": "Weekly sync", "duration": 600000,
            "start_at": start.astimezone(tz=__import__("datetime").timezone.utc).replace(tzinfo=None).isoformat()}


class SendToClaude(unittest.TestCase):
    def setUp(self):
        Fake.calls.clear()
        Fake.mode = "ok"
        state = {"processed": {}, "claude": {"since": (datetime.now() - timedelta(hours=1)).isoformat(), "sent": {}, "failed": {}, "fires": []}}
        pa.save_state(state)
        self.note = Path(DATA) / "note.md"
        self.note.write_text("# Weekly sync\n\n## Summary\nPatch window moves.\n")
        self.log: list = []

    def send(self, r=None, transcript="[00:00 - 00:05] Speaker 1: hello", manual=False):
        web.send_to_claude(r or rec(), self.note, transcript, self.log.append, manual=manual)
        return pa.load_state()["claude"]

    def test_sends_notes_and_transcript_and_records_the_session(self):
        c = self.send()
        self.assertEqual(len(Fake.calls), 1)
        call = Fake.calls[0]
        self.assertEqual(call["auth"], "Bearer sk-ant-oat01-test")
        self.assertEqual(call["version"], "2023-06-01")
        self.assertIn("Patch window moves.", call["text"])
        self.assertIn("Speaker 1: hello", call["text"])
        self.assertIn("Plaud file ID: rec1", call["text"])
        self.assertEqual(c["sent"]["rec1"]["session_url"], "https://claude.ai/code/session_01X")

    def test_sends_each_recording_once_unless_asked(self):
        self.send()
        self.send()
        self.assertEqual(len(Fake.calls), 1)
        self.send(manual=True)
        self.assertEqual(len(Fake.calls), 2)

    def test_skips_recordings_from_before_it_was_switched_on(self):
        self.send(rec("old", minutes_ago=600))
        self.assertEqual(Fake.calls, [])
        self.send(rec("old", minutes_ago=600), manual=True)
        self.assertEqual(len(Fake.calls), 1)

    def test_rate_limit_is_kept_for_retry(self):
        Fake.mode = "429"
        c = self.send()
        self.assertTrue(c["failed"]["rec1"]["retryable"])
        self.assertNotIn("rec1", c["sent"])

    def test_bad_token_waits_for_the_owner(self):
        Fake.mode = "401"
        c = self.send()
        self.assertFalse(c["failed"]["rec1"]["retryable"])
        self.assertIn("token", c["failed"]["rec1"]["error"])

    def test_timeout_is_never_retried(self):
        Fake.mode = "slow"
        original = cr.fire
        cr.fire = lambda url, token, text, timeout=30: original(url, token, text, timeout=0.5)
        try:
            c = self.send()
        finally:
            cr.fire = original
        self.assertFalse(c["failed"]["rec1"]["retryable"])
        self.assertIn("Not retried", c["failed"]["rec1"]["error"])

    def test_long_transcript_fits_the_limit(self):
        transcript = "\n".join(f"[00:{i % 60:02d} - 00:00] Speaker 1: " + "words " * 30 for i in range(2000))
        self.send(transcript=transcript)
        text = Fake.calls[0]["text"]
        self.assertLessEqual(len(text), cr.MAX_TEXT)
        self.assertIn("Transcript cut at line", text)
        self.assertIn("Patch window moves.", text)

    def test_hourly_budget_holds_back_a_burst(self):
        state = pa.load_state()
        state["claude"]["fires"] = [time.time()] * 28
        pa.save_state(state)
        c = self.send()
        self.assertEqual(Fake.calls, [])
        self.assertTrue(c["failed"]["rec1"]["retryable"])

    def test_token_only_goes_to_anthropic(self):
        self.assertEqual(real_normalize("trig_01ABC"), "https://api.anthropic.com/v1/claude_code/routines/trig_01ABC/fire")
        for bad in ["https://evil.example/v1/claude_code/routines/trig_01ABC/fire",
                    "http://api.anthropic.com/v1/claude_code/routines/trig_01ABC/fire",
                    "https://api.anthropic.com.evil.example/v1/claude_code/routines/trig_01ABC/fire", ""]:
            with self.assertRaises(cr.RoutineError):
                real_normalize(bad)


if __name__ == "__main__":
    unittest.main()
