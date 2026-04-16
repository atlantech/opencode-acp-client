# opencode-acp-client

JSON-RPC over stdio client for [opencode](https://github.com/opencode-ai/opencode), enabling agent-driven coding sessions with multi-turn support, auto-approve, and permission handling.

## Features

- **Multi-turn conversations** — reuse sessions via `--session-id`, powered by opencode's `session/load`
- **Auto-approve policies** — `all`, `safe` (reads only), or `none`
- **Permission/question handling** — surface tool approval requests and agent questions via files
- **Streaming response chunks** — agent thoughts, messages, tool calls, and usage stats
- **Session tracking** — files touched, tools used, token consumption

## Requirements

- Python 3.10+
- [opencode](https://github.com/opencode-ai/opencode) installed at `~/.opencode/bin/opencode`

## Usage

### Single turn

```bash
python3 acp_run.py /path/to/repo --task "implement user authentication"
```

### Multi-turn

```bash
# Turn 1 — creates a session
python3 acp_run.py /path/to/repo --task "read the auth module"

# Turn 2 — reuses session, continues with full context
python3 acp_run.py /path/to/repo --session-id ses_abc --task "now refactor it to use arrow functions"
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--task` | *(required)* | Task description for the agent |
| `--session-id` | — | Reuse an existing session |
| `--mode` | `build` | Session mode |
| `--timeout` | `0` | Timeout in seconds (0 = unlimited) |
| `--approve` | `all` | Auto-approve policy: `all`, `safe`, `none` |
| `--question-file` | `/tmp/acp-question.json` | Path for agent question output |
| `--answer-file` | `/tmp/acp-answer.json` | Path for agent question input |
| `--raw-log` | `/tmp/acp-raw.jsonl` | Raw JSON-RPC log |

## Output

Returns JSON on stdout:

```json
{
  "status": "done",
  "sessionId": "ses_abc",
  "filesTouched": ["src/auth.ts"],
  "toolsUsed": ["read", "write", "edit"],
  "text": "...",
  "tokensUsed": 1234,
  "cost": "0.01"
}
```

Status values: `done`, `error`, `question`, `incomplete`.

## How It Works

1. Spawns `opencode` as a subprocess (JSON-RPC over stdio)
2. Sends `initialize` → `session/load` (or `session/new`) → `session/prompt`
3. Streams response chunks (thoughts, messages, tool calls)
4. Auto-handles tool permissions based on `--approve` policy
5. Collects results and returns structured JSON

## License

MIT
