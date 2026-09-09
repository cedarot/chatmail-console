#!/usr/bin/env python3
"""Export only Dovecot login metadata from the Chatmail Docker log.

This helper is intended to run as a root-owned host service. It never copies
raw Docker logs to the console and never writes message bodies or headers.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import deque
from pathlib import Path


LOGIN_LINE = re.compile(r"imap-login: Login: user=<([^>]+)>.*?rip=([^,\s]+)")
OUTPUT = Path(os.getenv("CHATMAIL_LOGIN_EVENTS_OUTPUT", "/srv/chatmail-console/data/logins.jsonl"))
MAX_LINES = int(os.getenv("CHATMAIL_LOGIN_EVENTS_MAX_LINES", "200000"))
INTERVAL = int(os.getenv("CHATMAIL_LOGIN_EVENTS_INTERVAL", "60"))


def docker_log_path() -> Path:
    value = subprocess.check_output(
        ["docker", "inspect", "chatmail", "--format", "{{.LogPath}}"],
        text=True,
        stderr=subprocess.STDOUT,
        timeout=10,
    ).strip()
    if not value:
        raise RuntimeError("Chatmail Docker log path is empty")
    return Path(value)


def write_snapshot(source: Path, target: Path) -> int:
    with source.open("r", encoding="utf-8", errors="replace") as handle:
        lines = deque(handle, maxlen=MAX_LINES)
    records: dict[tuple[str, str, str], dict[str, str]] = {}
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        match = LOGIN_LINE.search(str(payload.get("log", "")))
        timestamp = payload.get("time")
        if not match or not timestamp:
            continue
        user_id, ip = match.groups()
        key = (str(timestamp), user_id, ip)
        records[key] = {
            "log": f"imap-login: Login: user=<{user_id}>, rip={ip}",
            "time": str(timestamp),
        }

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="logins-", suffix=".jsonl", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for record in sorted(records.values(), key=lambda item: item["time"]):
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return len(records)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        count = write_snapshot(docker_log_path(), OUTPUT)
        print(json.dumps({"event": "login_events_exported", "count": count}), flush=True)
        return
    while True:
        try:
            count = write_snapshot(docker_log_path(), OUTPUT)
            print(json.dumps({"event": "login_events_exported", "count": count}), flush=True)
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
            print(json.dumps({"event": "login_events_export_failed", "error": str(exc)}), flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
