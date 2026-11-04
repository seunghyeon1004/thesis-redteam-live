"""Append-only, hash-chained ledger stored as JSON lines. There is no update or delete path."""
import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "GENESIS"


def entry_hash(entry):
    core = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(core, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def entries(self):
        with self.path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def append(self, report, event, data):
        # one writer at a time; the chain head is re-read under the lock
        with self.path.open("a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            lines = [line for line in f if line.strip()]
            last = json.loads(lines[-1]) if lines else None
            entry = {
                "seq": last["seq"] + 1 if last else 1,
                "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "report": report,
                "event": event,
                "data": data,
                "prev": last["hash"] if last else GENESIS,
            }
            entry["hash"] = entry_hash(entry)
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return entry

    def verify(self):
        """(True, None) when every link holds, else (False, first bad seq)."""
        prev, seq = GENESIS, 0
        for e in self.entries():
            seq += 1
            if e.get("seq") != seq or e.get("prev") != prev or e.get("hash") != entry_hash(e):
                return False, seq
            prev = e["hash"]
        return True, None

    def head(self):
        es = self.entries()
        return {"count": len(es), "head": es[-1]["hash"] if es else GENESIS}
