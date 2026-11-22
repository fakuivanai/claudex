"""Confirm per-chat Remote Control attachment from the native Desktop log."""
import os
import re


def log_position(path):
    entry = path.stat()
    return entry.st_ino, entry.st_size


def connection_seen(path, cursor, target):
    with path.open(errors="replace") as source:
        current = os.fstat(source.fileno())
        if current.st_ino != cursor[0] or current.st_size < cursor[1]:
            return False
        source.seek(cursor[1])
        lines = source.read(1_500_000).splitlines()

    selected = False
    enabled = False
    for line in lines:
        attempt = re.search(r"Enabling remote control for session (local_[0-9a-f-]+)", line)
        if attempt:
            selected = attempt[1] == target
            enabled = False
        if selected and "Remote control enabled:" in line:
            enabled = True
        if selected and enabled and re.search(r'bridge_state:\s*"connected"', line):
            return True
    return False
