#!/bin/bash
set -e
python3 - <<'PY'
import socket
from pathlib import Path

with socket.create_connection(("redis", 6379), timeout=5) as client:
    client.sendall(b"*1\r\n$4\r\nPING\r\n")
    response = client.makefile("rb").readline()
assert response == b"+PONG\r\n", response
Path("/app/result.txt").write_text("PONG", encoding="utf-8")
PY
