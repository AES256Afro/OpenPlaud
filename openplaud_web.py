"""OpenPlaud web app: sign in to Plaud, see recordings, summarize them, read and organize the notes,
and (optionally) send each recording to Claude as its own session.

Settings come from the environment (BoxPilot's app settings): see README.md for the list. The page is
drawn by openplaud_ui in one of ten looks; OPENPLAUD_LOOK sets the default and each browser can pick its own.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import claude_routine as cr
import openplaud_core as core
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
CLAUDE_URL = os.environ.get("CLAUDE_ROUTINE_URL", "")
CLAUDE_TOKEN = os.environ.get("CLAUDE_ROUTINE_TOKEN", "")
AUTH_URL_FILE = Path(os.environ.get("OPENPLAUD_AUTH_URL_FILE", "/tmp/openplaud-auth-url"))
COOKIE = "openplaud_session"
LOOK_COOKIE = "openplaud_look"
SESSION_DAYS = 30
PROVIDER_LABELS = {"claude": "Claude", "chatgpt": "ChatGPT", "local": "Local model"}
TASK_LABELS = {"summary": "Meeting notes", "actions": "Action items", "followup": "Follow-up email"}

plaud = core.PlaudSession(LIST_DAYS)
signin = core.PlaudSignIn(plaud, AUTH_URL_FILE)
passwords = core.Passwords(PASSWORD, b"openplaud-session:", SESSION_DAYS)


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def claude_on() -> bool:
    return cr.configured(CLAUDE_URL, CLAUDE_TOKEN)


# ---------------------------------------------------------------- sending to Claude

def claude_state(state: dict) -> dict:
    c = state.setdefault("claude", {})
    c.setdefault("sent", {})
    c.setdefault("failed", {})
    c.setdefault("fires", [])
    return c


def send_to_claude(rec: dict, note_path: Path, transcript: str, log, manual: bool = False) -> None:
    """Fire the scheduled task with this recording's note and transcript. Automatic sends cover only
    recordings made after sending was switched on, each once; a manual send always goes."""
    if not claude_on():
        return
    rid = pa.rec_id(rec)
    state = pa.load_state()
    c = claude_state(state)
    if not manual:
        since = datetime.fromisoformat(c["since"]) if c.get("since") else None
        when = pa.recorded_at(rec)
        if rid in c["sent"] or (since and when and when < since):
            return
    budget = cr.FireBudget([float(s) for s in c["fires"]])
    if not budget.available():
        c["failed"][rid] = {"error": "Waiting: Claude's hourly limit for this task is nearly used. Sent on a later sync.",
                            "retryable": True, "at": datetime.now().isoformat()}
        pa.save_state(state)
        log(f"… Claude: hourly limit near, {rec.get('name', rid)} waits for the next sync")
        return
    text = cr.build_payload(rec.get("name") or rid, pa.recorded_at(rec).strftime("%Y-%m-%d %H:%M") if pa.recorded_at(rec) else "?",
                            pa.duration(rec), rid, [("Notes written by OpenPlaud", note_path.read_text())], transcript)
    budget.spend()
    c["fires"] = budget.stamps
    try:
        result = cr.fire(CLAUDE_URL, CLAUDE_TOKEN, text)
    except cr.RoutineError as err:
        c["failed"][rid] = {"error": str(err), "retryable": err.retryable, "at": datetime.now().isoformat()}
        pa.save_state(state)
        log(f"✗ Claude: {rec.get('name', rid)}: {err}")
        return
    c["sent"][rid] = {"session_url": result["session_url"], "at": datetime.now().isoformat(), "note": str(note_path)}
    c["failed"].pop(rid, None)
    pa.save_state(state)
    log(f"→ Claude: {rec.get('name', rid)} · {result['session_url']}")


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
        return {job[1] for job in self.pending if job[0] in ("process", "claude")}

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
            self.current = {"sync": "Syncing new recordings", "process": "Summarizing a recording", "claude": "Sending to Claude"}[job[0]]
            self.active_id = job[1] if job[0] != "sync" else None
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

    def _on_done(self, manual: bool = False):
        return lambda rec, path, transcript: send_to_claude(rec, path, transcript, self.say, manual=manual)

    def _run(self, job: tuple) -> None:
        cfg = pa.load_config()
        backend = pa.make_backend(cfg, cfg["backend"])
        out_dir = pa.notes_dir(cfg)
        with plaud.lock:
            p = plaud.get()
            if job[0] == "sync":
                self.say(f"Sync started (last {SYNC_DAYS} days, {backend.name})")
                written, failures = pa.sync(p, backend, SYNC_DAYS, pa.TASKS[DEFAULT_TASK], DEFAULT_TASK, out_dir, log=self.say,
                                            on_start=lambda rec: setattr(self, "active_id", pa.rec_id(rec)), on_done=self._on_done())
                self.last_sync = datetime.now().strftime("%-d %b %H:%M")
                self.say(f"Sync finished: {len(written)} new, {failures} failed")
                self._retry_claude(p, out_dir)
            elif job[0] == "process":
                _, file_id, task = job
                rec = pa.unwrap_file(p.call_json("get_file", file_id=file_id))
                pa.process_and_record(p, backend, rec, pa.TASKS[task], task, out_dir, log=self.say, on_done=self._on_done())
            else:
                _, file_id = job
                rec = pa.unwrap_file(p.call_json("get_file", file_id=file_id))
                note = nt.note_for(out_dir, pa.load_state()["processed"].get(file_id))
                if note:
                    send_to_claude(rec, note, pa.fetch_transcript(p, file_id), self.say, manual=True)
                else:
                    pa.process_and_record(p, backend, rec, pa.TASKS[DEFAULT_TASK], DEFAULT_TASK, out_dir, log=self.say,
                                          on_done=self._on_done(manual=True))

    def _retry_claude(self, p: pa.Plaud, out_dir: Path) -> None:
        """Send again what Claude refused with a rate limit or overload; errors that need the owner wait for them."""
        if not claude_on():
            return
        state = pa.load_state()
        for rid, failure in list(claude_state(state)["failed"].items()):
            if not failure.get("retryable"):
                continue
            note = nt.note_for(out_dir, state["processed"].get(rid))
            if not note:
                continue
            rec = pa.unwrap_file(p.call_json("get_file", file_id=rid))
            send_to_claude(rec, note, pa.fetch_transcript(p, rid), self.say, manual=True)


jobs = Jobs()


# ---------------------------------------------------------------- what a page shows

def transcript_for(file_id: str | None, may_ask_plaud: bool) -> list[str] | None:
    """A recording's transcript with the owner's speaker names: the stored copy, else Plaud (and keep a copy)."""
    if not file_id:
        return None
    text = pa.load_transcript(file_id)
    if text is None and may_ask_plaud:
        lines = plaud.transcript(file_id)
        if lines:
            text = "\n".join(lines)
            pa.save_transcript(file_id, text)
    if text is None:
        return None
    return pa.apply_speakers(text, pa.load_state().get("speakers", {}).get(file_id)).splitlines()


def model_label(cfg: dict) -> str:
    provider = cfg["backend"]
    model = {"claude": cfg["claude"]["model"], "chatgpt": cfg["chatgpt"]["model"], "local": cfg["local"]["model"]}.get(provider, "?")
    return f"{PROVIDER_LABELS.get(provider, provider)} · {model}"


def read_note(root: Path, path: Path) -> dict:
    text = path.read_text()
    folder = nt.rel_of(root, path.parent) if path.parent != root else ""
    return {"rel": nt.rel_of(root, path), "url": nt.note_url(root, path), "folder": folder, "back": nt.folder_url(folder),
            "title": nt.note_title(text, path.stem), "html": ui.render_markdown(text), "text": text, "stem": path.stem}


FILTERS = {key: test for key, test in ui.FILTER_TESTS.items() if key}


def mark_read(note_text: str) -> None:
    rid = nt.plaud_id(note_text)
    if not rid:
        return
    state = pa.load_state()
    if rid not in state.setdefault("read", {}):
        state["read"][rid] = datetime.now().isoformat(timespec="seconds")
        pa.save_state(state)


def build_context(look: str, query: dict, flash: str | None, here: str) -> dict:
    cfg = pa.load_config()
    root = pa.notes_dir(cfg)
    root.mkdir(parents=True, exist_ok=True)
    here_dir = nt.inside(root, query.get("folder", ""))
    here_dir = here_dir if here_dir and here_dir.is_dir() else root
    folder = nt.rel_of(root, here_dir) if here_dir != root else ""
    plaud_state = signin.page_state()

    # Every note once: titles, summaries and action items for the looks that show them.
    notes = []
    for path in nt.all_notes(root):
        try:
            text = path.read_text()
        except OSError:
            continue
        notes.append({"path": path, "rel": nt.rel_of(root, path), "url": nt.note_url(root, path), "stem": path.stem,
                      "folder": nt.rel_of(root, path.parent) if path.parent != root else "", "title": nt.note_title(text, path.stem),
                      "summary": nt.note_summary(text), "actions": nt.note_actions(text, with_lines=True), "plaud_id": nt.plaud_id(text),
                      "day": datetime.fromtimestamp(path.stat().st_mtime).date()})
    by_path = {n["path"].resolve(): n for n in notes}

    state = pa.load_state()
    sent = claude_state(state)["sent"]
    failed = claude_state(state)["failed"]
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
                elif rid in state.get("failed", {}):
                    status = "failed"
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
                    "actions_open": sum(1 for a in note["actions"] if not a["done"]) if note else 0,
                    "claude_url": sent.get(rid, {}).get("session_url"), "claude_error": failed.get(rid, {}).get("error"),
                    "unread": bool(note) and rid not in state.get("read", {}),
                    "error": state.get("failed", {}).get(rid, {}).get("error")})
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

    actions = [{"text": a["text"], "done": a["done"], "line": a["line"], "note_rel": n["rel"], "note_url": n["url"],
                "note_title": n["title"], "day": n["day"]} for n in notes for a in n["actions"]]

    show = query.get("show", "")
    all_recordings = recordings
    if recordings is not None and show in FILTERS:
        recordings = [r for r in recordings if FILTERS[show](r)]
    search = None
    if query.get("q", "").strip():
        titles = {r["id"]: r["name"] for r in all_recordings or []}
        search = nt.search(root, query["q"], titles)
        for hit in search:
            if hit["kind"] == "transcript":
                path = nt.note_for(root, state["processed"].get(hit["id"]))
                hit["url"] = nt.note_url(root, path) if path else None

    selected = None
    if query.get("note"):
        path = nt.inside(root, query["note"])
        if path and path.suffix == ".md" and path.is_file():
            selected = read_note(root, path)
            mark_read(selected["text"])
    transcript = None
    if look == "reader" and selected:
        transcript = transcript_for(nt.plaud_id(selected["text"]), plaud_state["state"] == "signed_in")

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
        "password": bool(PASSWORD), "plaud": plaud_state, "claude_on": claude_on(),
        "recordings": recordings, "recordings_error": recordings_error,
        "folders": folders, "folder_counts": folder_counts, "subfolders": subfolders, "breadcrumbs": crumbs,
        "notes_here": [n for n in notes if n["folder"] == folder], "recent_notes": notes,
        "actions": actions, "counts": {"notes": len(notes)},
        "tasks": TASK_LABELS, "default_task": DEFAULT_TASK, "note": selected, "transcript": transcript,
        "show": show if show in FILTERS else "", "q": query.get("q", ""), "search": search, "undo": query.get("undo", ""),
        "all_recordings": all_recordings, "moves": nt.move_history(root, 10),
    }


def with_undo(url: str, move_id: str) -> str:
    base, hash_, fragment = url.partition("#")
    return base + ("&" if "?" in base else "?") + urllib.parse.urlencode({"undo": move_id}) + hash_ + fragment


def claude_context(look: str, flash: str | None) -> dict:
    state = pa.load_state()
    c = claude_state(state)
    return {"look": look, "flash": flash, "here": "/claude", "folder": "", "folders": [], "claude_on": claude_on(),
            "since": c.get("since"), "sent": len(c["sent"]), "failed": c["failed"],
            "fires_hour": len([s for s in c["fires"] if float(s) > time.time() - 3600]), "prompt": cr.SUGGESTED_PROMPT,
            "url_ok": bool(CLAUDE_URL) and cr.configured(CLAUDE_URL, "x"), "token_set": bool(CLAUDE_TOKEN.strip())}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "OpenPlaud"

    def log_message(self, fmt, *args):
        pass

    def _look(self) -> str:
        chosen = core.cookie(self.headers, LOOK_COOKIE)
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
        return passwords.valid(core.cookie(self.headers, COOKIE) or "")

    def _form(self) -> dict:
        """The posted form; a field sent more than once (selected notes) keeps every value under "<name>[]"."""
        length = min(int(self.headers.get("Content-Length") or 0), 65536)
        fields = urllib.parse.parse_qs(self.rfile.read(length).decode())
        form = {k: v[0] for k, v in fields.items()}
        form.update({f"{k}[]": v for k, v in fields.items()})
        return form

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
        kept = {k: v for k, v in query.items() if k not in ("msg", "undo")}
        here = url.path + ("?" + urllib.parse.urlencode(kept) if kept else "")
        if path == "/":
            return self._send(200, ui.dashboard(build_context(self._look(), query, query.get("msg"), here)))
        if path == "/claude":
            return self._send(200, ui.claude_page(claude_context(self._look(), query.get("msg"))))
        if path == "/api/status":
            body = {"busy": bool(jobs.current) or not jobs.queue.empty(), "current": jobs.current,
                    "log": list(jobs.log)[-80:], "signin": signin.state["status"]}
            return self._send(200, json.dumps(body), "application/json")
        if path.startswith("/notes/"):
            return self._note(urllib.parse.unquote(path.removeprefix("/notes/")), "raw" in query, query.get("msg"), here)
        self._send(404, ui.page(self._bare_ctx(), "Not found", '<p style="padding:20px">Not found. <a href="/">Back</a></p>'))

    def _note(self, rel: str, raw: bool, flash: str | None, here: str):
        root = pa.notes_dir(pa.load_config())
        target = nt.inside(root, rel)
        if not target or target.suffix != ".md" or not target.is_file():
            return self._send(404, ui.page(self._bare_ctx(), "Not found", '<p style="padding:20px">No such note. <a href="/#notes">Back</a></p>'))
        if raw:
            return self._send(200, target.read_text(), "text/markdown; charset=utf-8",
                              {"Content-Disposition": f'attachment; filename="{target.name}"'})
        look = self._look()
        note = read_note(root, target)
        mark_read(note["text"])
        ctx = {"look": look, "flash": flash, "here": here, "folder": note["folder"], "folders": nt.all_folders(root)}
        with_transcript = look == "reader"
        transcript = transcript_for(nt.plaud_id(note["text"]), True) if with_transcript else None
        self._send(200, ui.note_page(ctx, note, transcript, with_transcript))

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        if not core.same_origin(self.headers):
            return self._send(403, "Cross-site request refused.", "text/plain")
        form = self._form()
        back = core.safe_back(form.get("back", "/"))
        if path == "/login":
            if passwords.check(form.get("password", "")):
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                cookie = f"{COOKIE}={passwords.make()}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_DAYS * 86400}{secure}"
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
            return self._redirect(back, headers={"Set-Cookie": f"{LOOK_COOKIE}={look}; Path=/; SameSite=Strict; Max-Age={365 * 86400}"})
        if path == "/plaud/signin":
            signin.start()
            return self._redirect("/", signin.state.get("message") if signin.state["status"] == "failed" else None)
        if path == "/plaud/callback":
            signin.complete(form.get("url", ""))
            ok = signin.state["status"] == "success"
            return self._redirect("/", "Signed in to Plaud." if ok else signin.state.get("message") or "Sign-in didn't finish.")
        if path == "/plaud/signout":
            signin.sign_out()
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
            moved, move_id = nt.move_note(root, note, dest)
            where = nt.rel_of(root, dest) if dest != root else "the top level"
            if back.startswith("/notes/"):
                back = nt.note_url(root, moved)
            elif "note=" in back:
                back = "/?" + urllib.parse.urlencode({"note": nt.rel_of(root, moved)})
            return self._redirect(with_undo(back, move_id), f"Moved to {where} as {moved.name}.")
        if path == "/notes/move-many":
            root = pa.notes_dir(pa.load_config())
            dest = nt.inside(root, form.get("dest", ""))
            picked = [nt.inside(root, rel) for rel in form.get("notes[]", [])[:200]]
            picked = [n for n in picked if n and n.suffix == ".md" and n.is_file()]
            if not dest or not dest.is_dir() or not picked:
                return self._redirect(back, "Pick at least one note and a folder.")
            for note in picked:
                nt.move_note(root, note, dest)
            where = nt.rel_of(root, dest) if dest != root else "the top level"
            return self._redirect(back, f"Moved {len(picked)} notes to {where}. Recent moves can be undone below.")
        if path == "/notes/undo":
            root = pa.notes_dir(pa.load_config())
            restored = nt.undo_move(root, form.get("id", ""))
            if not restored:
                return self._redirect(back, "That move can't be undone: what it moved isn't where it was put any more.")
            return self._redirect(back, f"Put back as {nt.rel_of(root, restored)}.")
        if path in ("/folders/rename", "/folders/move"):
            root = pa.notes_dir(pa.load_config())
            folder = nt.inside(root, form.get("folder", ""))
            if not folder or folder == root or not folder.is_dir():
                return self._redirect(back, "That folder doesn't exist.")
            try:
                if path == "/folders/rename":
                    moved, move_id = nt.rename_folder(root, folder, form.get("name", "").strip())
                else:
                    dest = nt.inside(root, form.get("dest", ""))
                    if not dest or not dest.is_dir():
                        return self._redirect(back, "That folder doesn't exist.")
                    moved, move_id = nt.move_folder(root, folder, dest)
            except ValueError as err:
                return self._redirect(back, str(err))
            return self._redirect(with_undo(nt.folder_url(nt.rel_of(root, moved)), move_id), f"Folder is now {nt.rel_of(root, moved)}.")
        if path == "/actions/toggle":
            root = pa.notes_dir(pa.load_config())
            note = nt.inside(root, form.get("note", ""))
            try:
                line = int(form.get("line", "-1"))
            except ValueError:
                line = -1
            ok = bool(note and note.suffix == ".md" and note.is_file()) and nt.set_action(note, line, form.get("text", ""), form.get("done") == "1")
            return self._redirect(back, None if ok else "That note changed since the page loaded; reload and try again.")
        if path == "/sync":
            added = jobs.add(("sync",))
            return self._redirect(back, "Sync queued." if added else "A sync is already queued.")
        if path in ("/process", "/claude/send"):
            file_id, task = form.get("id", ""), form.get("task", DEFAULT_TASK)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", file_id) or task not in pa.TASKS:
                return self._redirect(back, "Unknown recording or task.")
            if path == "/claude/send":
                if not claude_on():
                    return self._redirect("/claude", "Set the Claude fire URL and token in BoxPilot first.")
                jobs.add(("claude", file_id))
                return self._redirect(back, "Sending to Claude.")
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
    if claude_on():
        state = pa.load_state()
        c = claude_state(state)
        if not c.get("since"):
            c["since"] = datetime.now().isoformat()
            pa.save_state(state)
    elif CLAUDE_URL or CLAUDE_TOKEN:
        print("Send to Claude is off: the fire URL or token is missing or malformed.", flush=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"OpenPlaud listening on :{PORT} ({model_label(cfg)}, look: {DEFAULT_LOOK}, "
          f"send to Claude: {'on' if claude_on() else 'off'})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
