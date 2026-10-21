"""OpenPlaud end to end: the real web app against a fake Plaud server and a fake model.

Syncs, moves, re-runs and renders every look, and asserts that no note's content is ever lost.
Run: python3 -m unittest discover tests
"""

import hashlib
import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOOKS = ["inbox", "timeline", "actions", "command", "board", "reader", "library", "calendar", "phone", "console"]


class FakeModel(BaseHTTPRequestHandler):
    """Ollama's /api/chat, answering with notes that name the recording."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        user = body["messages"][-1]["content"]
        title = user.split("\n", 1)[0].removeprefix("Recording: ")
        content = f"## Summary\nNotes for {title}.\n\n## Action items\n- [ ] Sam: share the list\n- [x] Priya: book the room\n"
        data = json.dumps({"message": {"content": content}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def contents(notes: Path) -> dict[str, str]:
    """Every note's content hash, by path."""
    return {str(p.relative_to(notes)): hashlib.sha256(p.read_bytes()).hexdigest() for p in notes.rglob("*.md")}


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = HTTPServer(("127.0.0.1", 0), FakeModel)
        threading.Thread(target=cls.model.serve_forever, daemon=True).start()
        cls.data = Path(tempfile.mkdtemp())
        cls.port = free_port()
        cls.log = open(cls.data / "server.log", "w+")
        env = dict(os.environ, OPENPLAUD_DATA=str(cls.data), OPENPLAUD_PORT=str(cls.port), OPENPLAUD_SYNC_EVERY="0",
                   OPENPLAUD_PASSWORD="test-password", OPENPLAUD_PROVIDER="local",
                   OLLAMA_URL=f"http://127.0.0.1:{cls.model.server_port}", LOCAL_MODEL="fake-model",
                   OPENPLAUD_PLAUD_COMMAND=json.dumps([sys.executable, str(ROOT / "tests" / "fake_plaud.py")]),
                   PLAUD_AI_CONFIG=str(cls.data / "none.toml"), HOME=str(cls.data))
        env.pop("CLAUDE_ROUTINE_URL", None)
        env.pop("CLAUDE_ROUTINE_TOKEN", None)
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "openplaud_web.py")], env=env, stdout=cls.log, stderr=subprocess.STDOUT)
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.base + "/healthz", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        jar = http.cookiejar.CookieJar()
        cls.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        cls.jar = jar
        cls.post("/login", password="test-password")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.model.shutdown()

    @classmethod
    def post(cls, path, **form):
        req = urllib.request.Request(cls.base + path, urllib.parse.urlencode(form).encode(), {"Sec-Fetch-Site": "same-origin"})
        return cls.opener.open(req, timeout=30)

    def get(self, path, look=None):
        req = urllib.request.Request(self.base + path)
        if look:
            req.add_header("Cookie", "; ".join([f"{c.name}={c.value}" for c in self.jar] + [f"openplaud_look={look}"]))
            return urllib.request.urlopen(req, timeout=30)
        return self.opener.open(req, timeout=30)

    def wait_idle(self):
        for _ in range(300):
            status = json.loads(self.get("/api/status").read())
            if not status["busy"]:
                return status
            time.sleep(0.1)
        self.fail("jobs never finished")

    def server_log(self) -> str:
        self.log.flush()
        return (self.data / "server.log").read_text()

    def test_everything(self):
        notes = self.data / "notes"
        # Sync: every recording with a transcript gets notes; the empty clip is remembered as waiting.
        self.post("/sync")
        log = self.wait_idle()["log"]
        made = contents(notes)
        self.assertEqual(len(made), 3, log)
        state = json.loads((self.data / ".plaud_ai_state.json").read_text())
        self.assertIn("rec_memo", state["no_transcript"])
        long_note = next(p for p in notes.glob("*.md") if "Long-workshop" in p.name)
        self.assertIn("Notes for Long workshop.", long_note.read_text())
        before = set(made.values())

        # Folder, move, and a re-run: each keeps every earlier note.
        self.post("/folders", parent="", name="Work")
        sync_note = next(p for p in notes.glob("*.md") if "Weekly-security-sync" in p.name)
        self.post("/notes/move", note=sync_note.name, dest="Work", back="/")
        self.assertTrue((notes / "Work" / sync_note.name).exists())
        self.post("/process", id="rec_sync", task="summary", back="/")
        self.wait_idle()
        after = contents(notes)
        self.assertTrue(before <= set(after.values()), "a note's content disappeared")
        self.assertEqual(len(after), 4)

        # The recording links to its newest note; the moved one is listed in its folder; every look renders.
        for look in LOOKS:
            html = self.get("/", look=look).read().decode()
            self.assertIn("Weekly security sync", html, look)
        self.assertIn("/notes/Work/", self.get("/?folder=Work", look="console").read().decode())
        page = self.get("/notes/Work/" + urllib.parse.quote(sync_note.name), look="reader").read().decode()
        self.assertIn("Notes for Weekly security sync.", page)
        self.assertEqual(self.get("/claude").status, 200)
        self.assertNotIn("Traceback", self.server_log())


if __name__ == "__main__":
    unittest.main()
