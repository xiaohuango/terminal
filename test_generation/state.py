"""Bounded, content-based filesystem snapshots, also executable inside Linux."""

import hashlib
import json
import os
from pathlib import Path
import stat
import sys


def snapshot(roots, max_entries=4000, max_bytes=65536):
    entries, errors = {}, []
    pending = list(roots)
    while pending:
        path = Path(pending.pop())
        key = str(path)
        if key in entries:
            continue
        if len(entries) >= max_entries:
            errors.append("entry limit exceeded")
            break
        try:
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                entries[key] = {"kind": "symlink", "target": os.readlink(path)}
            elif stat.S_ISDIR(mode):
                entries[key] = {"kind": "directory"}
                pending.extend(sorted(path.iterdir(), reverse=True))
            elif stat.S_ISREG(mode):
                size = path.stat().st_size
                item = {"kind": "file", "size": size}
                if size <= 64 * 1024 * 1024:
                    digest = hashlib.sha256()
                    with path.open("rb") as stream:
                        for block in iter(lambda: stream.read(65536), b""):
                            digest.update(block)
                    item["sha256"] = digest.hexdigest()
                else:
                    errors.append("hash limit exceeded: " + key)
                if size <= max_bytes:
                    try:
                        item["text"] = path.read_bytes().decode("utf-8")
                    except UnicodeDecodeError:
                        pass
                entries[key] = item
            else:
                entries[key] = {"kind": "special"}
        except FileNotFoundError:
            continue
        except OSError as error:
            errors.append(key + ": " + str(error))
    return {"entries": dict(sorted(entries.items())), "errors": errors}


def difference(before, after):
    old, new = before["entries"], after["entries"]
    return {"created": {p: new[p] for p in sorted(new.keys() - old.keys())},
            "deleted": {p: old[p] for p in sorted(old.keys() - new.keys())},
            "modified": {p: {"before": old[p], "after": new[p]} for p in sorted(old.keys() & new.keys())
                         if old[p] != new[p]}}


if __name__ == "__main__":
    print(json.dumps(snapshot(json.loads(sys.argv[1])), ensure_ascii=False))
