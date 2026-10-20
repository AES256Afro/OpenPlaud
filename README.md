# Plaud → your own model

`plaud_ai.py` pulls recordings from your Plaud account and runs the transcripts through **your** model,
either the local LLM on `bigbox` over Tailscale or Claude, then writes Markdown notes to `notes/`.

The Plaud recorder and app can't be pointed at a custom model directly, so this works on the transcript
after Plaud syncs it. It reads Plaud through the official Plaud MCP server (installed in `node_modules/`),
so it uses the same Plaud sign-in as Claude Code's Plaud tools.

## First run

```bash
uv run plaud_ai.py login      # opens a browser to sign in to Plaud
uv run plaud_ai.py check      # confirms Plaud, bigbox, and Claude are reachable
```

## Everyday use

```bash
uv run plaud_ai.py list                         # last 7 days; ✓ = already processed
uv run plaud_ai.py sync                         # summarize every new recording from the last 7 days
uv run plaud_ai.py process <file_id> --task actions --print
uv run plaud_ai.py --backend local process <file_id> --task followup   # use bigbox instead
uv run plaud_ai.py process <file_id> --prompt "Write a SOAP note from this visit"
```

Tasks: `summary` (default), `actions`, `followup`, or any `--prompt`.

## Choosing the model

Edit `plaud_ai.toml`:

- `backend = "claude"` (the default, using Claude Haiku 5.5) or `"local"` (override with `--backend`).
- `[local]` is set to Ollama on `http://bigbox:11434` with `qwen3:30b-a3b`. Any Tailscale machine
  works. For LM Studio or another OpenAI-compatible server, set `kind = "openai"` and its `base_url`.
- `[claude]` needs `ANTHROPIC_API_KEY`, either exported in your shell or pasted into `.env` in this folder.

Long recordings that exceed `max_context` are summarized in parts and then combined.
