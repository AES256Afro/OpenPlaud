"""Pieces shared by OpenPlaud and Plaud2Claude: one Plaud session, the headless Plaud sign-in,
page passwords and the same-origin check. Importing this module starts nothing."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import plaud_ai as pa

PLAUD_CALLBACK = "http://127.0.0.1:8199/auth/callback"


class PlaudSession:
    """One Plaud MCP process shared by every request, used by one caller at a time."""

    def __init__(self, list_days: int = 30):
        self.lock = threading.Lock()
        self.list_days = list_days
        self._plaud: pa.Plaud | None = None
        self._user: tuple[float, str | None] = (0.0, None)
        self._recordings: tuple[float, list[dict]] = (0.0, [])

    def get(self) -> pa.Plaud:
        if self._plaud is None or not self._plaud.alive():
            self._plaud = pa.Plaud(pa.Plaud.default_command(pa.load_config()))
        return self._plaud

    def forget(self) -> None:
        self._user = (0.0, None)
        self._recordings = (0.0, [])

    def cached_user(self) -> str | None:
        return self._user[1]

    def user(self) -> str | None:
        """Who is signed in (cached), or None. Raises TimeoutError while another caller holds the session."""
        stamp, who = self._user
        if time.time() - stamp < 300:
            return who
        if not self.lock.acquire(timeout=5):
            raise TimeoutError
        try:
            info = json.loads(self.get().call("get_current_user"))
            who = info.get("email") or info.get("nickname") or "your Plaud account"
        except pa.PlaudError:
            who = None
        finally:
            self.lock.release()
        self._user = (time.time(), who)
        return who

    def recordings(self) -> list[dict]:
        stamp, recs = self._recordings
        if time.time() - stamp < 60:
            return recs
        if not self.lock.acquire(timeout=5):
            raise TimeoutError
        try:
            since = (date.today() - timedelta(days=self.list_days)).isoformat()
            recs = pa.list_recordings(self.get(), since, None, None)
        finally:
            self.lock.release()
        recs.sort(key=lambda r: pa.recorded_at(r) or datetime.min, reverse=True)
        self._recordings = (time.time(), recs)
        return recs

    def transcript(self, file_id: str) -> list[str] | None:
        if not self.lock.acquire(timeout=5):
            return None
        try:
            text = pa.fetch_transcript(self.get(), file_id)
            return text.splitlines() if text else []
        except pa.PlaudError:
            return None
        finally:
            self.lock.release()


class PlaudSignIn:
    """Runs Plaud's `login` tool, captures the sign-in link it tries to open, and completes the
    callback from inside the container with the address the owner pastes back."""

    def __init__(self, plaud: PlaudSession, url_file: Path):
        self.plaud = plaud
        self.url_file = url_file
        self.state = {"status": "idle", "url": None, "message": None}
        self.thread: threading.Thread | None = None

    def reset(self) -> None:
        self.state = {"status": "idle", "url": None, "message": None}

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.url_file.unlink(missing_ok=True)
        self.state = {"status": "starting", "url": None, "message": None}
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        for _ in range(60):
            if self.url_file.exists() and self.url_file.read_text().strip():
                self.state.update(status="waiting", url=self.url_file.read_text().strip())
                return
            if self.state["status"] in ("success", "failed"):
                return
            time.sleep(0.25)
        if self.state["status"] == "starting":
            self.state.update(status="failed", message="Plaud didn't produce a sign-in link. Try again.")

    def _run(self) -> None:
        with self.plaud.lock:
            try:
                text = self.plaud.get().call("login")
                ok = "Successfully" in text or "Already logged in" in text
                self.state.update(status="success" if ok else "failed", message=text)
            except pa.PlaudError as err:
                self.state.update(status="failed", message=str(err).split("\n")[0])
        self.plaud.forget()

    def complete(self, pasted: str) -> None:
        query = urllib.parse.urlsplit(pasted.strip()).query or pasted.strip().lstrip("?")
        params = urllib.parse.parse_qs(query)
        if "code" not in params or "state" not in params:
            self.state["message"] = "That address has no sign-in code in it. Paste the full address from the browser's address bar."
            return
        if self.state["status"] != "waiting":
            self.state["message"] = "The sign-in expired (Plaud allows 2 minutes). Start again."
            return
        try:
            with urllib.request.urlopen(f"{PLAUD_CALLBACK}?{query}", timeout=30):
                pass
        except urllib.error.URLError as err:
            self.state["message"] = f"Couldn't finish the sign-in: {err}"
            return
        if self.thread:
            self.thread.join(timeout=30)

    def sign_out(self) -> None:
        with self.plaud.lock:
            try:
                self.plaud.get().call("logout")
            except pa.PlaudError:
                pass
        self.plaud.forget()
        self.reset()

    def page_state(self) -> dict:
        """What the page shows about the Plaud sign-in: signed_in, waiting, busy or signed_out."""
        st = self.state
        out = {"state": "signed_out", "who": None, "url": st.get("url"), "message": st.get("message"), "failed": st["status"] == "failed"}
        if st["status"] in ("starting", "waiting"):
            out["state"] = "waiting" if st.get("url") else "busy"
            return out
        try:
            who = self.plaud.user()
            if who:
                out.update(state="signed_in", who=who)
        except TimeoutError:
            who = self.plaud.cached_user()
            out.update(state="signed_in" if who else "busy", who=who)
        return out


class Passwords:
    """The page's own password and its signed session cookie."""

    def __init__(self, password: str, salt: bytes, days: int = 30):
        self.password = password
        self.days = days
        self.key = hashlib.sha256(salt + password.encode()).digest()

    def check(self, given: str) -> bool:
        return bool(self.password) and hmac.compare_digest(given.encode(), self.password.encode())

    def make(self) -> str:
        expires = str(int(time.time()) + self.days * 86400)
        return f"{expires}.{hmac.new(self.key, expires.encode(), 'sha256').hexdigest()}"

    def valid(self, token: str) -> bool:
        if not self.password:
            return True
        expires, _, mac = (token or "").partition(".")
        if not expires.isdigit() or int(expires) < time.time():
            return False
        return hmac.compare_digest(mac, hmac.new(self.key, expires.encode(), "sha256").hexdigest())


def same_origin(headers) -> bool:
    site = headers.get("Sec-Fetch-Site")
    if site:
        return site in ("same-origin", "none")
    origin = headers.get("Origin")
    if not origin:
        return True
    # Behind Tailscale Serve the Host header can be the backend's; the public name is forwarded.
    return urllib.parse.urlsplit(origin).netloc in {headers.get("Host"), headers.get("X-Forwarded-Host")}


def cookie(headers, name: str) -> str | None:
    for part in headers.get("Cookie", "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def safe_back(value: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") else "/"
