"""Request Remote Control reattachment from an opted-in Claude Desktop build."""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid


CHAT_ID = re.compile(r"local_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
OUTCOMES = {"connected", "deferred", "failed", "not_run", "refused"}


def verify_directory(directory):
    if not directory.is_absolute() or directory.resolve(strict=True) != directory:
        raise ValueError("Use the absolute path to the private request directory.")
    identity = directory.lstat()
    if (
        not stat.S_ISDIR(identity.st_mode)
        or identity.st_uid != os.getuid()
        or stat.S_IMODE(identity.st_mode) != 0o700
    ):
        raise ValueError("The request directory must be owned by you and mode 0700.")


def read_reply(path):
    try:
        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(handle, "rb") as stream:
        identity = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(identity.st_mode)
            or identity.st_uid != os.getuid()
            or stat.S_IMODE(identity.st_mode) != 0o600
            or identity.st_size > 1024
        ):
            raise ValueError("The app's reply file is unsafe.")
        reply = json.loads(stream.read(1025))
    fields = set(reply) if isinstance(reply, dict) else set()
    reason = reply.get("reason") if fields else None
    if (
        fields not in ({"status"}, {"status", "reason"})
        or reply.get("status") not in OUTCOMES
        or ("reason" in fields and (not isinstance(reason, str) or not re.fullmatch(r"[a-z_]{1,64}", reason)))
    ):
        raise ValueError("The app returned an unexpected reconnect result.")
    return reply


def reconnect(directory, launcher, session_id, timeout):
    verify_directory(directory)
    if not CHAT_ID.fullmatch(session_id):
        raise ValueError("Use a saved native Claude Desktop Code ID.")
    if not launcher.is_absolute() or not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise ValueError("Use the absolute path to the selected Desktop launcher.")

    nonce = str(uuid.uuid4())
    request = directory / (nonce + ".request.json")
    reply = directory / (nonce + ".reply.json")
    descriptor = os.open(request, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        # The second-instance notification is sent only after form data is closed.
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"session_id": session_id}, stream)
        launcher_process = subprocess.Popen(
            [str(launcher), "--thread-bridge-reconnect=" + nonce],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            outcome = read_reply(reply)
            if outcome is not None:
                reply.unlink()
                return outcome
            launcher_process.poll()
            time.sleep(0.15)
        # The app may already have claimed the request. A missing reply is
        # inconclusive and must not cause a replay.
        return {"status": "unconfirmed"}
    finally:
        request.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("session_id")
    args = parser.parse_args()
    if not 0 < args.timeout < float("inf"):
        parser.error("Provide a finite positive timeout.")
    try:
        result = reconnect(args.directory, args.launcher, args.session_id, args.timeout)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Reconnect request failed: {error}\n")
    print(json.dumps(result))
    return 0 if result["status"] == "connected" else 1


if __name__ == "__main__":
    raise SystemExit(main())
