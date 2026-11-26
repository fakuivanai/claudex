# Claude Desktop Linux fixture

These files hold fields recorded from a real local Code chat in Claude
Desktop 5.11492.5, with a Claude Code 2.1.325 worker:

- `registry-record.json`: the chat's saved record in Desktop's chat
  registry.
- `runtime-marker.json`: the marker the running worker writes, with its
  process, PID namespace, inbox socket and status.

The chat IDs, process ID, machine and PID namespace, socket path and title
are replaced with stand-ins. Fields Claudex doesn't read are left out.

`history-authorship.jsonl` uses sanitized transcript envelopes recorded from
the same Desktop and worker versions. IDs and bodies are stand-ins. The
older unauthenticated body tests that text cannot supply authorship.
