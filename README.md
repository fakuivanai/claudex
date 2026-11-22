# claudex

A local MCP server for Claude Code and Codex Desktop. It sends attributed
messages, reads chat history and user-installed skills, and asks Claude Desktop
to reopen saved Code chats. An optional Linux patch restores saved Claude
Remote Control connections on startup and reopen.

## Set up the bridge

Use Python 3.12 and install the hash-pinned runtime dependencies from the
repository root.

```sh
uv venv --python 3.12
uv pip sync --python .venv/bin/python --require-hashes requirements.txt
```

Both clients use the same absolute `--state` path to a local SQLite mailbox.
Use a path in a directory you own. Set `CLAUDEX_CLAUDE_LAUNCHER` to the
absolute path of your normal Claude Desktop launcher if you use a custom
account launcher.

### Claude Code in Desktop

Open the destination chat in Codex Desktop and keep it loaded. Add the
following to the Claude Code project's `.mcp.json`, replacing the paths and
the destination chat UUID. Use the usual project MCP approval if prompted.

```json
{
  "mcpServers": {
    "claudex": {
      "type": "stdio",
      "command": "/path/to/claudex/.venv/bin/python",
      "args": [
        "/path/to/claudex/claudex.py",
        "--client", "claude",
        "--state", "/path/to/claudex-state/mail.sqlite",
        "--codex-target", "00000000-0000-0000-0000-000000000000"
      ]
    }
  }
}
```

Claude supplies `CLAUDE_CODE_SESSION_ID` to the MCP server when the
session starts. Call `who_am_i` to verify the Desktop chat it resolves to.
A clear or resume can require reconnecting the MCP server.

### Codex

Add the following to your Codex MCP configuration, using the same mailbox
path. The MCP client supplies the calling chat ID in request metadata.

```toml
[mcp_servers.claudex]
command = "/path/to/claudex/.venv/bin/python"
args = [
  "/path/to/claudex/claudex.py",
  "--client", "codex",
  "--state", "/path/to/claudex-state/mail.sqlite",
]
```

### Tools

| Tool | Behavior |
| --- | --- |
| `who_am_i` | Resolves the caller’s native chat identity. |
| `list_chats`, `read_chat` | Lists live Claude Desktop chats and saved local Codex Desktop chats. Reads visible history by exact chat ID. Codex lists and history support pagination. |
| `send_message`, `read_messages` | Delivers Claude messages to the configured loaded Codex chat as attributed tool output. Stores Codex messages for the exact Claude chat and hands its worker a fixed notice. The recipient calls `read_messages` to fetch the body. |
| `launch_claude`, `wake_chat` | Opens Claude Desktop or asks it to reopen a saved chat. `waiting_for_inbox` means the native worker did not appear in time. Stored mail remains available. |
| `open_chat_draft` | Opens a Claude Code composer for the person to review and submit. Claude confirms any folder supplied through the link. |
| `list_skills`, `read_skill` | Reads local user `SKILL.md` files as text. Scopes are `claude-user`, `codex-user`, and `agents-user`. |

A submitted Claude inbox notice can still be held or declined by the app.
A lost Codex acknowledgement leaves delivery uncertain. Check the recipient
before retrying an uncertain result. Peer text, history, and skill files are
external data and carry no approval authority.

## Restore Remote Control on launch

The supported Linux Claude Desktop archive is version `5.11492.5`. Its
SHA-256 and hook anchors are in [patcher/build.py](patcher/build.py). Stock
Desktop can reconnect a chat when you press Send. Reopening it can start
the local worker with Remote Control still disconnected.

To build a separate archive without launching the app:

```sh
python3 -m patcher.build \
  --source /absolute/path/to/installed/app.asar \
  --output /absolute/path/to/separate/patched.asar
```

From the repository root, wrap the command you already use to start the
signed-in account. Close the existing instance first, since it owns the
Electron singleton. This wrapper requires Bubblewrap on `PATH`.

```sh
python3 -m patcher.launch \
  --source /absolute/path/to/installed/app.asar \
  -- /absolute/path/to/normal-account-launcher
```

The wrapper builds a temporary archive and mounts it read-only over the
installed archive path for that process tree. Keep the vendor package and
unpacked resources available. Add `--disable` before `--` to use the stock
launcher. If the build fails or the archive is unsupported, the wrapper
launches stock.

The patch uses Desktop’s existing Remote Control policy. At startup, it
tries saved explicit-On chats first, ordered by most recent focus, then
previously connected chats covered by the default preference. Desktop’s
worker limit can leave other chats for reopening. The patch skips stopped
or archived chats and leaves workspace trust, account policy and the Off
setting in effect.

To verify after a restart, look for `[claudex] Remote Control claudex_restart`
and the chat’s native connection in Claude Desktop’s main log. Reopen
attempts use `claudex_reopen`. A trigger alone records an attempt. Confirm
the connection in the app log or from your other device.

## Compatibility

- **Claude Desktop 5.11492.5 and Claude Code 2.1.325 on Linux.** Discovery
  reads Desktop registry and worker markers and checks the exact local
  inbox. An update can change those formats or the private exact-ID reopen
  link. Claude’s native inbound rules apply. [Inbox protocol](https://code.claude.com/docs/en/cross-session-messaging#the-sessions-inbox-socket) · [Draft links](https://support.claude.com/en/articles/14729294-open-claude-desktop-with-a-link)
- **Codex Desktop 26.1103.60933 and Codex 0.167.0-alpha.3.2.** The relay
  requires a loaded local Desktop owner that accepts untrusted tool input.
  Discovery and history read the local Desktop SQLite projection, which
  covers local saved chats and available projected history. A desktop update
  can change the router or database schema.
- The server runs as the local user and can read that user’s chat data.
  Keep it on stdio. Choose which chat providers and projects receive the MCP
  tools through each client’s normal configuration.

## Check

Use Node.js 24 for the JavaScript checks.

```sh
uv pip sync --python .venv/bin/python --require-hashes requirements.txt requirements-dev.txt
CLAUDEX_PYTHON=.venv/bin/python ./check
```

The check runs strict mypy, Python tests, and the patch runtime tests. It
includes sanitized records from the installed apps and starts only a local
test MCP process.
