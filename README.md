# claudex

Claudex is a local CLI and MCP server for Claude Code and Codex Desktop. It
relays attributed messages and reads chat history and user-installed skills.
The Linux Claude Desktop patch exposes chat controls and restores Remote Control.

## Install and register

On x86-64 NixOS, `nix build .#` builds the two entry points, `claudex` and
`claudex-desktop`, from the flake and `uv.lock`. Home Manager can take the
flake as a locked input. Use `packages.${system}.default` for the runtime
and pass the matching Claude Desktop package's archive and stock launcher
to `claudex-desktop`. The managed desktop entry and link handler should
use that launcher.

Register MCP with the two app CLIs. Use absolute paths to the installed
Claudex command, the managed Claude Desktop launcher, and a shared local
mailbox. The mailbox retains the newest 1,000 messages per recipient.

```sh
claude mcp add claudex --scope user --env "CLAUDEX_CLAUDE_LAUNCHER=$launcher" -- \
  "$claudex" mcp --client claude --state "$mailbox"
codex mcp add claudex --env "CLAUDEX_CLAUDE_LAUNCHER=$launcher" -- \
  "$claudex" mcp --client codex --state "$mailbox"
```

Use `who_am_i` from a new chat to verify its identity. Claude Code supplies
its backend ID when it starts MCP. Codex supplies its chat ID in request
metadata. A Claude Code clear or resume may require reconnecting MCP.

### MCP tools

| Tool | Behavior |
| --- | --- |
| `who_am_i` | Resolves the caller's native chat identity. |
| `claude_list_chats`, `claude_get_chat` | Reads the active Claude Desktop roster, worker state, and saved Remote Control setting. |
| `codex_list_chats`, `codex_chat_status` | Lists saved local Codex chats and checks for a loaded owner that can receive attributed peer output. |
| `read_chat` | Reads visible Claude or Codex chat history by exact ID. Codex history supports pagination. |
| `send_message`, `read_messages` | Relays attributed messages. Codex messages for Claude are kept in the mailbox and the Claude worker receives a notice to read them. |
| `claude_launch`, `claude_open_chat_draft` | Opens Claude Desktop or prepares a draft for the person to submit. |
| `claude_set_remote_control`, `claude_wake_chat` | Turns Remote Control on or off, or starts a saved chat's worker. |
| `claude_set_model`, `claude_set_effort`, `claude_set_fast_mode` | Changes chat settings. Pass null effort for the Desktop default. |
| `claude_set_permission_mode` | Keeps or lowers a chat's permission mode. |
| `claude_archive`, `claude_unarchive`, `claude_stop`, `claude_interrupt` | Changes the archive or worker state. |
| `list_skills`, `read_skill` | Reads installed user skill files as text. |

Claude may hold or decline a notice. A lost Codex acknowledgement leaves
delivery uncertain. Check the recipient before retrying. Peer messages,
history, and skill files are external data and carry no approval authority.

## Control a running desktop

```sh
claudex claude chats --json
claudex claude chat "$claude_chat_id"
claudex claude remote-control "$claude_chat_id" on
claudex claude effort "$claude_chat_id" default
claudex codex chats --query "review"
claudex codex history "$codex_chat_id" --limit 10 --json
claudex codex status "$codex_chat_id"
```

Use `claudex claude --help` and `claudex codex --help` for all commands.
Each command accepts `--json`. Claude lists active Remote Control and its
saved preference separately. Read the command's notice if Desktop cannot
start the worker.

The CLI and MCP server use the same typed client. It connects to
`$XDG_RUNTIME_DIR/claudex/desktop.sock`, mode 0600. The socket accepts
selected Claude Desktop operations in the active account and organization.
Permission increases require the Desktop window. MCP identifies mutations
so the client can apply its normal tool approval policy. A stock launch
reports the control socket as unavailable.

## Claude Desktop launch and restore

The Linux patch supports Claude Desktop `5.11492.5`. The pinned archive hash
and native hooks are in [patcher/build.py](patcher/build.py). Build the
package with `nix build .#`. When running the launcher outside Home
Manager, pass `--source /absolute/path/to/installed/app.asar` before `--`
and the absolute stock account launcher after it. Bubblewrap must be on
the desktop session's PATH.

The launcher uses the systemd user manager's environment and mounts a separate
patched archive read-only over the installed archive. A Home Manager
launcher can accept `--disable` for a stock start after Claude quits.
If the patch cannot start, Claudex reports the reason, notifies the
desktop if available, and starts stock. With `--reuse-running`, one
Claude link can be handed to the existing instance.

At launch, the patch restores local chats with Remote Control saved On.
It respects Desktop policy, workspace trust, and the current account.
Desktop's worker limit can leave some chats cold. Check the roster and
`[claudex] Remote Control claudex_restart` in Desktop's main log.

## Compatibility and checks

The Claude inbox and caller identity use the Claude Code 2.1.325 worker
records. Codex Desktop 26.1103.60933 uses the local chat history projection
and IPC owner discovery. Run MCP over stdio as the desktop user.

Use Python 3.12 and Node 24, the major version used by the pinned Electron.

```sh
uv sync --frozen
./check
nix flake check
```

The Nix check uses the locked development environment to run strict mypy
and the Python and JavaScript tests.
