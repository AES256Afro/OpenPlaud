"""Notes on disk: folders, safe paths, and what the page reads out of each note.

Nothing here deletes or overwrites a note: moves land on a free name (see plaud_ai.unique_path).
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

import plaud_ai as pa

FOLDER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,59}")
PATH_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,199}")
ACTION_LINE = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s+(.*)$")
PLAUD_ID_LINE = re.compile(r"Plaud file ID:\s*`([A-Za-z0-9_-]+)`")


def inside(root: Path, rel: str) -> Path | None:
    """The path `rel` names under `root`, or None when it is malformed or would leave it."""
    rel = (rel or "").strip("/")
    if rel and not all(PATH_PART.fullmatch(part) for part in rel.split("/")):
        return None
    path = (root / rel).resolve()
    return path if path == root or root in path.parents else None


def rel_of(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def note_url(root: Path, path: Path) -> str:
    return "/notes/" + urllib.parse.quote(rel_of(root, path))


def folder_url(rel: str, **extra: str) -> str:
    params = {k: v for k, v in {"folder": rel, **extra}.items() if v}
    return ("/?" + urllib.parse.urlencode(params) if params else "/") + "#notes"


def all_folders(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(rel_of(root, p) for p in root.rglob("*")
                  if p.is_dir() and not any(s.startswith(".") for s in p.relative_to(root).parts))


def all_notes(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted((p for p in root.rglob("*.md") if not any(s.startswith(".") for s in p.relative_to(root).parts)),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def note_for(root: Path, entry: dict | None) -> Path | None:
    """Where a processed recording's note is now, following it if it was moved."""
    if not entry or not entry.get("file"):
        return None
    path = Path(entry["file"]).resolve()
    if path.exists() and root in path.parents:
        return path
    return next(iter(sorted(root.rglob(path.name))), None)


def move_note(root: Path, note: Path, folder: Path) -> Path:
    target = pa.unique_path(folder / note.name)
    note.rename(target)
    state = pa.load_state()
    for entry in state["processed"].values():
        if entry.get("file") and Path(entry["file"]).resolve() == note.resolve():
            entry["file"] = str(target)
    pa.save_state(state)
    return target


def note_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def note_summary(text: str, limit: int = 260) -> str:
    """The first paragraph under "Summary", else the first prose paragraph after the metadata."""
    lines = text.splitlines()
    start = next((i + 1 for i, line in enumerate(lines) if re.match(r"^#{1,3}\s+Summary\b", line)), None)
    candidates = lines[start:] if start is not None else lines
    para: list[str] = []
    for line in candidates:
        stripped = line.strip()
        if not stripped:
            if para:
                break
            continue
        if stripped.startswith(("#", "- ", "* ", "|", "```")):
            if para:
                break
            continue
        para.append(stripped)
    summary = re.sub(r"\*\*|`", "", " ".join(para))
    return summary if len(summary) <= limit else summary[:limit].rsplit(" ", 1)[0] + "…"


def note_actions(text: str) -> list[dict]:
    return [{"done": m.group(1).lower() == "x", "text": re.sub(r"\*\*|`", "", m.group(2)).strip()}
            for m in map(ACTION_LINE.match, text.splitlines()) if m]


def plaud_id(text: str) -> str | None:
    m = PLAUD_ID_LINE.search(text)
    return m.group(1) if m else None
