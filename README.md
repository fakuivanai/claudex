# claudex

Claudex lets Claude Code chats in Claude Desktop and chats in Codex Desktop
work together on one Linux machine. It is a command-line tool and an MCP
server. With it, a chat in either app can:

- start a chat in the other app, with a folder, model, effort and task;
- send a chat in the other app a message, and get the reply back;
- read the other app's chat history and installed skills;
- see whether a chat is running, interrupt it, change its model or effort,
  and archive it;
- manage Claude Desktop's Remote Control, fast mode and permission mode.

Most of this needs a small patch for each app. Claudex applies the patches
at launch, as a read-only copy of the app's archive, and never changes the
installed files. Without the patches, a chat can still read history and
skills, and send to a Codex chat that's open in a window.

Claudex is built and tested on x86-64 NixOS with KDE Plasma.

## Install

`nix build .#` builds three commands from the flake and `uv.lock`:

- `claudex`: the CLI and the MCP server.
- `claudex-desktop`: launches Claude Desktop with its patch.
- `claudex-codex-patch`: builds the Codex Desktop patch.

To use Home Manager, add this flake as an input and install
`packages.x86_64-linux.default`. Point Claude Desktop's menu entry and
`claude://` link handler at a wrapper that runs `claudex-desktop`, and
Codex Desktop's at a launcher that mounts its patch. Both are described
below.

## Register the MCP server

Register Claudex in both apps, with absolute paths:

```sh
claude mcp add claudex --scope user --env "CLAUDEX_CLAUDE_LAUNCHER=$launcher" -- \
  "$claudex" mcp --client claude --state "$mailbox"
codex mcp add claudex --env "CLAUDEX_CLAUDE_LAUNCHER=$launcher" -- \
  "$claudex" mcp --client codex --state "$mailbox"
```

- `$claudex` is the installed `claudex` command.
- `$launcher` opens Claude Desktop with its patch. `claude_launch` and
  `claudex claude draft` use it.
- `$mailbox` is a SQLite file both apps share. It keeps the newest 1,000
  messages for each chat.

New chats get the tools straight away. Existing Codex chats get them when
Codex Desktop restarts. To check a chat, call `who_am_i` from it. It
returns the chat's app, ID and title. Claude Code tells the server who the
chat is once, when the server starts, so after a `/clear` or a resume you
may need to reconnect MCP. Codex says it with every request.

## Start a chat in the other app

A Codex chat calls `claude_start_chat`, and a Claude chat calls
`codex_start_chat`. Each takes an existing local folder, a model, an
optional effort and the task, and returns the new chat's ID. Use that ID
for follow-up messages, history and controls.

- **A new Claude chat** starts with a fixed Claudex notice. The notice says
  which Codex chat sent the task and that the task is waiting in the
  mailbox. The task's text never becomes the chat's first message. The
  chat gets the permission mode Desktop would give any new chat in that
  folder: your saved choice, or Desktop's Auto default where policy allows
  it. Desktop's workspace trust check still applies.
- **A new Codex chat** gets the task as its opening prompt, under a fixed
  label. The label names the sending Claude chat and says the task doesn't
  count as your approval for anything. The chat runs with Codex's default
  permissions.

## How messages travel

**From a Claude chat to a Codex chat:**

1. Claudex asks Codex Desktop which window has the chat. With the Codex
   patch, Desktop can also load a saved chat in the background, with no
   window or focus change.
2. Once Desktop confirms it has the chat, Claudex hands over the message as
   tool output from `claudex.peer_message`, never as your input. Codex
   starts a turn, or adds the message to the turn already running.

Each send reports one of:

- `submitted`, with the ID of the turn that got the message.
  `joined_existing` is true when that turn was already running before the
  send, and null when Claudex can't tell;
- `not_loaded`: Desktop couldn't load the chat. Without the patch, that
  means no window has it open;
- `refused`;
- `unknown`: Codex didn't confirm. Check the chat before sending again.

`codex_chat_status` says whether a chat is open (`loaded`) and whether a
message can reach it (`reachable`). An unopened chat the patch can load is
reachable but not loaded. Checking doesn't load anything.

