# Native Claude Desktop reconnect experiment

Claude Desktop 5.11492.5 checks saved Remote Control eligibility in its
main process before a composer send. The reopen link and the Claude Code
peer inbox use different entry points. A reopen and a real inbox-triggered
turn completed on the disposable chat without an attachment attempt.

This experimental command forwards a single local request through
Electron's existing second-instance notification to the active Desktop
session manager. The adapter calls Desktop's existing automatic Remote
Control handler for one already-warm local chat. This path submits no
prompt or peer message. Desktop still determines eligibility, account
policy, and the result. The command does not turn on Remote Control for
a chat that Desktop's automatic policy declines.

## Verify offline

Run `node --test adapter.test.cjs` and `python3 -B client.py --help`.
Use `python3 -B build.py --source /absolute/installed/app.asar --output /absolute/separate/path/app.asar` to
build against the pinned installed bundle. Use a separate output path
for the archive. Keep the vendor package and its unpacked resources
available to the test installation. The builder tests the exact
version and fails if its patch anchors change. Use Node 24 to syntax-check
the extracted application chunk. The adapter can also run under Node 22.

The application receives a request directory path through
`THREAD_BRIDGE_CLAUDE_RECONNECT_DIR`. The operator creates that directory
with mode 0700 and a mode-0600 `config.json`. The config names the exact
Desktop chat IDs, expected native account and organization, and existing
profile marker. These are identifiers and a path. The adapter does not
read credentials. Use a null organization ID for a personal tenancy if
the native record has no organization. See `adapter.cjs` for the config
fields and responses.

Use the same temporary launcher for both the running Desktop copy and
the command that sends the request. It must load the patched archive and
preserve the selected account profile. The command then reuses
Electron's singleton transport to reach the running copy.

```sh
python3 -B client.py \
  --directory /absolute/private/request-directory \
  --launcher /absolute/path/to/temporary-managed-desktop-launcher \
  local_00000000-0000-0000-0000-000000000000
```

Verify the actual Desktop signal for the exact chat and the connected
state after any deferred result. A missing reply is uncertain. Use
the app's log to check that attempt before issuing another command.

On the tested NixOS packaging, both invocations can use a process-local
bind mount of the patched archive at the stock archive's path followed
by the normal account launcher. The mount must stay confined to those
processes and retain the vendor package's unpacked resources. A dummy
bind-mount fixture verified that the overlay survives a nested namespace
and that the original file stays unchanged outside it.
