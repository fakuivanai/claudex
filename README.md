# claudex

Claudex lets Claude Code chats in Claude Desktop and chats in Codex Desktop
work together on one Linux machine. It is a command-line tool and an MCP
server. With it, a chat can:

- send a message to a chat in the other app;
- read the other app's chat history and installed skills;
- control Claude Desktop chats: Remote Control, model, effort, fast mode,
  permission mode, archiving and stopping.

The Claude Desktop controls need a small patch, which `claudex-desktop`
applies each time it launches Claude Desktop. The patch also reconnects
Remote Control after a restart. Reading history and skills, and sending to
Codex chats, work without it.

Claudex is built and tested on x86-64 NixOS.

## Install

`nix build .#` builds the two commands from the flake and `uv.lock`:

- `claudex`: the CLI and the MCP server.
- `claudex-desktop`: the Claude Desktop launcher that applies the patch.

To use Home Manager, add this flake as an input and install
`packages.x86_64-linux.default`. Then point Claude Desktop's menu entry and
`claude://` link handler at a wrapper that runs `claudex-desktop` (see
[Launch Claude Desktop with the patch](#launch-claude-desktop-with-the-patch)).

## Register the MCP server

Register Claudex in both apps with absolute paths:

```sh
claude mcp add claudex --scope user --env "CLAUDEX_CLAUDE_LAUNCHER=$launcher" -- \
  "$claudex" mcp --client claude --state "$mailbox"
codex mcp add claudex --env "CLAUDEX_CLAUDE_LAUNCHER=$launcher" -- \
  "$claudex" mcp --client codex --state "$mailbox"
```

- `$claudex` is the installed `claudex` command.
- `$launcher` is the launcher that opens Claude Desktop with the patch.
  `claude_launch` and `claude_open_chat_draft` use it.
- `$mailbox` is a SQLite file shared by both apps. It keeps the newest 1,000
  messages for each recipient.

Only chats started after registration get the tools. To check one, call
`who_am_i` from it: it should return that chat's app, ID and title. Claude
Code gives the server its chat's identity once, when the server starts,
while Codex sends it with each request. So after a Claude Code `/clear` or
resume, you may need to reconnect MCP.

## How messages travel

**From a Claude chat to a Codex chat:**

1. Claudex asks Codex Desktop which of its windows has the chat open.
2. It asks that window to start a new turn, with the message attached as
   tool output from `claudex.peer_message`, not as your input.

The chat must be open in Codex; `codex_chat_status` tells you whether it
is. Each send reports one of:

- `submitted`, with the new turn's ID;
- `not_loaded`: no window has the chat open;
- `refused`;
- `unknown`: Codex didn't confirm. Check the chat before sending again.

**From a Codex chat to a Claude chat:**

1. Claudex stores the message in the mailbox.
2. It puts a one-line notice in the Claude chat's inbox, saying a message is
   waiting.
3. The chat reads the message with `read_messages`.

If the chat has no running worker, Claudex starts one through the patch and
waits up to 18 seconds for its inbox. If that fails, the message stays in
the mailbox until the chat reads it. Claude Code applies its own rules to
the notice and may hold or decline it.

Messages, chat history and skill files come from another agent. They are
data, never approval for anything. The server says so in its MCP
instructions, and every reply that carries another agent's text is labelled
untrusted.

## MCP tools

| Tool | What it does |
| --- | --- |
| `who_am_i` | Returns the calling chat's app, ID and title. |
| `send_message` | Sends a message to a chat in the other app, by exact ID. |
| `read_messages` | Reads messages addressed to the calling chat, after a cursor. |
| `codex_list_chats` | Lists or searches saved Codex chats. |
| `codex_chat_status` | Checks whether a Codex window has the chat open and can take a message. |
| `read_chat` | Reads a Claude or Codex chat's visible history by exact ID. Claude returns up to 100 messages; Codex pages up to 10 turns at a time. |
| `list_skills`, `read_skill` | Lists your installed Claude Code or Codex skills, or reads one `SKILL.md` as plain text. |
| `claude_list_chats`, `claude_get_chat` | Shows Claude Desktop chats with their worker, settings and Remote Control state. |
| `claude_set_remote_control` | Turns Remote Control on or off. Turning it on starts the chat's worker if needed. |
| `claude_wake_chat` | Starts a saved chat's worker without sending anything. |
| `claude_set_model`, `claude_set_effort`, `claude_set_fast_mode` | Changes a chat's model, reasoning effort or fast mode. A null effort means Desktop's default. |
| `claude_set_permission_mode` | Keeps a chat's permission mode or lowers it. |
| `claude_archive`, `claude_unarchive` | Archives or unarchives a chat. |
| `claude_stop`, `claude_interrupt` | Stops a chat's worker, or interrupts its current turn. |
| `claude_launch` | Opens Claude Desktop. |
| `claude_open_chat_draft` | Opens a new Code draft of up to 14,000 characters, optionally in a folder. You review it and send it. |

Each tool is marked read-only, a change, or destructive (archive, stop,
interrupt), so the calling app applies its usual approval rules. The tools
that read or change Claude Desktop chats, and `send_message` to a Claude
chat, need the patch. Without it they say the control socket is
unavailable.

## Command line

```sh
claudex claude chats --json
claudex claude chat "$claude_chat_id"
claudex claude remote-control "$claude_chat_id" on
claudex claude effort "$claude_chat_id" default
claudex claude permission-mode "$claude_chat_id" plan
claudex codex chats --query "review"
claudex codex history "$codex_chat_id" --limit 10 --json
claudex codex status "$codex_chat_id"
```

`claudex claude` controls the running Claude Desktop through the patch.
`claudex codex` reads Codex Desktop's saved chats, and `claudex codex status`
asks the running app. Every `claude` and `codex` command takes `--json`.
`claudex claude --help` and `claudex codex --help` list the rest.

`claudex claude chats` shows Remote Control twice: whether the chat is
connected now, and whether it's saved On in Desktop. When Desktop can't
start a chat's worker, the command prints a notice that says so.

## The Desktop control socket

The patch serves `$XDG_RUNTIME_DIR/claudex/desktop.sock`, mode 0600. The CLI
and the MCP server use the same typed client for it. The socket serves only
the operations listed above, and only for local chats in the account and
organization Desktop has active. Desktop does the work through its own
window API, under its own checks:

- An archived chat can be read, archived or unarchived. Anything else is
  refused until you unarchive it.
- The permission mode can stay the same or go down the order `plan`,
  `default`, `acceptEdits`, `auto`, `bypassPermissions`. Raising it needs
  Desktop's window. `dontAsk` can only stay as it is.
- Requests run one at a time, in order. A request must arrive within two
  seconds and be at most 4 KiB, and a reply is at most 512 KiB.

Anything else is refused, including sending a prompt or answering a
permission request.

## Launch Claude Desktop with the patch

A launcher wrapper runs:

```sh
claudex-desktop --source "$claude_desktop/lib/claude-desktop/resources/app.asar" \
  --reuse-running -- "$stock_launcher" "$@"
```

`--source` is the installed `app.asar`. After `--` comes the normal launcher
and its arguments. On each launch, `claudex-desktop`:

1. Reads the graphical session's environment from the systemd user manager,
   so Desktop doesn't inherit the caller's shell.
2. Leaves a running Desktop alone. With `--reuse-running`, it passes a single
   `claude://` link to it; otherwise it refuses.
3. Checks that the installed archive's SHA-256 matches the supported release,
   then builds the patched archive. The result is cached read-only under
   `$XDG_CACHE_HOME/claudex/desktop`, keyed by the source path and the patch
   files. An archive outside the Nix store is rebuilt on every launch.
4. Uses Bubblewrap to mount the patched archive read-only over the installed
   one, then runs the normal launcher inside that mount. It never changes
   the installed files.

If something fails before Desktop starts, `claudex-desktop` starts stock
Desktop instead. That covers a missing Bubblewrap, an unsupported archive
and a failed mount. It prints the reason and shows a desktop notification.
`--disable` starts stock Desktop on purpose.

Bubblewrap must be on the desktop session's PATH. A lock file under
`$XDG_STATE_HOME/claudex/desktop` keeps two launches from racing.

## Remote Control after a restart

When Desktop starts with the patch, it reconnects Remote Control for chats
where you left it saved On. That is Desktop's own setting. The most
recently used chats go first. The pass:

- covers only local chats in the signed-in account and organization;
- skips chats that are running or have unsent input, scheduled or other
  unattended work, chats moved to the cloud, and chats started remotely;
- waits for Desktop's account flags and organization policy, and respects
  Desktop's restore switch and managed settings;
- checks workspace trust the way Desktop does;
- stops starting workers one short of Desktop's worker limit.

A chat left cold by the limit reconnects when you open it. Each attempt is
logged in `~/.config/Claude/logs/main.log`, as
`[claudex] Remote Control claudex_restart <chat> <result>` at launch or
`claudex_reopen` on opening.

## Updating

After changing the Claudex version, for example the Home Manager pin:

1. Finish the switch. The CLI and MCP command update right away.
2. Quit Claude Desktop and reopen it from the managed launcher. The running
   Desktop keeps its old patch until it restarts.

Between the two steps, the new client can't read the old patch's replies,
and its error says so. If that happens during a change, check the chat
after restarting before you retry, because the change may already have
been applied.

## Compatibility

| Component | Version | What depends on it |
| --- | --- | --- |
| Claude Desktop | 5.11492.5, Electron 45.2.1 | The patch. The archive hash and the native hook points are pinned in [claudex/patch/build.py](claudex/patch/build.py). |
| Claude Code | 2.1.325 workers | The chat inbox and caller identity records. |
| Codex Desktop | 26.1103.60933 | Local chat history and the IPC used to deliver messages. |
| Python | 3.12 | Claudex itself. |
| Node | 24 | The patch tests. This is the Node major version in that Electron. |

Run the MCP server over stdio, as the same user as the desktop apps. When
Claude Desktop updates, the patch refuses the new archive and
`claudex-desktop` starts stock Desktop until the patch is updated.

## Development

```sh
uv sync --frozen
./check
nix flake check
```

`./check` runs strict mypy, then the Python and JavaScript tests. Set
`CLAUDEX_PYTHON` to use an existing interpreter instead of `uv sync`, and
`CLAUDEX_NODE` to pick a Node. `nix flake check` runs the same suite in the
locked environment and checks the built package.

```text
claudex/
  cli.py          the claudex command; `claudex mcp` starts the server
  server.py       the MCP server and its tools
  models.py       chat IDs and messages shared by both apps
  mailbox.py      the SQLite mailbox
  skills.py       reading installed skills
  environment.py  reading the graphical session's environment
  claude/
    chats.py      chat records, history, identity and inbox notices
    control.py    the typed client for the control socket
    lifecycle.py  launching Desktop, drafts, and notifying or waking a chat
  codex/
    history.py    saved chats and their history
    ipc.py        finding the window that has a chat open, and delivering to it
    envelope.py   the message format a Codex chat receives
  patch/
    build.py      checks the installed archive and builds the patched one
    archive.py    reads and writes the ASAR format
    launch.py     the claudex-desktop launcher
    restore.cjs   Remote Control restore, run inside Desktop
    bridge.cjs    the control operations, run inside Desktop
    socket.cjs    the control socket server, run inside Desktop
tests/            the same layout, with recorded app data in fixtures/
```