**From a Codex chat to a Claude chat:**

1. Claudex stores the message in the mailbox.
2. It puts a one-line notice in the Claude chat's inbox, saying a message
   is waiting.
3. The chat reads the message with `read_messages`.

If the chat has no running worker, Claudex starts one through the patch and
waits up to 18 seconds for its inbox. If that fails, the message waits in
the mailbox until the chat reads it.

Claude Desktop runs at most 10 workers at a time, and every chat with
Remote Control on keeps one. When all 10 are taken, a chat with no worker
can't wake up, and its message waits in the mailbox. Turning Remote Control
off on chats you don't need from your phone frees workers.

Claude Code applies its own rules to the notice and may hold or decline
it. It also shows the notice as coming from another Claude session, even
when a Codex chat sent it. The notice itself names the real sender.

## What a chat may do with a request

The server tells both apps the same thing. A message from another chat is
a request from an AI working for the same person. The chat acts on it
within its own permissions and your standing instructions, and replies with
`send_message`. A request never grants a permission and never counts as
your approval. Chat history and skill files are data to read, not
instructions to follow.

Each message carries the sending app, chat ID and title. Claudex doesn't
verify them: any program running as you could write to the mailbox
directly. On a single-user machine that's fine, but the sender is a label,
not proof.

`read_chat` reports a message's author separately from its stored role.
It uses the apps' recorded metadata and returns `unknown` when authorship
is ambiguous. Set `developer_only` to keep messages with a recorded
developer author. Codex's visible history does not record who supplied
a user-role prompt, so that filter can return no messages even when
the chat shows prompts.

## MCP tools

| Tool | What it does |
| --- | --- |
| `who_am_i` | Returns the calling chat's app, ID and title. |
| `claude_start_chat`, `codex_start_chat` | Start a chat in the other app with a task, and return its ID. |
| `send_message` | Sends a message to a chat in the other app, by exact ID. |
| `read_messages` | Reads messages for the calling chat, after a cursor. |
| `read_chat` | Reads a Claude or Codex chat's history by exact ID. Claude returns up to 100 messages. Codex pages up to 10 turns at a time. |
| `list_skills`, `read_skill` | Lists your installed Claude Code or Codex skills, or reads one `SKILL.md`. |
| `codex_list_chats` | Lists or searches saved Codex chats. |
| `codex_chat_status` | Says whether a Codex chat is open, reachable and running. |
| `codex_interrupt` | Interrupts a Codex chat's running turn. |
| `codex_set_model`, `codex_set_effort` | Sets the model or effort for a Codex chat's next turns. |
| `codex_archive`, `codex_unarchive` | Archives a Codex chat, or brings it back. |
| `claude_list_chats`, `claude_get_chat` | Shows Claude chats with their worker, settings and Remote Control state. |
| `claude_set_remote_control` | Turns Remote Control on or off. Turning it on starts the chat's worker if needed. |
| `claude_wake_chat` | Starts a saved chat's worker without sending anything. |
| `claude_set_model`, `claude_set_effort`, `claude_set_fast_mode` | Changes a Claude chat's model, effort or fast mode. A null effort means Desktop's default. |
| `claude_set_permission_mode` | Keeps a Claude chat's permission mode or lowers it. |
| `claude_archive`, `claude_unarchive` | Archives a Claude chat, or brings it back. |
| `claude_stop`, `claude_interrupt` | Stops a Claude chat's worker, or interrupts its turn. |
| `claude_launch` | Opens Claude Desktop. |

Each tool is marked read-only, a change, or destructive (archive, stop,
interrupt), so the calling app applies its usual approval rules.

The `claude_*` tools, and `send_message` to a Claude chat, need the Claude
patch. Without it, they say the control socket is unavailable. Starting a
Codex chat, interrupting or archiving one, and seeing whether one is
running need the Codex patch. Without it, they say so.

## Command line

