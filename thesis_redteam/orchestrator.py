"""M2 agent orchestration with explicit budgets and an audit trace."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .agents import planner, redteam, reporter
from .extract import extract_fields
from .llm import FakeProvider, LLMProvider, provider_from_env
from .facts import attach_layers
from .pit import filter_filings, parse_sgml_acceptance
from .prices import PriceProvider, TiingoProvider, market_cap_for_fields
from .report import build_report, inconclusive_evaluation, write_reports
from .sec_client import SecClient, SecNetworkError
from .templates import evaluate, supported_templates
from .verifier import overall_verdict, run_extra_checks


MAX_LLM_CALLS = 4
MAX_SEC_REQUESTS = 12
MAX_WALL_SECONDS = 180.0


class BudgetStop(RuntimeError):
    pass


@dataclass
class Budget:
    max_llm_calls: int = MAX_LLM_CALLS
    max_sec_requests: int = MAX_SEC_REQUESTS
    max_wall_seconds: float = MAX_WALL_SECONDS
    started: float = field(default_factory=time.perf_counter)
    llm_calls: int = 0
    sec_requests: int = 0
    stop_reason: str | None = None

    @property
    def elapsed_seconds(self) -> float:
        return time.perf_counter() - self.started

    def check_wall(self) -> None:
        if self.elapsed_seconds >= self.max_wall_seconds:
            self.stop_reason = "wall_seconds"
            raise BudgetStop("BUDGET_STOP")

    def allow_llm(self) -> None:
        self.check_wall()
        if self.llm_calls >= self.max_llm_calls:
            self.stop_reason = "llm_calls"
            raise BudgetStop("BUDGET_STOP")
        self.llm_calls += 1

    def allow_sec(self) -> None:
        self.check_wall()
        if self.sec_requests >= self.max_sec_requests:
            self.stop_reason = "sec_requests"
            raise BudgetStop("BUDGET_STOP")
        self.sec_requests += 1


def _model(stage: str, fallback: str = "sonnet") -> str:
    names = (
        f"THESIS_REDTEAM_{stage.upper()}_MODEL",
        f"{stage.upper()}_MODEL",
        "THESIS_REDTEAM_MODEL",
    )
    return next((os.environ[name] for name in names if os.environ.get(name)), fallback)


def _provider_stage(provider: Any, stage: str) -> None:
    setter = getattr(provider, "set_stage", None)
    if setter:
        setter(stage)


def _provider_records(provider: Any, start: int) -> list[dict[str, Any]]:
    records = getattr(provider, "records", [])
    return list(records[start:]) if isinstance(records, list) else []


def _usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    input_tokens = [item.get("usage", {}).get("input_tokens") for item in records]
    output_tokens = [item.get("usage", {}).get("output_tokens") for item in records]
    return {
        "calls": len(records),
        "input_tokens": sum(value for value in input_tokens if isinstance(value, (int, float))),
        "output_tokens": sum(value for value in output_tokens if isinstance(value, (int, float))),
        "durations_ms": [round(float(item.get("duration_ms", 0.0)), 3) for item in records],
    }


def _call_stage(
    *,
    stage: str,
    budget: Budget,
    provider: LLMProvider,
    operation: Callable[[], dict[str, Any]],
    trace: list[dict[str, Any]],
    provider_stage: str | None = None,
) -> dict[str, Any] | None:
    started = time.perf_counter()
    decisions: list[str] = []
    record_start = len(getattr(provider, "records", []))
    result: dict[str, Any] | None = None
    try:
        for attempt in range(2):
            budget.allow_llm()
            _provider_stage(provider, provider_stage or stage)
            try:
                result = operation()
                decisions.append("accepted" if attempt == 0 else "accepted_after_schema_retry")
                break
            except Exception as exc:
                if attempt == 0:
                    decisions.append(f"schema_retry:{type(exc).__name__}")
                    continue
                decisions.append("INCONCLUSIVE(llm_schema_error)")
    except BudgetStop:
        decisions.append("BUDGET_STOP")
        raise
    finally:
        trace.append(
            {
                "stage": stage,
                "ms": round((time.perf_counter() - started) * 1000, 3),
                "llm_usage": _usage(_provider_records(provider, record_start)),
                "decisions": decisions,
            }
        )
    return result


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


def _load_fixture(fixture_dir: Path, ticker: str) -> dict[str, Any]:
    path = fixture_dir / f"{ticker.upper()}.json"
    if not path.is_file():
        raise FileNotFoundError(f"fixture not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _rows_from_recent(recent: dict[str, list[Any]]) -> list[dict[str, Any]]:
    length = max((len(values) for values in recent.values()), default=0)
    return [{key: values[index] for key, values in recent.items() if index < len(values)} for index in range(length)]


def _ticker_rows(ticker_map: dict[str, Any]) -> list[dict[str, Any]]:
    values = ticker_map.values() if isinstance(ticker_map, dict) else ticker_map
    return [row for row in values if isinstance(row, dict)]


def _collect_evidence(
    *,
    ticker: str,
    template: str,
    asof: str,
    offline: bool,
    fixture_dir: Path,
    cik: str | int | None,
    budget: Budget,
    price_provider: PriceProvider | None,
    no_prices: bool,
    criteria: str = "default",
) -> dict[str, Any]:
    if offline:
        client = SecClient(cache_dir=Path("cache"), offline=True, fixture_dir=fixture_dir)
        case = _load_fixture(fixture_dir, ticker)
        filings = case["filings"]
        raw_companyfacts = case["companyfacts"]
        resolved_cik = str(cik if cik is not None else case["cik"])
        sic = case.get("sic")
        primary_document = case.get("primary_document")
        receipts = _fixture_receipts(case)
    else:
        client = SecClient(cache_dir=Path("cache"), receipts_path=Path("cache") / "receipts.jsonl", offline=False)

        def get_json(url: str) -> dict[str, Any]:
            budget.allow_sec()
            return client.get_json(url)

        def get_bytes(url: str) -> bytes:
            budget.allow_sec()
            return client.get(url)

        try:
            resolved_cik = str(cik) if cik is not None else None
            if resolved_cik is None:
                ticker_map = get_json("https://www.sec.gov/files/company_tickers.json")
                matching = [row for row in _ticker_rows(ticker_map) if str(row.get("ticker", "")).upper() == ticker.upper()]
                if not matching:
                    raise ValueError(f"ticker not found in SEC ticker map: {ticker}")
                resolved_cik = str(matching[0].get("cik_str", matching[0].get("cik")))
            submissions = get_json(f"https://data.sec.gov/submissions/CIK{int(resolved_cik):010d}.json")
            sic = submissions.get("sic")
            recent = submissions.get("filings", {}).get("recent", {})
            filings = _rows_from_recent(recent)
            raw_companyfacts = get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{int(resolved_cik):010d}.json")
            selected = sorted(filter_filings(filings, asof), key=lambda row: row.get("filing_date") or "", reverse=True)
            primary_document = selected[0].get("primaryDocument") if selected else None
            if selected:
                selected_filing = selected[0]
                acc = selected_filing["accession"]
                index_data = get_json(f"https://www.sec.gov/Archives/edgar/data/{int(resolved_cik)}/{acc.replace('-', '')}/index.json")
                if not any(selected_filing.get(name) for name in ("acceptanceDateTime", "acceptance_datetime", "sgml_text", "submission_text")):
                    sgml_text = get_bytes(f"https://www.sec.gov/Archives/edgar/data/{int(resolved_cik)}/{acc.replace('-', '')}/{acc}-index.htm").decode("utf-8", errors="replace")
                    acceptance = parse_sgml_acceptance(sgml_text)
                    if acceptance:
                        selected_filing = dict(selected_filing, sgml_text=sgml_text, acceptance_datetime=acceptance)
                        filings = [selected_filing if (row.get("accession") or row.get("accessionNumber")) == acc else row for row in filings]
                        selected = sorted(filter_filings(filings, asof), key=lambda row: row.get("filing_date") or "", reverse=True)
                        primary_document = selected[0].get("primaryDocument") if selected else None
                if primary_document is None and isinstance(index_data, dict):
                    items = index_data.get("directory", {}).get("item", [])
                    names = [item.get("name") for item in items if isinstance(item, dict)]
                    primary_document = next((name for name in names if name and name.lower().endswith((".htm", ".html")) and "index" not in name.lower()), None)
                # Primary HTML is loaded per monetary input by the advisory check.
            receipts_path = Path("cache") / "receipts.jsonl"
            receipts = [json.loads(line) for line in receipts_path.read_text(encoding="utf-8").splitlines() if line.strip()] if receipts_path.exists() else []
            case = {"sic": sic}
        except SecNetworkError:
            raise
    pit_filings = filter_filings(filings, asof)
    receipt_path = client.receipts_path
    receipt_offset = receipt_path.stat().st_size if receipt_path.exists() else 0
    fields = extract_fields(raw_companyfacts, filings, asof=asof, cik=resolved_cik,
                            sec_client=client, before_fetch=None if offline else budget.allow_sec)
    if receipt_path.exists():
        with receipt_path.open(encoding="utf-8") as stream:
            stream.seek(receipt_offset)
            receipts.extend(json.loads(line) for line in stream if line.strip())
    no_prices = no_prices or criteria == "none"
    market_cap = None if no_prices else (case.get("market_cap") if offline else None)
    price_info: dict[str, Any] | None = None
    if not no_prices and market_cap in (None, "") and (price_provider is not None or not offline):
        provider = price_provider or TiingoProvider(cache_dir=Path("cache") / "prices")
        market_cap, price_info = market_cap_for_fields(fields, ticker=ticker, provider=provider)
        if price_info and price_info.get("price_receipt"):
            receipts.append(price_info["price_receipt"])
    return {
        "case": case,
        "companyfacts": raw_companyfacts,
        "cik": resolved_cik,
        "filings": filings,
        "pit_filings": pit_filings,
        "fields": fields,
        "market_cap": market_cap,
        "price_info": price_info,
        "sic": sic,
        "receipts": receipts,
        "primary_document": primary_document,
        "sec_client": client,
    }


def default_fake_responses(template: str = "shareholder_return") -> dict[str, dict[str, Any]]:
    if template == "earnings_quality":
        return {
            "planner": {
                "template": "earnings_quality",
                "claims": [
                    {"id": "E1", "statement_ko": "이익이 개선된다.", "metric": "TTM net income YoY", "required_fields": ["net_income"], "falsify_if": "YoY 증가가 아님"},
                    {"id": "E2", "statement_ko": "이익이 현금으로 뒷받침된다.", "metric": "CFO / net income", "required_fields": ["cfo", "net_income"], "falsify_if": "비율이 0.8 미만"},
                    {"id": "E3", "statement_ko": "발생액이 악화되지 않는다.", "metric": "accruals / average assets", "required_fields": ["net_income", "cfo", "assets"], "falsify_if": "YoY 2%p 초과 증가"},
                    {"id": "E4", "statement_ko": "CFO 개선이 운전자본 회수에만 의존하지 않는다.", "metric": "working capital contribution", "required_fields": ["cfo", "accounts_receivable_change", "inventory_change", "accounts_payable_change"], "falsify_if": "증가분의 70% 초과"},
                    {"id": "E5", "statement_ko": "이익 개선이 일회성 이익에 의존하지 않는다.", "metric": "YoY increase in positive one-off contributions / net income increase", "required_fields": ["net_income", "one_off_items"], "falsify_if": "증가분의 30% 초과"},
                ],
                "assumptions_ko": "기준일 전에 공개된 동일 filing 비교열과 USD XBRL 값을 사용한다.",
                "unsupported_claims": [],
            },
            "redteam": {
                "challenges": [
                    {"counterexample_id": "kic_B01_earnings_quality", "risk_ko": "현금품질 가설이 기존 발생액·수익성 팩터와 중복될 수 있다.", "extra_check": "accrual_spike", "severity": "medium"},
                    {"counterexample_id": "trap_PAG_working_capital_release", "risk_ko": "CFO 증가가 운전자본 회수에 치우칠 수 있다.", "extra_check": "working_capital_release", "severity": "high"},
                    {"counterexample_id": "trap_ARW_working_capital_contribution", "risk_ko": "CFO 변화는 운전자본 행과 다른 상쇄 항목을 분리해 읽어야 한다.", "extra_check": "working_capital_release", "severity": "medium"},
                ],
                "plan_flaws": [],
            },
            "reporter": {
                "summary_ko": "검증 코어는 이익 증가, CFO 동반, 발생액, 운전자본, 일회성 항목을 결정론적으로 확인했습니다.",
                "conclusion_ko": "결론: 검증한 청구항 모두 원문 숫자로 반증되지 않았다(참임을 보증하지 않음)",
            },
        }
    return {
        "planner": {
            "template": "shareholder_return",
            "claims": [
                {"id": "C1", "statement_ko": "순현금 주주환원이 양수다.", "metric": "net buyback", "required_fields": ["repurchase", "common_issuance"], "falsify_if": "발행 차감 후 순환원이 0 이하"},
                {"id": "C2", "statement_ko": "기말 보통주 수가 감소한다.", "metric": "period-end shares YoY", "required_fields": ["shares"], "falsify_if": "2% 이상 감소하지 않음"},
                {"id": "C3", "statement_ko": "현금흐름이 매입을 충당한다.", "metric": "CFO minus capex", "required_fields": ["cfo", "capex", "repurchase"], "falsify_if": "CFO-capex가 매입보다 작음"},
                {"id": "C4", "statement_ko": "부채성 환원이 과하지 않다.", "metric": "net debt/assets delta", "required_fields": ["interest_bearing_debt", "cash", "assets"], "falsify_if": "변화가 2%p 초과"},
            ],
            "assumptions_ko": "기준일 전에 공개된 동일 10-K와 XBRL 값을 사용한다.",
            "unsupported_claims": [],
        },
        "redteam": {
            "challenges": [
                {"counterexample_id": "cash_TA", "risk_ko": "현금 여유가 있어도 common 발행액이 매입을 상쇄할 수 있다.", "extra_check": "net_issuance_offsets_buyback", "severity": "high"},
                {"counterexample_id": "trap_AAPL_period_end_shares", "risk_ko": "EPS용 가중평균 주식수를 기말 outstanding로 오인할 수 있다.", "extra_check": "period_end_shares_not_weighted", "severity": "medium"},
                {"counterexample_id": "trap_AAPL_debt_rise", "risk_ko": "매입이 부채 증가로 자금 조달됐을 수 있다.", "extra_check": "debt_funded_return", "severity": "high"},
            ],
            "plan_flaws": [],
        },
        "reporter": {
            "summary_ko": "검증 코어는 각 청구항을 원문 입력과 결정론적 계산으로 판정했습니다. 레드팀은 발행 현금이 매입을 상쇄하는 위험을 확인했습니다. 최종 결과는 verifier 판정과 일치합니다.",
            "conclusion_ko": "결론: 검증한 청구항 모두 원문 숫자로 반증되지 않았다(참임을 보증하지 않음)",
        },
    }


def orchestrate(
    *,
    thesis: str,
    ticker: str,
    asof: str,
    offline: bool = False,
    fake_llm: bool = False,
    provider: LLMProvider | None = None,
    fixture_dir: str | Path | None = None,
    cik: str | int | None = None,
    max_llm_calls: int = MAX_LLM_CALLS,
    max_sec_requests: int = MAX_SEC_REQUESTS,
    max_wall_seconds: float = MAX_WALL_SECONDS,
    price_provider: PriceProvider | None = None,
    no_prices: bool = False,
    template: str = "shareholder_return",
    criteria: str = "default",
    memory: str = "two-pass",
) -> dict[str, Any]:
    """Run planner → evidence → redteam → re-collect → verifier → reporter."""

    fixture_root = Path(fixture_dir) if fixture_dir else Path(__file__).resolve().parents[1] / "tests" / "fixtures"
    if template not in supported_templates():
        raise ValueError(f"unsupported template: {template}")
    if criteria not in {"default", "none"}:
        raise ValueError(f"unsupported criteria: {criteria}")
    if memory not in {"two-pass", "on", "off"}:
        raise ValueError(f"unsupported memory mode: {memory}")
    if fake_llm and provider is None:
        responses = default_fake_responses(template)
        responses["planner"] = lambda **kw: planner.literal_plan(json.loads(kw["user"])["thesis"], preferred_template=template)
        provider = FakeProvider(responses)
    llm = provider or provider_from_env()
    budget = Budget(max_llm_calls=max_llm_calls, max_sec_requests=max_sec_requests, max_wall_seconds=max_wall_seconds)
    trace: list[dict[str, Any]] = []
    plan_result: dict[str, Any] | None = None
    redteam_result: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    evaluation: dict[str, Any]
    reporter_result: dict[str, Any]
    status = "COMPLETE"
    stop_reason: str | None = None

    try:
        plan_result = _call_stage(
            stage="planner",
            budget=budget,
            provider=llm,
            operation=lambda: planner.plan(
                thesis=thesis,
                ticker=ticker,
                asof=asof,
                templates=list(supported_templates()),
                provider=llm,
                model=_model("planner"),
            ),
            trace=trace,
        )
        if plan_result is None:
            stop_reason = "llm_schema_error"
        if plan_result is not None and plan_result["template"] == "unsupported":
            status = "UNSUPPORTED"
            stop_reason = "unsupported_claims"
        elif plan_result is not None:
            template = plan_result["template"]
            started = time.perf_counter()
            try:
                evidence = _collect_evidence(
                    ticker=ticker,
                    template=template,
                    asof=asof,
                    offline=offline,
                    fixture_dir=fixture_root,
                    cik=cik,
                    budget=budget,
                    price_provider=price_provider,
                    no_prices=no_prices,
                    criteria=criteria,
                )
                trace.append({"stage": "evidence", "ms": round((time.perf_counter() - started) * 1000, 3), "llm_usage": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "durations_ms": []}, "decisions": [f"sec_requests:{budget.sec_requests}"]})
            except BudgetStop:
                trace.append({"stage": "evidence", "ms": round((time.perf_counter() - started) * 1000, 3), "llm_usage": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "durations_ms": []}, "decisions": ["BUDGET_STOP"]})
                raise
            except (SecNetworkError, FileNotFoundError, ValueError) as exc:
                trace.append({"stage": "evidence", "ms": round((time.perf_counter() - started) * 1000, 3), "llm_usage": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "durations_ms": []}, "decisions": [f"INCONCLUSIVE:{type(exc).__name__}"]})
                evidence = None
            if evidence is not None:
                redteam_result = _call_stage(
                    stage="redteam",
                    budget=budget,
                    provider=llm,
                    operation=lambda: redteam.red_team(
                        thesis=thesis,
                        plan_result=plan_result or {},
                        template=template,
                        provider=llm,
                        model=_model("redteam"),
                        subject_ticker=ticker,
                        subject_cik=evidence["cik"],
                        memory="off" if memory == "two-pass" else memory,
                    ),
                    trace=trace,
                )
                if redteam_result is not None and trace and trace[-1].get("stage") == "redteam":
                    excluded = list((redteam_result.get("retrieval") or {}).get("excluded_same_subject", []))
                    trace[-1]["retrieval.excluded_same_subject"] = excluded
                    trace[-1]["pass"] = "legacy" if memory == "on" else "독립 감사"
                if memory == "two-pass" and redteam_result is not None:
                    # Assign pass 1 before attempting enrichment so budget/schema
                    # failures cannot erase its findings from the final report.
                    redteam_result["retrieval"]["memory"] = memory
                    enriched = _call_stage(
                        stage="redteam_enrich", provider_stage="redteam",
                        budget=budget, provider=llm,
                        operation=lambda: redteam.reinforce(
                            thesis=thesis, plan_result=plan_result or {},
                            audit_result=redteam_result, template=template,
                            provider=llm, model=_model("redteam"),
                            subject_ticker=ticker, subject_cik=evidence["cik"],
                        ),
                        trace=trace,
                    )
                    trace[-1]["pass"] = "근거 보강"
                    if enriched is not None:
                        redteam_result = enriched
                        trace[-1]["retrieval.excluded_same_subject"] = list(enriched["retrieval"]["excluded_same_subject"])
                names = [challenge.get("extra_check") for challenge in (redteam_result or {}).get("challenges", []) if challenge.get("extra_check")]
                missing = [name for name in names if _missing_for_check(name, evidence["fields"])]
                if missing:
                    started = time.perf_counter()
                    try:
                        budget.check_wall()
                        evidence["fields"] = extract_fields(
                            evidence["companyfacts"], evidence["filings"], asof=asof, cik=evidence["cik"],
                            sec_client=evidence["sec_client"], before_fetch=None if offline else budget.allow_sec,
                        )
                        trace.append({"stage": "recollect", "ms": round((time.perf_counter() - started) * 1000, 3), "llm_usage": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "durations_ms": []}, "decisions": [f"attempted:{','.join(missing)}"]})
                    except BudgetStop:
                        trace.append({"stage": "recollect", "ms": round((time.perf_counter() - started) * 1000, 3), "llm_usage": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "durations_ms": []}, "decisions": ["BUDGET_STOP"]})
                        raise
        if evidence is not None and criteria == "default":
            verifier_started = time.perf_counter()
            evaluation = evaluate(
                template,
                evidence["fields"],
                market_cap=evidence.get("market_cap"),
                sic=evidence.get("sic"),
                price_info=evidence.get("price_info"),
                filings=evidence.get("filings"),
                asof=asof,
            )
            names = [challenge.get("extra_check") for challenge in (redteam_result or {}).get("challenges", []) if challenge.get("extra_check")]
            names = list(dict.fromkeys([*names, "xbrl_html_mismatch"]))
            receipt_path = evidence["sec_client"].receipts_path
            receipt_offset = receipt_path.stat().st_size if receipt_path.exists() else 0
            extras = run_extra_checks(
                names, evidence["fields"], evaluation=evaluation, filings=evidence["pit_filings"],
                sec_client=evidence["sec_client"], before_fetch=None if offline else budget.allow_sec,
            )
            evaluation["extra_checks"] = extras
            if receipt_path.exists():
                with receipt_path.open("rb") as stream:
                    stream.seek(receipt_offset)
                    evidence["receipts"].extend(json.loads(line) for line in stream if line.strip())
            if evaluation["verdict"] != "NOT_APPLICABLE":
                evaluation["verdict"] = overall_verdict([{"status": evaluation["verdict"]}, *extras])
            trace.append({"stage": "verifier", "ms": round((time.perf_counter() - verifier_started) * 1000, 3), "llm_usage": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "durations_ms": []}, "decisions": [f"extra_checks:{len(extras)}"]})
        else:
            evaluation = inconclusive_evaluation(stop_reason or "증거 수집 또는 planner가 완료되지 않았습니다.", template=template)
            if status == "UNSUPPORTED" or criteria == "none":
                evaluation["claims"] = []
    except BudgetStop:
        status = "BUDGET_STOP"
        stop_reason = budget.stop_reason or "budget"
        evaluation = evaluation if "evaluation" in locals() else inconclusive_evaluation("BUDGET_STOP", template=template)

    attach_layers(evaluation, fields=(evidence or {}).get("fields", {}),
                  claims=(plan_result or {}).get("fact_claims", []), template=template,
                  criteria=criteria, unsupported_claims=(plan_result or {}).get("unsupported_claims", []))

    if status != "BUDGET_STOP" and evidence is not None and plan_result is not None:
        try:
            reporter_result = _call_stage(
                stage="reporter",
                budget=budget,
                provider=llm,
                operation=lambda: reporter.report(verifier_result=evaluation, provider=llm, model=_model("reporter")),
                trace=trace,
            ) or reporter.fallback_report(evaluation, reason="llm_schema_error")
        except BudgetStop:
            status = "BUDGET_STOP"
            stop_reason = budget.stop_reason or "budget"
            reporter_result = reporter.fallback_report(evaluation)
    else:
        reporter_result = reporter.fallback_report(evaluation, reason=stop_reason)

    total_ms = budget.elapsed_seconds * 1000
    trace.append({"stage": "total", "ms": round(total_ms, 3), "llm_usage": _usage(getattr(llm, "records", [])), "llm_calls": budget.llm_calls, "max_llm_calls": budget.max_llm_calls, "decisions": [status] + ([f"stop_reason:{stop_reason}"] if stop_reason else [])})
    for entry in trace:
        entry["memory"] = memory
    timing_ms = {entry["stage"]: entry["ms"] for entry in trace}
    if evidence is None:
        resolved_cik = str(cik or "")
        receipts: list[dict[str, Any]] = []
        primary_document = None
    else:
        resolved_cik = evidence["cik"]
        receipts = evidence["receipts"]
        primary_document = evidence["primary_document"]
    report = build_report(
        ticker=ticker,
        cik=resolved_cik,
        template=template,
        asof=asof,
        evaluation=evaluation,
        receipts=receipts,
        timing_ms=timing_ms,
        primary_document=primary_document,
        filings=evidence.get("filings") if evidence is not None else None,
        thesis=thesis,
        plan=plan_result,
        redteam=redteam_result,
        reporter=reporter_result,
        trace=trace,
        status=status,
        stop_reason=stop_reason,
    )
    return report


def _missing_for_check(name: str, fields: dict[str, Any]) -> bool:
    requirements = {
        "net_issuance_offsets_buyback": ("repurchase", "common_issuance"),
        "period_end_shares_not_weighted": ("shares",),
        "debt_funded_return": ("interest_bearing_debt", "cash", "assets"),
        "fcf_covers_buyback": ("cfo", "capex", "repurchase"),
        "working_capital_release": ("cfo",),
        "one_off_gain": ("net_income",),
        "accrual_spike": ("net_income", "cfo", "assets"),
    }
    names = requirements.get(name, ())
    return bool(names) and any(not fields.get(key) for key in names)


def run_agent(**kwargs: Any) -> tuple[Path, Path, dict[str, Any]]:
    out_dir = kwargs.pop("out_dir", "runs/")
    report = orchestrate(**kwargs)
    json_path, html_path = write_reports(report, out_dir)
    return json_path, html_path, report


run = orchestrate

