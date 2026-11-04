"""Recorded end-to-end run over the localhost API, on two real post-seal cases.

Celestica: a real broken premise. Submit is refused, the reviewer opens an investigation with a reason, submit passes.
QSR: a source-checked false alarm. It still blocks submission until it is dismissed with a reason.
Writes gate/runs/<name>-ledger.jsonl, <name>-transcript.json and gate/ANCHOR.json (head hash to commit to GitHub).
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from gate.server import serve

HERE = Path(__file__).resolve().parent
STEPS = [
    ("POST", "/reports/CLS-shareholder_return/attach", {}),
    ("POST", "/reports/CLS-shareholder_return/submit", {"actor": "담당자A"}),
    ("POST", "/reports/CLS-shareholder_return/resolve", {"finding": "CLS-shareholder_return:F2", "action": "조사 착수",
                                                         "reason": "10-Q 현금흐름표에서 8/7 공모 현금 유입 $3,391.3M 확인. 일회성 여부와 비중 재검토", "actor": "담당자A"}),
    ("POST", "/reports/CLS-shareholder_return/submit", {"actor": "담당자A"}),
    ("POST", "/reports/QSR-shareholder_return/attach", {}),
    ("POST", "/reports/QSR-shareholder_return/submit", {"actor": "담당자B"}),
    ("POST", "/reports/QSR-shareholder_return/resolve", {"finding": "QSR-shareholder_return:F1", "action": "기각",
                                                         "reason": "원문 현금흐름표에 보통주 매입 (444) 존재. 태그 누락에 의한 오탐", "actor": "담당자B"}),
    ("POST", "/reports/QSR-shareholder_return/submit", {"actor": "담당자B"}),
    ("POST", "/reports/QSR-shareholder_return/resolve", {"finding": "QSR-shareholder_return:F2", "action": "기각",
                                                         "reason": "F1과 같은 원인(매입 줄 미인식)", "actor": "담당자B"}),
    ("POST", "/reports/QSR-shareholder_return/submit", {"actor": "담당자B"}),
    ("DELETE", "/ledger/verify", None),
    ("GET", "/ledger/verify", None),
]


def call(base, method, path, body):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def main(name):
    ledger = HERE / "runs" / f"{name}-ledger.jsonl"
    if ledger.exists():
        raise SystemExit(f"{ledger} exists; runs are never overwritten")
    httpd = serve(ledger)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    transcript = []
    try:
        for method, path, body in STEPS:
            code, resp = call(base, method, path, body)
            transcript.append({"request": f"{method} {path}", "body": body, "status": code, "response": resp})
            print(code, method, path)
    finally:
        httpd.shutdown()
    (HERE / "runs" / f"{name}-transcript.json").write_text(json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
    verify = transcript[-1]["response"]
    anchor = {"run": name, "ledger": f"gate/runs/{name}-ledger.jsonl", "count": verify["count"], "head": verify["head"],
              "chain_ok": verify["ok"], "written_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "note": "The GitHub commit containing this file fixes the head hash in time. Any later edit to the ledger changes the head."}
    (HERE / "ANCHOR.json").write_text(json.dumps(anchor, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(anchor, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "scenario")