```sh
claudex claude chats --json
claudex claude chat "$claude_chat_id"
claudex claude remote-control "$claude_chat_id" on
claudex claude effort "$claude_chat_id" default
claudex claude permission-mode "$claude_chat_id" plan
claudex claude draft "Review the open pull request" --folder "$repo"
claudex codex chats --query "review"
claudex codex history "$codex_chat_id" --limit 10 --json
claudex codex status "$codex_chat_id"
claudex claude restart --wait
claudex codex restart --wait
```

Every `claude` and `codex` command takes `--json`, and `--help` lists the
rest. `claudex claude chats` shows Remote Control twice: whether the chat
is connected now, and whether it's saved On in Desktop. When Desktop can't
start a chat's worker, the command says so. `claudex claude draft` opens a
draft in Claude Desktop for you to review and send yourself.

## Restart either app

`claudex claude restart` and `claudex codex restart`:

1. Refuse if a chat is running, and list the chats that are. `--wait`
   waits for them to finish. `--force` lets the restart interrupt them.
2. Quit the app cleanly. Claude quits through its control socket. A Claude
   Desktop still running an older patch quits through the KDE tray menu.
3. Start the app again in its own systemd user service, so neither app ends
   up inside the other's.
4. Report what came back: Claude chats with Remote Control on, and Codex
   chats that can be reached.

Run from inside the app being restarted, the command hands off to a
separate service and prints a receipt path, so you can check the result
after the connection drops. Set `CLAUDEX_CLAUDE_LAUNCHER` or
`CLAUDEX_CODEX_LAUNCHER` if a launcher isn't on the usual path.

## The Claude Desktop patch

### Launch Claude Desktop with it

A launcher wrapper runs:

```sh
claudex-desktop --source "$claude_desktop/lib/claude-desktop/resources/app.asar" \
  --reuse-running -- "$stock_launcher" "$@"
```

`--source` is the installed `app.asar`. After `--` comes the normal
launcher and its arguments. On each launch, `claudex-desktop`:

1. Reads the graphical session's environment from the systemd user
   manager, so Desktop doesn't inherit the caller's shell.
2. Leaves a running Desktop alone. With `--reuse-running`, it passes a
   single `claude://` link to it. Otherwise it refuses.
3. Checks that the installed archive's SHA-256 matches the supported
   release, then builds the patched archive. The result is cached read-only
   under `$XDG_CACHE_HOME/claudex/desktop`, keyed by the source path and the
   patch files. An archive outside the Nix store is rebuilt on every launch.
4. Uses Bubblewrap to mount the patched archive read-only over the
   installed one, then runs the normal launcher inside that mount.

If anything fails before Desktop starts, `claudex-desktop` starts stock
Desktop instead and says why, in the terminal and as a desktop
notification. That covers a missing Bubblewrap, an unsupported archive and
a failed mount. `--disable` starts stock Desktop on purpose. Bubblewrap
must be on the desktop session's PATH. A lock file under
`$XDG_STATE_HOME/claudex/desktop` keeps two launches from racing.

### The control socket

The patch serves `$XDG_RUNTIME_DIR/claudex/desktop.sock`, mode 0600. The
CLI and the MCP server share one client for it. If `XDG_RUNTIME_DIR` is
missing, as in the servers Codex starts, they use `/run/user/<uid>`, but
only if that folder belongs to you and has mode 0700.

The socket only acts on local chats in the account and organization
Desktop has active, through Desktop's own APIs:

- An archived chat can be read, archived or unarchived. Anything else is
  refused until you unarchive it.
- A permission mode can stay the same or go down the order `plan`,
  `default`, `acceptEdits`, `auto`, `bypassPermissions`. Raising it needs
  Desktop's window. `dontAsk` can only stay as it is.
- Starting a chat sends only the fixed Claudex notice. The socket never
  sends your words or a peer's words as a prompt, and never answers a
  permission request.
- Requests run one at a time, in order. A request must arrive within two
  seconds and be at most 4 KiB. A reply is at most 512 KiB.

### Remote Control after a restart

When Desktop starts with the patch, it reconnects Remote Control for chats
you left saved On. That's Desktop's own setting. The most recently used
chats go first. The pass:

