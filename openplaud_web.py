"""OpenPlaud web app: sign in to Plaud, see recordings, summarize them, read and organize the notes.

Settings come from the environment (BoxPilot's app settings): see README.md for the list. The page is
drawn by openplaud_ui in one of ten looks; OPENPLAUD_LOOK sets the default and each browser can pick its own.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import openplaud_notes as nt
import openplaud_ui as ui
import plaud_ai as pa

PORT = int(os.environ.get("OPENPLAUD_PORT", "8795"))
PASSWORD = os.environ.get("OPENPLAUD_PASSWORD", "")
SYNC_EVERY_MIN = int(os.environ.get("OPENPLAUD_SYNC_EVERY", "60") or 0)
SYNC_DAYS = int(os.environ.get("OPENPLAUD_SYNC_DAYS", "7") or 7)
LIST_DAYS = int(os.environ.get("OPENPLAUD_LIST_DAYS", "30") or 30)
DEFAULT_TASK = os.environ.get("OPENPLAUD_TASK", "summary")
DEFAULT_LOOK = os.environ.get("OPENPLAUD_LOOK", "inbox")
AUTH_URL_FILE = Path(os.environ.get("OPENPLAUD_AUTH_URL_FILE", "/tmp/openplaud-auth-url"))
PLAUD_CALLBACK = "http://127.0.0.1:8199/auth/callback"
COOKIE = "openplaud_session"
LOOK_COOKIE = "openplaud_look"
SESSION_DAYS = 30
PROVIDER_LABELS = {"claude": "Claude", "chatgpt": "ChatGPT", "local": "Local model"}
TASK_LABELS = {"summary": "Meeting notes", "actions": "Action items", "followup": "Follow-up email"}


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------- Plaud session

class PlaudSession:
    """One Plaud MCP process shared by every request, used by one caller at a time."""

    def __init__(self):
        self.lock = threading.Lock()
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
            since = (date.today() - timedelta(days=LIST_DAYS)).isoformat()
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


plaud = PlaudSession()


# ---------------------------------------------------------------- sign-in to Plaud (headless OAuth)

class PlaudSignIn:
    """Runs Plaud's `login` tool, captures the sign-in link it tries to open, and completes the
    callback from inside the container with the address the owner pastes back."""

    def __init__(self):
        self.state = {"status": "idle", "url": None, "message": None}
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        AUTH_URL_FILE.unlink(missing_ok=True)
        self.state = {"status": "starting", "url": None, "message": None}
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        for _ in range(60):
            if AUTH_URL_FILE.exists() and AUTH_URL_FILE.read_text().strip():
                self.state.update(status="waiting", url=AUTH_URL_FILE.read_text().strip())
                return
            if self.state["status"] in ("success", "failed"):
                return
            time.sleep(0.25)
        if self.state["status"] == "starting":
            self.state.update(status="failed", message="Plaud didn't produce a sign-in link. Try again.")

    def _run(self) -> None:
        with plaud.lock:
            try:
                text = plaud.get().call("login")
                ok = "Successfully" in text or "Already logged in" in text
                self.state.update(status="success" if ok else "failed", message=text)
            except pa.PlaudError as err:
                self.state.update(status="failed", message=str(err).split("\n")[0])
        plaud.forget()

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


signin = PlaudSignIn()


# ---------------------------------------------------------------- background jobs

class Jobs:
    def __init__(self):
        self.queue: queue.Queue = queue.Queue()
        self.log: deque[str] = deque(maxlen=300)
        self.current: str | None = None
        self.active_id: str | None = None
        self.pending: set[tuple] = set()
        self.last_sync: str | None = None
        self.next_sync: datetime | None = None
        threading.Thread(target=self._worker, daemon=True).start()
        if SYNC_EVERY_MIN > 0:
            threading.Thread(target=self._scheduler, daemon=True).start()

    def say(self, message: str) -> None:
        self.log.append(f"{now()}  {message}")

    def add(self, job: tuple) -> bool:
        if job in self.pending:
            return False
        self.pending.add(job)
        self.queue.put(job)
        return True

    def queued_ids(self) -> set[str]:
        return {job[1] for job in self.pending if job[0] == "process"}

    def _scheduler(self) -> None:
        self.next_sync = datetime.now() + timedelta(seconds=30)
        time.sleep(30)
        while True:
            self.next_sync = datetime.now() + timedelta(minutes=SYNC_EVERY_MIN)
            self.add(("sync",))
            time.sleep(SYNC_EVERY_MIN * 60)

    def _worker(self) -> None:
        while True:
            job = self.queue.get()
            self.current = "Syncing new recordings" if job[0] == "sync" else "Summarizing a recording"
            self.active_id = job[1] if job[0] == "process" else None
            try:
                self._run(job)
            except pa.NotSignedIn:
                self.say("Not signed in to Plaud. Sign in from the page to continue.")
            except Exception as err:
                self.say(f"✗ {type(err).__name__}: {err}")
            finally:
                self.pending.discard(job)
                self.current = None
                self.active_id = None
                plaud.forget()

    def _run(self, job: tuple) -> None:
        cfg = pa.load_config()
        backend = pa.make_backend(cfg, cfg["backend"])
        out_dir = pa.notes_dir(cfg)
        with plaud.lock:
            p = plaud.get()
            if job[0] == "sync":
                self.say(f"Sync started (last {SYNC_DAYS} days, {backend.name})")
                written, failures = pa.sync(p, backend, SYNC_DAYS, pa.TASKS[DEFAULT_TASK], DEFAULT_TASK, out_dir,
                                            log=self.say, on_start=lambda rec: setattr(self, "active_id", pa.rec_id(rec)))
                self.last_sync = datetime.now().strftime("%-d %b %H:%M")
                self.say(f"Sync finished: {len(written)} new, {failures} failed")
            else:
                _, file_id, task = job
                rec = pa.unwrap_file(p.call_json("get_file", file_id=file_id))
                pa.process_and_record(p, backend, rec, pa.TASKS[task], task, out_dir, log=self.say)


jobs = Jobs()


# ---------------------------------------------------------------- sessions (the page's own password)

def _session_key() -> bytes:
    return hashlib.sha256(b"openplaud-session:" + PASSWORD.encode()).digest()


def make_session() -> str:
    expires = str(int(time.time()) + SESSION_DAYS * 86400)
    return f"{expires}.{hmac.new(_session_key(), expires.encode(), 'sha256').hexdigest()}"


def valid_session(token: str) -> bool:
    expires, _, mac = token.partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(mac, hmac.new(_session_key(), expires.encode(), "sha256").hexdigest())


# ---------------------------------------------------------------- what a page shows

def model_label(cfg: dict) -> str:
    provider = cfg["backend"]
    model = {"claude": cfg["claude"]["model"], "chatgpt": cfg["chatgpt"]["model"], "local": cfg["local"]["model"]}.get(provider, "?")
    return f"{PROVIDER_LABELS.get(provider, provider)} · {model}"


def read_note(root: Path, path: Path) -> dict:
    text = path.read_text()
    folder = nt.rel_of(root, path.parent) if path.parent != root else ""
    return {"rel": nt.rel_of(root, path), "url": nt.note_url(root, path), "folder": folder, "back": nt.folder_url(folder),
            "title": nt.note_title(text, path.stem), "html": ui.render_markdown(text), "text": text, "stem": path.stem}


def build_context(look: str, query: dict, flash: str | None, here: str) -> dict:
    cfg = pa.load_config()
    root = pa.notes_dir(cfg)
    root.mkdir(parents=True, exist_ok=True)
    folder_param = query.get("folder", "")
    here_dir = nt.inside(root, folder_param)
    here_dir = here_dir if here_dir and here_dir.is_dir() else root
    folder = nt.rel_of(root, here_dir) if here_dir != root else ""

    st = signin.state
    plaud_state = {"state": "signed_out", "who": None, "url": st.get("url"), "message": st.get("message"), "failed": st["status"] == "failed"}
    if st["status"] in ("starting", "waiting"):
        plaud_state["state"] = "waiting" if st.get("url") else "busy"
    else:
        try:
            who = plaud.user()
            if who:
                plaud_state.update(state="signed_in", who=who)
        except TimeoutError:
            plaud_state["state"] = "busy" if not plaud._user[1] else "signed_in"
            plaud_state["who"] = plaud._user[1]

    # Every note once: titles, summaries and action items for the looks that show them.
    notes = []
    for path in nt.all_notes(root):
        try:
            text = path.read_text()
        except OSError:
            continue
        notes.append({"path": path, "rel": nt.rel_of(root, path), "url": nt.note_url(root, path), "stem": path.stem,
                      "folder": nt.rel_of(root, path.parent) if path.parent != root else "", "title": nt.note_title(text, path.stem),
                      "summary": nt.note_summary(text), "actions": nt.note_actions(text), "plaud_id": nt.plaud_id(text),
                      "day": datetime.fromtimestamp(path.stat().st_mtime).date()})
    by_path = {n["path"].resolve(): n for n in notes}

    state = pa.load_state()
    recordings, recordings_error = None, None
    if plaud_state["state"] == "signed_in":
        try:
            raw = plaud.recordings()
            queued = jobs.queued_ids()
            recordings = []
            for r in raw:
                rid = pa.rec_id(r)
                when = pa.recorded_at(r)
                path = nt.note_for(root, state["processed"].get(rid))
                note = by_path.get(path.resolve()) if path else None
                if rid == jobs.active_id:
                    status = "writing"
                elif rid in queued:
                    status = "queued"
                elif note:
                    status = "filed" if note["folder"] else "ready"
                elif rid in state.get("no_transcript", {}):
                    status = "no_transcript"
                else:
                    status = "new"
                recordings.append({
                    "id": rid, "name": r.get("name") or rid, "when": when, "day": when.date() if when else None,
                    "when_s": when.strftime("%Y-%m-%d %H:%M") if when else "?", "time_s": when.strftime("%-I:%M %p") if when else "",
                    "duration": pa.duration(r), "status": status,
                    "note_url": note["url"] if note else None, "note_rel": note["rel"] if note else None,
                    "note_folder": note["folder"] if note else "", "summary": note["summary"] if note else "",
                    "actions_open": sum(1 for a in note["actions"] if not a["done"]) if note else 0})
        except TimeoutError:
            recordings_error = "Plaud is busy with a sync. The list refreshes when it's done."
        except pa.PlaudError as err:
            recordings_error = str(err)

    counts: dict[str, int] = {}
    for n in notes:
        parts = n["folder"].split("/") if n["folder"] else []
        for i in range(1, len(parts) + 1):
            counts["/".join(parts[:i])] = counts.get("/".join(parts[:i]), 0) + 1
    folders = nt.all_folders(root)
    folder_counts = [{"rel": f, "name": f.rsplit("/", 1)[-1], "count": counts.get(f, 0), "url": nt.folder_url(f)} for f in folders]
    subfolders = [fc for fc in folder_counts if (fc["rel"].rsplit("/", 1)[0] if "/" in fc["rel"] else "") == folder]

    crumbs = [("Notes", nt.folder_url(""))]
    parts = folder.split("/") if folder else []
    for i, part in enumerate(parts):
        crumbs.append((part, nt.folder_url("/".join(parts[: i + 1]))))

    actions = [{"text": a["text"], "done": a["done"], "note_url": n["url"], "note_title": n["title"], "day": n["day"]}
               for n in notes for a in n["actions"]]

    selected = None
    if query.get("note"):
        path = nt.inside(root, query["note"])
        if path and path.suffix == ".md" and path.is_file():
            selected = read_note(root, path)
    transcript = None
    if look == "reader" and selected:
        pid = nt.plaud_id(selected["text"])
        transcript = plaud.transcript(pid) if pid and plaud_state["state"] == "signed_in" else None

    try:
        week = max(-52, min(52, int(query.get("week", "0"))))
    except ValueError:
        week = 0

    return {
        "look": look, "flash": flash, "here": here, "folder": folder, "week": week, "rec": query.get("rec", ""),
        "provider": PROVIDER_LABELS.get(cfg["backend"], cfg["backend"]), "model": model_label(cfg),
        "sync_every": SYNC_EVERY_MIN, "sync_days": SYNC_DAYS, "list_days": LIST_DAYS,
        "last_sync": jobs.last_sync, "next_sync": jobs.next_sync.strftime("%-I:%M %p") if jobs.next_sync else None,
        "busy": bool(jobs.current) or not jobs.queue.empty(), "current": jobs.current, "log": list(jobs.log)[-200:],
        "password": bool(PASSWORD), "plaud": plaud_state,
        "recordings": recordings, "recordings_error": recordings_error,
        "folders": folders, "folder_counts": folder_counts, "subfolders": subfolders, "breadcrumbs": crumbs,
        "notes_here": [n for n in notes if n["folder"] == folder], "recent_notes": notes,
        "actions": actions, "counts": {"notes": len(notes)},
        "tasks": TASK_LABELS, "default_task": DEFAULT_TASK, "note": selected, "transcript": transcript,
    }


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "OpenPlaud"

    def log_message(self, fmt, *args):
        pass

    def _cookie(self, name: str) -> str | None:
        for part in self.headers.get("Cookie", "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return None

    def _look(self) -> str:
        chosen = self._cookie(LOOK_COOKIE)
        if chosen in ui.LOOKS:
            return chosen
        return DEFAULT_LOOK if DEFAULT_LOOK in ui.LOOKS else "inbox"

    def _send(self, status: int, body: str | bytes, ctype: str = "text/html; charset=utf-8", headers: dict | None = None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        # Not no-referrer: under it browsers send "Origin: null" on every form post, same-site included.
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                         "font-src https://fonts.gstatic.com; script-src 'unsafe-inline'; form-action 'self'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to: str = "/", flash: str | None = None, headers: dict | None = None):
        if flash:
            to, hash_, fragment = to.partition("#")
            to += ("&" if "?" in to else "?") + urllib.parse.urlencode({"msg": flash}) + hash_ + fragment
        self.send_response(303)
        self.send_header("Location", to)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authed(self) -> bool:
        return not PASSWORD or valid_session(self._cookie(COOKIE) or "")

    def _form(self) -> dict:
        length = min(int(self.headers.get("Content-Length") or 0), 16384)
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}

    def _same_origin(self) -> bool:
        site = self.headers.get("Sec-Fetch-Site")
        if site:
            return site in ("same-origin", "none")
        origin = self.headers.get("Origin")
        if not origin:
            return True
        # Behind Tailscale Serve the Host header can be the backend's; the public name is forwarded.
        return urllib.parse.urlsplit(origin).netloc in {self.headers.get("Host"), self.headers.get("X-Forwarded-Host")}

    def _bare_ctx(self) -> dict:
        return {"look": self._look(), "flash": None, "here": "/", "folder": "", "folders": []}

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if not self._authed():
            return self._send(200 if path == "/login" else 401, ui.login_page(self._bare_ctx()))
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        here = url.path + ("?" + urllib.parse.urlencode({k: v for k, v in query.items() if k != "msg"}) if any(k != "msg" for k in query) else "")
        if path == "/":
            ctx = build_context(self._look(), query, query.get("msg"), here)
            return self._send(200, ui.dashboard(ctx))
        if path == "/api/status":
            body = {"busy": bool(jobs.current) or not jobs.queue.empty(), "current": jobs.current,
                    "log": list(jobs.log)[-80:], "signin": signin.state["status"]}
            return self._send(200, json.dumps(body), "application/json")
        if path.startswith("/notes/"):
            return self._note(urllib.parse.unquote(path.removeprefix("/notes/")), "raw" in query, query.get("msg"), here)
        self._send(404, ui.page(self._bare_ctx(), "Not found", '<p style="padding:20px">Not found. <a href="/">Back</a></p>'))

    def _note(self, rel: str, raw: bool, flash: str | None, here: str):
        cfg = pa.load_config()
        root = pa.notes_dir(cfg)
        target = nt.inside(root, rel)
        if not target or target.suffix != ".md" or not target.is_file():
            return self._send(404, ui.page(self._bare_ctx(), "Not found", '<p style="padding:20px">No such note. <a href="/#notes">Back</a></p>'))
        if raw:
            return self._send(200, target.read_text(), "text/markdown; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="{target.name}"'})
        look = self._look()
        note = read_note(root, target)
        ctx = {"look": look, "flash": flash, "here": here, "folder": note["folder"], "folders": nt.all_folders(root)}
        with_transcript = look == "reader"
        transcript = None
        if with_transcript and nt.plaud_id(note["text"]):
            transcript = plaud.transcript(nt.plaud_id(note["text"]))
        self._send(200, ui.note_page(ctx, note, transcript, with_transcript))

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        if not self._same_origin():
            return self._send(403, "Cross-site request refused.", "text/plain")
        form = self._form()
        back = form.get("back", "/")
        back = back if back.startswith("/") and not back.startswith("//") else "/"
        if path == "/login":
            if PASSWORD and hmac.compare_digest(form.get("password", "").encode(), PASSWORD.encode()):
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                cookie = f"{COOKIE}={make_session()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS * 86400}{secure}"
                return self._redirect("/", headers={"Set-Cookie": cookie})
            time.sleep(1)
            return self._send(401, ui.login_page(self._bare_ctx(), "Wrong password."))
        if not self._authed():
            return self._send(401, ui.login_page(self._bare_ctx()))
        if path == "/logout":
            return self._redirect("/", headers={"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0"})
        if path == "/look":
            look = form.get("look", "")
            if look not in ui.LOOKS:
                return self._redirect(back, "Unknown look.")
            cookie = f"{LOOK_COOKIE}={look}; Path=/; SameSite=Strict; Max-Age={365 * 86400}"
            return self._redirect(back, headers={"Set-Cookie": cookie})
        if path == "/plaud/signin":
            signin.start()
            return self._redirect("/", signin.state.get("message") if signin.state["status"] == "failed" else None)
        if path == "/plaud/callback":
            signin.complete(form.get("url", ""))
            ok = signin.state["status"] == "success"
            return self._redirect("/", "Signed in to Plaud." if ok else signin.state.get("message") or "Sign-in didn't finish.")
        if path == "/plaud/signout":
            with plaud.lock:
                try:
                    plaud.get().call("logout")
                except pa.PlaudError:
                    pass
            plaud.forget()
            signin.state = {"status": "idle", "url": None, "message": None}
            return self._redirect("/", "Signed out of Plaud.")
        if path == "/folders":
            root = pa.notes_dir(pa.load_config())
            parent, name = nt.inside(root, form.get("parent", "")), form.get("name", "").strip()
            if not parent or not nt.FOLDER_NAME.fullmatch(name):
                return self._redirect(nt.folder_url(form.get("parent", "")), "Folder names use letters, numbers, spaces, dots, dashes and underscores.")
            (parent / name).mkdir(parents=True, exist_ok=True)
            return self._redirect(nt.folder_url(nt.rel_of(root, parent / name)), f"Folder {name} is ready.")
        if path == "/notes/move":
            root = pa.notes_dir(pa.load_config())
            note, dest = nt.inside(root, form.get("note", "")), nt.inside(root, form.get("dest", ""))
            if not note or note.suffix != ".md" or not note.is_file() or not dest or not dest.is_dir():
                return self._redirect(back, "That note or folder doesn't exist.")
            moved = nt.move_note(root, note, dest)
            where = nt.rel_of(root, dest) if dest != root else "the top level"
            if back.startswith("/notes/"):
                back = nt.note_url(root, moved)
            elif "note=" in back:
                back = "/?" + urllib.parse.urlencode({"note": nt.rel_of(root, moved)})
            return self._redirect(back, f"Moved to {where} as {moved.name}.")
        if path == "/sync":
            added = jobs.add(("sync",))
            return self._redirect(back, "Sync queued." if added else "A sync is already queued.")
        if path == "/process":
            file_id, task = form.get("id", ""), form.get("task", DEFAULT_TASK)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", file_id) or task not in pa.TASKS:
                return self._redirect(back, "Unknown recording or task.")
            jobs.add(("process", file_id, task))
            return self._redirect(back, "Queued.")
        if path == "/test":
            cfg = pa.load_config()
            try:
                result = pa.make_backend(cfg, cfg["backend"]).check()
            except Exception as err:
                result = f"{PROVIDER_LABELS.get(cfg['backend'])}: {err}"
            return self._redirect(back, result)
        self._send(404, "Not found", "text/plain")


def main() -> None:
    cfg = pa.load_config()
    if cfg["backend"] not in pa.PROVIDERS:
        raise SystemExit(f"OPENPLAUD_PROVIDER must be one of {', '.join(pa.PROVIDERS)}")
    if DEFAULT_TASK not in pa.TASKS:
        raise SystemExit(f"OPENPLAUD_TASK must be one of {', '.join(pa.TASKS)}")
    if DEFAULT_LOOK not in ui.LOOKS:
        raise SystemExit(f"OPENPLAUD_LOOK must be one of {', '.join(ui.LOOKS)}")
    pa.notes_dir(cfg).mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"OpenPlaud listening on :{PORT} ({model_label(cfg)}, look: {DEFAULT_LOOK})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
