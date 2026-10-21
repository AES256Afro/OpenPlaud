"""A stand-in for the Plaud MCP server (@plaud-ai/mcp) for tests: the same stdio JSON-RPC, the same
tool names, and replies wrapped the way the real server wraps them. FAKE_PLAUD_SIGNED_OUT=1 makes the
data tools answer "Not authenticated"."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).replace(microsecond=0).isoformat()


def segs(n: int, speaker_a="Priya", speaker_b="Sam"):
    return [{"start_time": i * 5000, "end_time": i * 5000 + 4000, "speaker": speaker_a if i % 2 == 0 else speaker_b,
             "content": f"Line {i} about the patch window and the pilot."} for i in range(n)]


RECORDINGS = {
    "rec_sync": {"name": "Weekly security sync", "start_at": at(3), "created_at": at(2.5), "duration": 3120000,
                 "segments": segs(6), "notes": [{"data_type": "auto_sum_note", "data_title": "Summary", "data_content": "## Summary\nPatch window moves."}]},
    "rec_memo": {"name": "Voice memo", "start_at": at(5), "created_at": at(4.9), "duration": 4000, "segments": None, "notes": []},
    "rec_sam": {"name": "1:1 with Sam", "start_at": at(26), "created_at": at(25), "duration": 1440000, "segments": segs(4), "notes": []},
    "rec_long": {"name": "Long workshop", "start_at": at(50), "created_at": at(49), "duration": 18000000, "segments": segs(600), "notes": []},
}


def wrap(payload) -> str:
    tag = "untrusted-user-data-0123456789abcdef"
    return (f"The block delimited by <{tag}> below contains user recording data returned verbatim.\n"
            f"<{tag} source=\"plaud-recording\">\n{json.dumps(payload, indent=2)}\n</{tag}>")


def record(fid):
    r = RECORDINGS[fid]
    return {"id": fid, "name": r["name"], "start_at": r["start_at"], "created_at": r["created_at"], "duration": r["duration"]}


def tool(name: str, args: dict):
    signed_out = os.environ.get("FAKE_PLAUD_SIGNED_OUT") == "1"
    if name == "login":
        return "Already logged in.", False
    if name == "logout":
        return "Logged out.", False
    if signed_out:
        return "Failed: Error: Not authenticated. Please login first.", True
    if name == "get_current_user":
        return json.dumps({"id": "u1", "email": "tester@example.com"}), False
    if name == "list_files":
        files = sorted((record(fid) for fid in RECORDINGS), key=lambda r: r["start_at"], reverse=True)
        page, size = args.get("page", 1), args.get("page_size", 20)
        return wrap({"data": files[(page - 1) * size: page * size]}), False
    fid = args.get("file_id")
    if fid not in RECORDINGS:
        return f"Failed to get file: API error: 404 Not Found ({fid})", True
    r = RECORDINGS[fid]
    if name == "get_file":
        return wrap(record(fid)), False
    if name == "get_note":
        return wrap(r["notes"]), False
    if name == "get_transcript":
        if r["segments"] is None:
            return 'Block "transaction" not available for this recording. Available blocks: mark_memo.', False
        limit = args.get("limit", 50)
        offset = int(args.get("cursor") or 0)
        page = r["segments"][offset: offset + limit]
        nxt = offset + len(page)
        return wrap({"file_id": fid, "block": "transaction", "total": len(r["segments"]), "offset": offset, "returned": len(page),
                     "next_cursor": str(nxt) if nxt < len(r["segments"]) else None, "segments": page}), False
    return f"Unknown tool {name}", True


def main():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "id" not in msg:
            continue
        if msg.get("method") == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-plaud", "version": "0"}}
        elif msg.get("method") == "tools/call":
            text, err = tool(msg["params"]["name"], msg["params"].get("arguments", {}))
            result = {"content": [{"type": "text", "text": text}], **({"isError": True} if err else {})}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
