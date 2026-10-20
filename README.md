# OpenPlaud

Run your Plaud recordings through **your** model: Claude, ChatGPT, or a local model such as Qwen on
Ollama. OpenPlaud pulls each recording's transcript from your Plaud account and writes Markdown notes:
meeting notes, action items, or a follow-up email.

The Plaud recorder and app can't be pointed at a custom model, so OpenPlaud works on the transcript after
Plaud syncs it. It reads Plaud through Plaud's official MCP server (`@plaud-ai/mcp`).

There are two ways to run it: as a web app (a container, installable from the BoxPilot App Catalog) or as
a command-line script.

## Web app (Docker / BoxPilot)

```bash
docker run -d -p 8795:8795 -v openplaud:/data \
  --add-host host.docker.internal:host-gateway \
  -e OPENPLAUD_PASSWORD=choose-one -e ANTHROPIC_API_KEY=sk-ant-... \
  ghcr.io/aes256afro/openplaud:0.1.0
```

Open `http://<server>:8795`, sign in, then **Start Plaud sign-in**. Plaud's page sends your browser to a
`localhost:8199` address that doesn't load. Paste that address into OpenPlaud to finish signing in.
After that, recordings sync on a schedule, and each one can also be run by hand.

### Settings

| Variable | What it does | Default |
| --- | --- | --- |
| `OPENPLAUD_PROVIDER` | `claude`, `chatgpt` or `local` | `claude` |
| `ANTHROPIC_API_KEY` | Claude API key | |
| `CLAUDE_MODEL` | Claude model | `claude-haiku-5-5` |
| `OPENAI_API_KEY` | OpenAI API key, for ChatGPT | |
| `OPENAI_MODEL` | OpenAI model | `gpt-5-mini` |
| `OLLAMA_URL` | Ollama address for local models | `http://localhost:11434` |
| `LOCAL_MODEL` | Ollama model name | `qwen3:30b-a3b` |
| `OPENPLAUD_TASK` | What auto-sync writes: `summary`, `actions` or `followup` | `summary` |
| `OPENPLAUD_SYNC_EVERY` | Minutes between automatic syncs; `0` turns it off | `60` |
| `OPENPLAUD_SYNC_DAYS` | How far back each sync looks, in days | `7` |
| `OPENPLAUD_PASSWORD` | Password for the web page; empty means no password | |

Notes, the Plaud sign-in and the record of processed recordings live in `/data`.

## Command line

```bash
npm install                    # installs the Plaud MCP server locally
uv run plaud_ai.py login       # opens a browser to sign in to Plaud
uv run plaud_ai.py check       # confirms Plaud and each provider are reachable
uv run plaud_ai.py list        # last 7 days; ✓ = already processed
uv run plaud_ai.py sync        # process every new recording from the last 7 days
uv run plaud_ai.py process <file_id> --task actions --print
uv run plaud_ai.py --backend local process <file_id> --prompt "Write a SOAP note from this visit"
```

Provider settings for the command line live in `plaud_ai.toml`; the environment variables above override
it. API keys can also go in a `.env` file next to the script (git-ignored).

Long recordings that exceed a local model's context are summarized in parts, then combined.
