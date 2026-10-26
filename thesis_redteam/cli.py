"""Command-line entry point for deterministic M1 runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .extract import extract_fields
from .facts import attach_layers
from .orchestrator import run_agent
from .pit import filter_filings, parse_sgml_acceptance
from .prices import PriceProvider, TiingoProvider, market_cap_for_fields
from .report import build_report, inconclusive_evaluation, render_html, write_reports
from .sec_client import SecClient, SecNetworkError
from .templates import evaluate, supported_templates
from .verifier import run_extra_checks


HISTORICAL_CIKS = Path(__file__).resolve().parent / "data" / "historical_ciks.json"


def _fixture_path(fixture_dir: Path, ticker: str) -> Path:
    path = fixture_dir / f"{ticker.upper()}.json"
    if not path.is_file():
        raise FileNotFoundError(f"fixture not found: {path}")
    return path


def _fixture_receipts(case: dict[str, Any]) -> list[dict[str, Any]]:
    receipts = []
    for name in ("filings", "companyfacts"):
        body = json.dumps(case.get(name, {}), ensure_ascii=False, sort_keys=True).encode("utf-8")
        receipts.append(
            {
                "url_key": hashlib.sha256(f"fixture:{case['ticker']}:{name}".encode()).hexdigest(),
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
                "fetched_at": case.get("fixture_fetched_at", datetime.now(timezone.utc).isoformat()),
                "source": "fixture",
            }
        )
    return receipts


def _read_receipts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _historical_cik(ticker: str) -> str | None:
    if not HISTORICAL_CIKS.is_file():
        return None
    aliases = json.loads(HISTORICAL_CIKS.read_text(encoding="utf-8"))
    value = aliases.get(ticker.upper())
    return str(int(value)) if value is not None else None


def _rows_from_recent(recent: dict[str, list[Any]]) -> list[dict[str, Any]]:
    if not recent:
        return []
    length = max((len(values) for values in recent.values()), default=0)
    return [{key: values[index] for key, values in recent.items() if index < len(values)} for index in range(length)]


def _ticker_rows(ticker_map: dict[str, Any]) -> list[dict[str, Any]]:
    rows = ticker_map.values() if isinstance(ticker_map, dict) else ticker_map
    return [row for row in rows if isinstance(row, dict)]


def _inconclusive_report(
    *,
    ticker: str,
    cik: str | int | None,
    template: str,
    asof: str,
    error: SecNetworkError,
    receipts: list[dict[str, Any]],
    timing_ms: dict[str, float],
    out_dir: str | Path,
    criteria: str = "default",
) -> tuple[Path, Path, dict[str, Any]]:
    reason = f"자료 수집 실패: {error.url} {error.status}"
    evaluation = inconclusive_evaluation(reason, template=template, url=error.url, status=error.status)
    attach_layers(evaluation, fields={}, claims=[], template=template, criteria=criteria)
    report = build_report(
        ticker=ticker,
        cik=cik or "",
        template=template,
        asof=asof,
        evaluation=evaluation,
        receipts=receipts,
        timing_ms=timing_ms,
    )
    return (*write_reports(report, out_dir), report)


def run_case(
    *,
    ticker: str,
    template: str,
    asof: str,
    offline: bool,
    out_dir: str | Path,
    fixture_dir: str | Path | None = None,
    cik: str | int | None = None,
    price_provider: PriceProvider | None = None,
    no_prices: bool = False,
    criteria: str = "default",
    fact_claims: list[dict[str, Any]] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    if template not in supported_templates():
        raise ValueError(f"unsupported template: {template}")
    if criteria not in {"default", "none"}:
        raise ValueError(f"unsupported criteria: {criteria}")
    stage_times: dict[str, float] = {}
    run_started = time.perf_counter()
    fixture_root = Path(fixture_dir) if fixture_dir else Path(__file__).resolve().parents[1] / "tests" / "fixtures"
    started = time.perf_counter()
    case: dict[str, Any] = {}
    sic: Any | None = None
    client: SecClient | None = None
    if offline:
        client = SecClient(cache_dir=Path("cache"), offline=True, fixture_dir=fixture_root)
        case = json.loads(_fixture_path(fixture_root, ticker).read_text(encoding="utf-8"))
        filings = case["filings"]
        raw_companyfacts = case["companyfacts"]
        cik = str(cik if cik is not None else case["cik"])
        sic = case.get("sic")
        primary_document = case.get("primary_document")
        receipts = _fixture_receipts(case)
    else:
        client = SecClient(cache_dir=Path("cache"), receipts_path=Path("cache") / "receipts.jsonl", offline=False)
        # The receipts log is shared by every run; a report must only carry rows from its own run.
        receipt_start = len(_read_receipts(client.receipts_path))
        try:
            cik = str(cik) if cik is not None else _historical_cik(ticker)
            if cik is None:
                ticker_rows = client.ticker_map()
                matching = [
                    row
                    for row in _ticker_rows(ticker_rows)
                    if str(row.get("ticker", "")).upper() == ticker.upper()
                ]
                if not matching:
                    raise ValueError(f"ticker not found in SEC ticker map: {ticker}")
                cik = str(matching[0].get("cik_str", matching[0].get("cik")))
            submissions = client.submissions(cik)
            sic = submissions.get("sic")
            recent = submissions.get("filings", {}).get("recent", {})
            filings = _rows_from_recent(recent)
            raw_companyfacts = client.companyfacts(cik)
            pit_filings = filter_filings(filings, asof)
            selected = sorted(pit_filings, key=lambda row: row.get("filing_date") or "", reverse=True)
            primary_document = selected[0].get("primaryDocument") if selected else None
            if selected:
                selected_filing = selected[0]
                index_data = client.filing_index(cik, selected_filing["accession"])
                if not any(
                    selected_filing.get(name)
                    for name in ("acceptanceDateTime", "acceptance_datetime", "sgml_text", "submission_text")
                ):
                    sgml_text = client.filing_index_html(cik, selected_filing["accession"]).decode("utf-8", errors="replace")
                    acceptance = parse_sgml_acceptance(sgml_text)
                    if acceptance:
                        selected_filing = dict(selected_filing, sgml_text=sgml_text, acceptance_datetime=acceptance)
                        filings = [
                            selected_filing
                            if (row.get("accession") or row.get("accessionNumber")) == selected_filing.get("accession")
                            else row
                            for row in filings
                        ]
                        selected = sorted(filter_filings(filings, asof), key=lambda row: row.get("filing_date") or "", reverse=True)
                        primary_document = selected[0].get("primaryDocument") if selected else None
                if primary_document is None and isinstance(index_data, dict):
                    items = index_data.get("directory", {}).get("item", [])
                    names = [item.get("name") for item in items if isinstance(item, dict)]
                    primary_document = next(
                        (name for name in names if name and name.lower().endswith((".htm", ".html")) and "index" not in name.lower()),
                        None,
                    )
                # The advisory verifier fetches each input's own primary HTML.
                # An unavailable document must not abort required claims.
            receipts = _read_receipts(client.receipts_path)[receipt_start:]
        except SecNetworkError as error:
            stage_times["fetch_or_fixture"] = (time.perf_counter() - started) * 1000
            stage_times["total"] = (time.perf_counter() - run_started) * 1000
            return _inconclusive_report(
                ticker=ticker,
                cik=cik,
                template=template,
                asof=asof,
                error=error,
                receipts=_read_receipts(client.receipts_path),
                timing_ms=stage_times,
                out_dir=out_dir,
                criteria=criteria,
            )
    stage_times["fetch_or_fixture"] = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    pit_filings = filter_filings(filings, asof)
    stage_times["pit_filter"] = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    receipt_count = len(_read_receipts(client.receipts_path)) if client is not None else 0
    fields = extract_fields(raw_companyfacts, filings, asof=asof, cik=cik, sec_client=client)
    if client is not None:
        receipts.extend(_read_receipts(client.receipts_path)[receipt_count:])
    stage_times["extract"] = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    no_prices = no_prices or criteria == "none"
    market_cap = None if no_prices else (case.get("market_cap") if offline else None)
    price_info: dict[str, Any] | None = None
    if not no_prices and market_cap in (None, "") and (price_provider is not None or not offline):
        provider = price_provider or TiingoProvider(cache_dir=Path("cache") / "prices")
        market_cap, price_info = market_cap_for_fields(fields, ticker=ticker, provider=provider)
        if price_info and price_info.get("price_receipt"):
            receipts.append(price_info["price_receipt"])
    if criteria == "default":
        evaluation = evaluate(
            template,
            fields,
            market_cap=market_cap,
            sic=sic,
            price_info=price_info,
            filings=filings,
            asof=asof,
        )
        receipt_count = len(_read_receipts(client.receipts_path)) if client is not None else 0
        evaluation["extra_checks"] = run_extra_checks(
            ["xbrl_html_mismatch"], fields, evaluation=evaluation, filings=filings, sec_client=client,
        )
        if client is not None:
            receipts.extend(_read_receipts(client.receipts_path)[receipt_count:])
    else:
        evaluation = {"verdict": "INCONCLUSIVE", "claims": [], "extra_checks": []}
    stage_times["verify"] = (time.perf_counter() - started) * 1000
    stage_times["total"] = (time.perf_counter() - run_started) * 1000
    started = time.perf_counter()
    attach_layers(evaluation, fields=fields, claims=fact_claims or [], template=template, criteria=criteria)
    report = build_report(
        ticker=ticker,
        cik=cik,
        template=template,
        asof=asof,
        evaluation=evaluation,
        receipts=receipts,
        timing_ms=stage_times,
        primary_document=primary_document,
        filings=filings,
    )
    stage_times["report"] = (time.perf_counter() - started) * 1000
    stage_times["total"] = (time.perf_counter() - run_started) * 1000
    report["timing_ms"] = stage_times
    # write_reports serializes the report, so rewrite after adding report timing.
    paths = write_reports(report, out_dir)
    return paths[0], paths[1], report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m thesis_redteam")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run a deterministic thesis template")
    run.add_argument("--ticker", required=True)
    run.add_argument("--template", required=True, choices=list(supported_templates()))
    run.add_argument("--asof", required=True)
    run.add_argument("--offline", action="store_true")
    run.add_argument("--out", default="runs/")
    run.add_argument("--fixture-dir")
    run.add_argument("--cik", help="explicit SEC CIK; skips ticker and alias lookup")
    run.add_argument("--no-prices", action="store_true", help="skip market-price lookup; C1 may use the public-float bound")
    run.add_argument("--criteria", choices=["default", "none"], default="default", help="default: use user-approved research screening rules; none: facts only")
    agent = subparsers.add_parser("agent", help="run the M2 planner/red-team/verifier/reporter flow")
    agent.add_argument("--thesis", required=True)
    agent.add_argument("--ticker", required=True)
    agent.add_argument("--asof", required=True)
    agent.add_argument("--template", choices=list(supported_templates()), default="shareholder_return")
    agent.add_argument("--offline", action="store_true")
    agent.add_argument("--fake-llm", action="store_true", help="use deterministic responses; never invokes Claude")
    agent.add_argument("--memory", choices=["two-pass", "on", "off"], default="two-pass", help="two-pass: independent audit then memory enrichment; on: legacy single pass; off: independent audit only")
    agent.add_argument("--out", default="runs/")
    agent.add_argument("--fixture-dir")
    agent.add_argument("--cik", help="explicit SEC CIK")
    agent.add_argument("--no-prices", action="store_true", help="skip market-price lookup; C1 may use the public-float bound")
    agent.add_argument("--criteria", choices=["default", "none"], default="default", help="default: use user-approved research screening rules; none: facts only")
    render = subparsers.add_parser("render", help="rebuild HTML from a saved report JSON without running an agent")
    render.add_argument("--json", required=True, dest="json_path", help="saved report JSON path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        json_path, html_path, report = run_case(
            ticker=args.ticker,
            template=args.template,
            asof=args.asof,
            offline=args.offline,
            out_dir=args.out,
            fixture_dir=args.fixture_dir,
            cik=args.cik,
            no_prices=args.no_prices,
            criteria=args.criteria,
        )
        print(json.dumps({"verdict": report["verdict"], "fact_verdict": report["fact_verdict"], "json": str(json_path), "html": str(html_path)}, ensure_ascii=False))
        return 0
    if args.command == "agent":
        json_path, html_path, report = run_agent(
            thesis=args.thesis,
            ticker=args.ticker,
            asof=args.asof,
            template=args.template,
            offline=args.offline,
            fake_llm=args.fake_llm,
            memory=args.memory,
            out_dir=args.out,
            fixture_dir=args.fixture_dir,
            cik=args.cik,
            no_prices=args.no_prices,
            criteria=args.criteria,
        )
        print(json.dumps({"status": report["status"], "verdict": report["verdict"], "fact_verdict": report["fact_verdict"], "json": str(json_path), "html": str(html_path)}, ensure_ascii=False))
        return 0
    if args.command == "render":
        json_path = Path(args.json_path)
        report = json.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("saved report JSON must contain an object")
        html_path = json_path.with_suffix(".html")
        html_path.write_text(render_html(report), encoding="utf-8")
        note = "saved report predates leave-one-out" if not any(
            isinstance(item, dict) and "retrieval.excluded_same_subject" in item
            for item in report.get("trace", [])
        ) else None
        payload = {"json": str(json_path), "html": str(html_path)}
        if note:
            payload["note"] = note
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    return 2