- covers only local chats in the signed-in account and organization;
- skips chats that are running or have unsent input, scheduled or other
  unattended work, chats moved to the cloud, and chats started remotely;
- waits for Desktop's account flags and organization policy, and respects
  Desktop's restore switch and managed settings;
- checks workspace trust the way Desktop does;
- always leaves one worker free.

With a limit of 10 workers, that means at most 9 chats reconnect. The rest
stay disconnected until you turn them back on. Desktop logs each attempt in
`~/.config/Claude/logs/main.log`, as
`[claudex] Remote Control claudex_restart <chat> <result>` at launch or
`claudex_reopen` when you open a chat.

## The Codex Desktop patch

The Codex patch adds a few local requests to Codex Desktop's own host
code. They load a saved chat in the background, start a chat, report
whether chats are running, interrupt a turn, change model or effort, and
archive or unarchive. They take chat IDs and fixed actions only, skip
archived chats and subagent chats, and never change the window or focus.

Build the patch from the installed archive, then start Codex Desktop with
it mounted over the original:

```sh
source=/absolute/path/to/codex/resources/app.asar
patched=/absolute/path/to/patched-codex.asar
claudex-codex-patch --source "$source" --output "$patched"
systemd-run --user --collect -p ExitType=cgroup \
  bwrap --bind / / --dev-bind /dev /dev --proc /proc \
  --ro-bind "$patched" "$source" -- chatgpt
```

The build checks the archive's hash and refuses any other release. If your
system config and Home Manager pin Codex Desktop separately, move both to
the same release together, or the Home Manager build fails.

## Updating

After changing the Claudex version, for example the Home Manager pin:

1. Finish the switch. The CLI and the MCP command update right away.
2. Restart each app whose patch changed. A running app keeps its current
   patch until it restarts.

Between the two steps, the new client may not understand the old patch's
replies, and its error says so. If that happens during a change, check the
chat after restarting before you retry, because the change may already
have happened.

## Compatibility

| Component | Version | What depends on it |
| --- | --- | --- |
| Claude Desktop | 5.11492.5, Electron 45.2.1 | The Claude patch. The archive hash and hook points are pinned in [claudex/patch/build.py](claudex/patch/build.py). |
| Claude Code | 2.1.325 workers | The chat inbox and caller identity records. |
| Codex Desktop | 26.1103.60933 | Chat history, message delivery, and the Codex patch in [claudex/codex/desktop_patch.py](claudex/codex/desktop_patch.py). |
| KDE Plasma | 6 | Quitting a Claude Desktop that still runs an older patch. |
| Python | 3.12 | Claudex itself. |
| Node | 24 | The patch tests. This is the Node major version in that Electron. |

Run the MCP server over stdio, as the same user as the desktop apps. When
Claude Desktop updates, its patch refuses the new archive and
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
  cli.py             the claudex command; `claudex mcp` starts the server
  server.py          the MCP server and its tools
  restart.py         guarded app restarts and their receipts
  models.py          chat IDs and messages shared by both apps
  mailbox.py         the SQLite mailbox
  skills.py          reading installed skills
  environment.py     the graphical session's environment and runtime folder
  claude/
    chats.py         chat records, history, identity and inbox notices
    control.py       the typed client for the control socket
    lifecycle.py     launching Desktop, drafts, and notifying or waking a chat
  codex/
    history.py       saved chats and their history
    ipc.py           message delivery and chat controls
    envelope.py      the message format a Codex chat receives
    desktop_patch.py builds the Codex Desktop patch
    prepare.cjs      the Codex patch's requests, run inside Codex Desktop
  patch/
    build.py         checks Claude's archive and builds the patched one
    archive.py       reads and writes the ASAR format
    launch.py        the claudex-desktop launcher
    restore.cjs      Remote Control restore, run inside Claude Desktop
    bridge.cjs       the control operations, run inside Claude Desktop
    socket.cjs       the control socket server, run inside Claude Desktop
tests/               the same layout, with recorded app data in fixtures/
```

## Licence

MIT. See [LICENSE](LICENSE).
