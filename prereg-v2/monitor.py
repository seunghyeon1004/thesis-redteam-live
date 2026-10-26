"""Live thesis-watch monitor for the pre-registered Q3-2026 season.

Each run: fetch fresh SEC submissions for every holding (no cache), find 10-Q/10-K accepted after the
pre-registration commit, and once the day after filing has begun (UTC), re-check both memos with the frozen code.
If any premise status changed versus the last recorded state, the investigator Agent writes an alert.
Usage (from the repo root): python prereg/monitor.py
"""
import csv
import datetime as dt
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from thesis_redteam.cli import run_case  # noqa: E402
from thesis_redteam.llm import ClaudeCLIProvider  # noqa: E402
from thesis_redteam.sec_client import SecClient  # noqa: E402
from thesis_redteam.watch import investigate, memo_claims, write_alert  # noqa: E402

PREREG_COMMIT_UTC = dt.datetime(2026, 10, 26, 4, 45, 0, tzinfo=dt.timezone.utc)  # v2 seal
# Test-only overrides; the scored live runs use neither.
if os.environ.get("MONITOR_TEST_SINCE"):
    PREREG_COMMIT_UTC = dt.datetime.fromisoformat(os.environ["MONITOR_TEST_SINCE"])
MEMOS = ("shareholder_return", "earnings_quality")
UA = "Public historical filing verification research@example.com"
HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("MONITOR_DATA", HERE / "live"))


def submissions(cik):
    req = urllib.request.Request(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", headers={"User-Agent": UA})
    return json.load(urllib.request.urlopen(req, timeout=30))["filings"]["recent"]


def new_filings(cik):
    r = submissions(cik)
    out = []
    for form, acc, accepted in zip(r["form"], r["accessionNumber"], r["acceptanceDateTime"]):
        if form not in ("10-Q", "10-K"):
            continue
        at = dt.datetime.fromisoformat(accepted.replace("Z", "+00:00"))
        if at > PREREG_COMMIT_UTC:
            out.append({"form": form, "accession": acc, "accepted_utc": at.isoformat()})
    return sorted(out, key=lambda x: x["accepted_utc"])


def check(ticker, cik, memo, asof, workdir):
    os.chdir(workdir)  # fresh SEC cache per filing so new facts are visible
    _, _, rep = run_case(ticker=ticker, template=memo, asof=asof, offline=False, out_dir=Path(workdir) / "reports",
                         cik=cik, no_prices=True, fact_claims=memo_claims(ticker, memo))
    return rep


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    state_path = DATA / "state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
    else:
        state = {"facts": {}, "processed": [], "reports": {}}
        for line in (HERE / "snapshot.jsonl").open():
            row = json.loads(line)
            state["facts"][f"{row['ticker']}|{row['memo']}"] = {k: v["status"] for k, v in row.get("facts", {}).items()}
    holdings = list(csv.DictReader((ROOT / "kic_holdings_2026Q2.csv").open()))
    if os.environ.get("MONITOR_TEST_TICKERS"):
        holdings = [h for h in holdings if h["ticker"] in os.environ["MONITOR_TEST_TICKERS"].split(",")]
    now = dt.datetime.now(dt.timezone.utc)
    events_path = DATA / "events.jsonl"
    log = {"run_utc": now.isoformat(), "checked": 0, "new_filings": 0, "processed": 0, "alerts": 0, "errors": []}
    for h in holdings:
        try:
            filings = new_filings(h["cik"])
        except Exception as exc:
            log["errors"].append(f"{h['ticker']} submissions: {exc}"[:200])
            continue
        log["checked"] += 1
        time.sleep(0.15)
        for f in filings:
            log["new_filings"] += 1
            if f["accession"] in state["processed"]:
                continue
            accepted = dt.datetime.fromisoformat(f["accepted_utc"])
            asof = (accepted.date() + dt.timedelta(days=1)).isoformat()
            if now < dt.datetime.fromisoformat(asof + "T00:00:00+00:00"):
                continue  # visible only from the next UTC day
            with tempfile.TemporaryDirectory() as work:
                for memo in MEMOS:
                    key = f"{h['ticker']}|{memo}"
                    started = time.time()
                    try:
                        new = check(h["ticker"], h["cik"], memo, asof, work)
                        old_statuses = state["facts"].get(key, {})
                        new_statuses = {x["id"]: x["status"] for x in new.get("fact_claims", [])}
                        changed = [k for k in new_statuses if new_statuses[k] != old_statuses.get(k)]
                        event = {"ticker": h["ticker"], "memo": memo, "form": f["form"], "accession": f["accession"],
                                 "accepted_utc": f["accepted_utc"], "asof": asof, "processed_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                                 "old": old_statuses, "new": new_statuses, "changed": changed}
                        if changed:
                            prev = state["reports"].get(key)
                            old_report = json.loads(Path(prev).read_text()) if prev and Path(prev).exists() else \
                                check(h["ticker"], h["cik"], memo, "2026-10-27", work)
                            alert = investigate(old_report, new, provider=ClaudeCLIProvider(), sec_client=SecClient(cache_dir=Path(work) / "cache"))
                            alert["memo"] = memo
                            out_dir = DATA / "alerts" / f"{h['ticker']}_{memo}_{f['accession']}"
                            write_alert(alert, out_dir)
                            event.update(severity=alert["severity"], headline_ko=alert["headline_ko"],
                                         tool_path=alert["trace"]["tool_path"], used_fallback=alert.get("used_fallback"))
                            log["alerts"] += 1
                        rep_path = DATA / "reports" / f"{h['ticker']}_{memo}.json"
                        rep_path.parent.mkdir(parents=True, exist_ok=True)
                        rep_path.write_text(json.dumps(new, ensure_ascii=False, default=str))
                        state["reports"][key] = str(rep_path)
                        state["facts"][key] = new_statuses
                        event["seconds"] = round(time.time() - started, 1)
                        with events_path.open("a") as fh:
                            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                    except Exception as exc:
                        log["errors"].append(f"{h['ticker']} {memo} {f['accession']}: {type(exc).__name__}: {exc}"[:300])
            state["processed"].append(f["accession"])
            log["processed"] += 1
            state_path.write_text(json.dumps(state, ensure_ascii=False))
    os.chdir(ROOT)
    state_path.write_text(json.dumps(state, ensure_ascii=False))
    with (DATA / "runs.jsonl").open("a") as fh:
        fh.write(json.dumps(log, ensure_ascii=False) + "\n")
    print(json.dumps(log, ensure_ascii=False))


if __name__ == "__main__":
    main()
