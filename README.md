# claudex

This is a local MCP bridge for Claude Code and Codex Desktop chats.

## Setup

Use Python 3.12 and the pinned dependencies in `requirements.txt`. The
environment can be disposed of and rebuilt with `uv pip sync --require-hashes`.
The exact versions in the live test were CPython 3.12.13, MCP SDK 1.30.0 and
Claude Agent SDK 0.2.187.

Open the destination in Codex Desktop and keep it open. In a dedicated Claude
Code test project, register this stdio server in `.mcp.json`. Replace the
paths and the destination UUID:

```json
{
  "mcpServers": {
    "thread_bridge": {
      "type": "stdio",
      "command": "/absolute/path/to/venv/bin/python",
      "args": [
        "/absolute/path/to/desktop_bridge.py",
        "--client", "claude",
        "--state", "/absolute/private/path/to/mail.sqlite",
        "--codex-target", "00000000-0000-0000-0000-000000000000"
      ]
    }
  }
}
```

Use the project MCP approval flow when opening the test folder if Claude
asks. Keep `CLAUDE_CODE_SESSION_ID` out of the MCP configuration. Claude Code
sets it itself. In a Claude Desktop Code session, `who_am_i` should report that
chat's exact Desktop ID and title. After a clear, resume or a runtime change,
reconnect if the old identifier no longer resolves. The same configuration
can name any one open local Codex chat. It is intentionally pointed at one
recipient for the live test.

To register from Codex use `--client codex` and the same absolute state path.
The MCP client must supply the calling thread ID in request metadata.
Check `who_am_i` before sending. The prototype sends a fixed inbox notice
that names the source Codex chat and exact ID. When the sender has no
saved title, the notice shows the ID. Claude supplies its own outer peer
heading. The recipient calls `inbox` to read the message body.

## What the tools do

- `launch_claude` opens the signed-in Claude Desktop app through the
  account-preserving launcher. Set `THREAD_BRIDGE_CLAUDE_LAUNCHER` to the absolute path
  of your launcher if it lives outside the default location.
- `wake_claude` asks the running app to reopen a saved Desktop ID and checks
  for a live inbox. It reports `waiting_for_inbox` when the app has not
  started the worker.
- `open_claude_draft` opens a native Code draft for the operator to submit.
  Claude asks for confirmation before adopting a folder supplied in a link.
- `list_peers` lists running Claude Code Desktop chats, or the configured
  Codex destination and whether its owner is reachable.
- `list_codex_chats` lists saved local Codex Desktop chats by title or exact
  ID. It paginates and can list archived chats separately. A saved chat
  can appear here even when its Desktop owner cannot receive a message.
- `send_peer` sends to the exact configured Codex chat or to a running Claude
  Desktop Code chat from Codex. If the saved chat has no live inbox, the
  bridge asks Desktop to open its exact ID and waits for the worker. Without
  a ready inbox, the peer message stays queued. A higher-priority user prompt or approval
  never comes from the peer body. Codex receives namespaced tool output.
  Claude receives a fixed message-ID notice under its native inbound policy.
  It can hold or refuse the notice. The message stays in the inbox.
- `read_thread` takes a native Claude Desktop ID or a Codex UUID. It uses
  the Claude Agent SDK for visible text from live Claude chats. For Codex
  it reads the installed Desktop's local SQLite history projection. Codex
  history paginates and returns user text, assistant text, and attributed
  bridge messages. Legacy rollout-only history and remote-host history
  are outside the projection's coverage. These tools read the local OS
  user's Codex data. They do not resolve ownership of historical chats
  against a currently signed-in Codex account.
- `list_skills` and `read_skill` give both clients read-only access to user
  skill manifests. Scopes are `claude-user`, `codex-user` and `agents-user`.
  This is local `SKILL.md` text, with a 64 KiB cap and no symlinked folders,
  script, command substitution, activation or installed plugin scanning.

Both clients trust the operator and programs running as the same local
user. The bridge does not use per-chat token enrollment. It resolves Claude's
runtime identity using the machine ID recorded by the installed engine.
The Codex private renderer transport requires the exact requested owner to
advertise untrusted input support. It does not reuse the caller identity of a
different Codex chat. The sender name inside the tool result is data, and its
permissions come from the recipient's app.

The inbox returns up to 20 records per call for bounded model context.
Claude history shows up to 20 entries with 4,000 characters of visible text
per entry. Message bodies are capped at 16,000 characters to discourage
sending whole transcripts. IPC waits up to 12 seconds for discovery and the
owner's acknowledgement, with a 4 MiB frame limit. This bounds waits and
allocation on a private, version-specific interface. The Claude inbox socket
uses a short timeout for the local write. Check the target after an uncertain
submission. The bridge does not replay messages behind the caller's back.

## Creation and runtime compatibility

Claude Desktop's in-app session tool has both a suggestion path and a
direct-start path, subject to Desktop's own checks. The documented
`claude://code/new` link opens a native draft. The installed app also
accepts `claude://code/continue?session=<local-id>`. Anthropic's public
deep-link page does not list the latter route.

The bundled Codex app-tools MCP server needs an Electron-supplied pipe and
executor turn metadata. Loading it separately from Claude does not provide
those. The transport here uses the loaded owner's private local renderer
connection, checked against ChatGPT Desktop 26.1103.60933 and Codex engine
0.167.0-alpha.3.2. Open Cross-session demonstrates the same ownership boundary
but can use a different open Codex chat as a visual carrier. The MVP sends
directly to the selected owner with its own tool name and no prompt or CLI
fallback. MCP Agent Mail could replace the inbox later. Its pull model
does not deliver into a native Codex turn.

[App Server tool output](https://learn.chatgpt.com/docs/app-server#start-a-turn) ·
[Open Cross-session transport](https://raw.githubusercontent.com/leeguooooo/open-cross-session/main/DESIGN.md) ·
[Claude MCP session identity](https://code.claude.com/docs/en/env-vars) ·
[Claude Desktop drafts](https://support.claude.com/en/articles/14729294-open-claude-desktop-with-a-link)

## Verification

Run `python -B -m unittest discover -v` and `node --test native-reconnect/adapter.test.cjs`.
