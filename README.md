# claudex

A local MCP server for Claude Code and Codex Desktop. It sends attributed
messages, reads chat history and user-installed skills, and asks Claude Desktop
to reopen saved Code chats. An optional Linux patch restores saved Claude
Remote Control connections on startup and reopen.

## Set up the bridge

Use Python 3.12. Install the hash-pinned runtime and build dependencies
and the `claudex` entry point from the repository root.

```sh
uv venv --python 3.12
uv pip sync --python .venv/bin/python --require-hashes requirements.txt requirements-dev.txt
uv pip install --python .venv/bin/python --no-build-isolation --no-deps .
```

Both clients use the same absolute `--state` path to a local SQLite mailbox
in a directory you own. It retains the newest 1,000 messages per recipient.
Use a new mailbox path if you tried the prototype's earlier format.

Set `CLAUDEX_CLAUDE_LAUNCHER` in the MCP client configuration to the absolute
path of the executable that opens your signed-in Claude Desktop. To use the
patch for cold launches, point it at a wrapper like the one below. Launch,
wake and draft use this one configured executable.

```sh
#!/bin/sh
exec /path/to/claudex/.venv/bin/python -m patcher.launch \
  --source /absolute/path/to/installed/app.asar --reuse-running \
  -- /absolute/path/to/normal-account-launcher "$@"
```

### Claude Code in Desktop

Open any intended destination in Codex Desktop. Add the following to the
Claude Code project's `.mcp.json`, replacing the paths. Use the usual
project MCP approval if prompted.

```json
{
  "mcpServers": {
    "claudex": {
      "type": "stdio",
      "command": "/path/to/claudex/.venv/bin/claudex",
      "args": [
        "--client", "claude",
        "--state", "/path/to/claudex-state/mail.sqlite"
      ],
      "env": {
        "CLAUDEX_CLAUDE_LAUNCHER": "/absolute/path/to/claude-launcher"
      }
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
command = "/path/to/claudex/.venv/bin/claudex"
args = [
  "--client", "codex",
  "--state", "/path/to/claudex-state/mail.sqlite",
]

[mcp_servers.claudex.env]
CLAUDEX_CLAUDE_LAUNCHER = "/absolute/path/to/claude-launcher"
```

### Tools

| Tool | Behavior |
| --- | --- |
| `who_am_i` | Resolves the caller’s native chat identity. |
| `list_chats`, `read_chat` | Lists live Claude Desktop chats and saved local Codex Desktop chats. Select a provider for either tool. Read history by exact ID, including saved Claude transcripts with no running worker. Codex lists and history support pagination. |
| `send_message`, `read_messages` | Delivers Claude messages to any loaded Codex chat as attributed tool output. An unloaded Codex chat is reported as `not_loaded`. Stores Codex messages for the exact Claude chat and hands its worker a fixed notice. The recipient calls `read_messages` to fetch the body. |
| `launch_claude`, `wake_chat` | Opens Claude Desktop or asks it to reopen a saved chat. `waiting_for_inbox` means the native worker did not appear in time. Stored mail remains available. |
| `open_chat_draft` | Opens a Claude Code composer for the person to review and submit. Claude confirms any folder supplied through the link. |
| `list_skills`, `read_skill` | Reads local user `SKILL.md` files as text, including linked skill directories installed by Home Manager. Scopes are `claude-user`, `codex-user`, and `agents-user`. |

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

Wrap the command you already use to start the signed-in account. Close the
existing instance first, since it owns the Electron singleton. Use the
Python environment where `claudex` is installed. The patch requires Bubblewrap
on the desktop session's `PATH`.

```sh
python3 -m patcher.launch \
  --source /absolute/path/to/installed/app.asar \
  -- /absolute/path/to/normal-account-launcher
```

The wrapper starts with the systemd user manager's desktop environment.
It caches the patched archive by source and patch digest and mounts it
read-only over the installed archive for that process tree. Keep the vendor
package and unpacked resources available.

Add `--disable` before `--` to launch stock through this wrapper. If the patch
cannot be built or the mount cannot start, the wrapper reports the reason,
notifies the graphical session when the notification service is available,
and starts stock. Once the app starts, its own exit status is returned.
By default the launcher refuses a second instance. With `--reuse-running`,
one native Claude link can be handed to the existing app without a new
launch.

For a Home Manager setup, keep the original account helper as a separate
stock command. Point the shared desktop entry, URL handler and account
switcher's relaunch at the wrapper shown above. Use an installed Claudex
environment in a persistent data directory and pass the archive from the
currently installed Claude Desktop package. An app update can then reach
the version check and fall back to stock when needed.

To roll back for one session, quit Desktop and run the wrapper with
`--disable` before the separator. The managed stock launch invalidates old
quit snapshots. You can then switch back to a Home Manager generation with
the original launcher for persistent rollback.

The patch snapshots the exact local chats with Remote Control connected
when Desktop quits. The next patched launch restores that account and
organization's snapshot through Desktop's native Remote Control policy.
Desktop's worker limit can leave some of those chats for reopening. The
patch skips stopped or archived chats and respects workspace trust, account
policy and the Off setting. The first patched launch starts tracking
connections for its next quit. Use the wrapper for stock launches as well,
so switching to stock invalidates any older patched snapshot.

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
