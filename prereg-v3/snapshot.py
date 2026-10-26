"""Pre-registration snapshot: freeze memo premises and their current states before the Q3-2026 filing season.

Records, for every KIC 2026Q2 holding and both memo types, the fact states as of SNAPSHOT_ASOF, plus a hash of the
verifier code. Published to a public git repo before any of the scored filings exist; later alerts are scored
against this frozen state with the same code hash.
Usage: python experiments/prereg/snapshot.py
"""
import csv
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from thesis_redteam.cli import run_case  # noqa: E402
from thesis_redteam.watch import memo_claims  # noqa: E402

SNAPSHOT_ASOF = "2026-10-27"  # sees filings accepted before 2026-10-27 00:00 UTC
OUT = Path(__file__).resolve().parents[1] / "prereg-v3"


def code_hash():
    h = hashlib.sha256()
    for p in sorted((ROOT / "thesis_redteam").rglob("*.py")):
        h.update(p.relative_to(ROOT).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def main():
    holdings = list(csv.DictReader((ROOT / "space/kic_holdings_2026Q2.csv").open()))
    out_path = OUT / "snapshot.jsonl"
    done = {(json.loads(l)["ticker"], json.loads(l)["memo"]) for l in out_path.open()} if out_path.exists() else set()
    started = time.time()
    with out_path.open("a") as fh, tempfile.TemporaryDirectory() as tmp:
        for h in holdings:
            for memo in ("shareholder_return", "earnings_quality"):
                if (h["ticker"], memo) in done:
                    continue
                try:
                    _, _, rep = run_case(ticker=h["ticker"], template=memo, asof=SNAPSHOT_ASOF, offline=False, out_dir=tmp,
                                         cik=h["cik"], no_prices=True, fact_claims=memo_claims(h["ticker"], memo))
                    facts = {f["id"]: {"check": f.get("check"), "status": f["status"], "reason": f.get("reason_ko", "")[:200]}
                             for f in rep.get("fact_claims", [])}
                    latest = sorted((s.get("filing_date") or s.get("filed") or "") for s in rep.get("sources", []))
                    row = {"ticker": h["ticker"], "cik": h["cik"], "memo": memo, "weight_pct": float(h["weight_pct"]),
                           "fact_verdict": rep.get("fact_verdict"), "facts": facts,
                           "sources": [s.get("accession") for s in rep.get("sources", [])]}
                except Exception as exc:
                    row = {"ticker": h["ticker"], "cik": h["cik"], "memo": memo, "error": f"{type(exc).__name__}: {exc}"[:200]}
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
    meta = {"snapshot_asof": SNAPSHOT_ASOF, "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "code_sha256": code_hash(), "holdings": len(holdings), "memos": ["shareholder_return", "earnings_quality"],
            "snapshot_sha256": hashlib.sha256(out_path.read_bytes()).hexdigest(),
            "rule": "Alert when a fact status changes after a new 10-Q/10-K is accepted; checked as of the day after filing. "
                    "Every alert is scored against the original filing. No code change to thesis_redteam during scoring.",
            "seconds": round(time.time() - started)}
    (OUT / "PREREG.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    print(json.dumps(meta, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
