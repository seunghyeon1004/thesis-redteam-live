"""JSON and standalone Korean HTML verdict reports."""

from __future__ import annotations

import copy
import html
import json
import re
from pathlib import Path
from typing import Any


def filing_url(cik: str | int | None, accession: str | None, primary_document: str | None = None) -> str | None:
    if not cik or not accession:
        return None
    cik_number = str(int(cik))
    accession_no_dash = accession.replace("-", "")
    document = primary_document or f"{accession}-index.htm"
    return f"https://www.sec.gov/Archives/edgar/data/{cik_number}/{accession_no_dash}/{document}"


def _filing_primary_documents(filings: list[dict[str, Any]] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in filings or []:
        if not isinstance(row, dict) or row.get("form") not in {"10-K", "10-K/A", "10-Q", "10-Q/A"}:
            continue
        accession = row.get("accessionNumber") or row.get("accession")
        document = row.get("primaryDocument")
        if accession and document:
            result[str(accession).replace("-", "")] = str(document)
    return result


def _decorate(
    value: Any,
    *,
    primary_document: str | None = None,
    filing_documents: dict[str, str] | None = None,
) -> Any:
    if isinstance(value, list):
        return [
            _decorate(item, primary_document=primary_document, filing_documents=filing_documents)
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    result = {
        key: _decorate(item, primary_document=primary_document, filing_documents=filing_documents)
        for key, item in value.items()
    }
    if result.get("accession") and result.get("cik"):
        document = (
            filing_documents.get(str(result["accession"]).replace("-", ""))
            if filing_documents is not None
            else primary_document
        )
        result["filing_url"] = filing_url(result["cik"], result["accession"], document)
    return result


def build_report(
    *,
    ticker: str,
    cik: str | int,
    template: str,
    asof: str,
    evaluation: dict[str, Any],
    receipts: list[dict[str, Any]],
    timing_ms: dict[str, float],
    primary_document: str | None = None,
    filings: list[dict[str, Any]] | None = None,
    thesis: str | None = None,
    plan: dict[str, Any] | None = None,
    redteam: dict[str, Any] | None = None,
    reporter: dict[str, Any] | None = None,
    trace: list[dict[str, Any]] | None = None,
    status: str = "COMPLETE",
    stop_reason: str | None = None,
) -> dict[str, Any]:
    decorated = _decorate(
        copy.deepcopy(evaluation),
        primary_document=primary_document,
        filing_documents=_filing_primary_documents(filings) if filings is not None else None,
    )
    sources: dict[tuple[str, str], dict[str, Any]] = {}
    for claim in [*decorated.get("claims", []), *decorated.get("fact_claims", [])]:
        stack = [claim.get("inputs", {}), claim.get("outputs", {})]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if item.get("accession") and item.get("filing_url"):
                    sources[(str(item.get("accession")), str(item.get("filing_url")))] = {
                        "accession": item["accession"],
                        "filing_url": item["filing_url"],
                    }
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    reporter = reporter or {}
    flags: list[str] = []
    flags.extend(str(flag) for flag in decorated.get("flags", []) if flag)
    if reporter.get("reporter_numeric_hallucination"):
        flags.append("reporter_numeric_hallucination")
    if reporter.get("reporter_advice_language"):
        flags.append("reporter_advice_language")
    result = {
        "product": "가설 레드팀",
        "ticker": ticker.upper(),
        "cik": str(cik),
        "template": template,
        "asof": asof,
        "thesis": thesis or "",
        "verdict": decorated["verdict"],
        "claims": decorated["claims"],
        "fact_claims": decorated.get("fact_claims", []),
        "fact_verdict": decorated.get("fact_verdict", "INCONCLUSIVE"),
        "criteria": decorated.get("criteria", {}),
        "unsupported_claims": decorated.get("unsupported_claims", []),
        "scope_note_ko": decorated.get("scope_note_ko", ""),
        "extra_checks": decorated.get("extra_checks", []),
        "sources": sorted(sources.values(), key=lambda source: source["accession"]),
        "receipts": receipts,
        "timing_ms": timing_ms,
        "trace": trace or [],
        "status": status,
        "stop_reason": stop_reason,
        "plan": plan,
        "redteam": redteam,
        "reporter": reporter,
        "summary_ko": reporter.get("summary_ko", ""),
        "conclusion_ko": reporter.get("conclusion_ko", ""),
        "flags": flags,
        "reporter_numeric_hallucination": bool(reporter.get("reporter_numeric_hallucination")),
        "reporter_advice_language": bool(reporter.get("reporter_advice_language")),
    }
    return result


def inconclusive_evaluation(
    reason: str,
    *,
    template: str = "shareholder_return",
    url: str | None = None,
    status: Any | None = None,
) -> dict[str, Any]:
    """Return a stable report shape when data collection cannot finish."""
    inputs: dict[str, Any] = {}
    if url is not None:
        inputs["url"] = url
    if status is not None:
        inputs["status"] = status
    claim_ids = ("E1", "E2", "E3", "E4", "E5") if template == "earnings_quality" else ("C1", "C2", "C3", "C4")
    claims = [
        {
            "id": claim_id,
            "status": "INCONCLUSIVE",
            "inputs": dict(inputs),
            "outputs": {},
            "reason_ko": reason,
        }
        for claim_id in claim_ids
    ]
    return {"verdict": "INCONCLUSIVE", "claims": claims}


def _value_html(value: Any) -> str:
    if isinstance(value, list):
        return "<ul>" + "".join(f"<li>{_value_html(item)}</li>" for item in value) + "</ul>"
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            if key == "filing_url" and item:
                continue
            label = html.escape(str(key))
            text = _value_html(item)
            if value.get("filing_url") and key == "value":
                text = f'<a href="{html.escape(str(value["filing_url"]), quote=True)}">{text}</a>'
            parts.append(f"<span><b>{label}</b>: {text}</span>")
        return "<div class=\"kv\">" + "<br>".join(parts) + "</div>"
    return html.escape(str(value))


_DISPLAY_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_])[-+−]?\$?[\d][\d,]*(?:\.\d+)?(?:[BMK])?(?:%p|pp|%)?(?![A-Za-z0-9_])", re.IGNORECASE)


def _filing_url_from(value: Any) -> str | None:
    if isinstance(value, dict):
        if value.get("filing_url"):
            return str(value["filing_url"])
        for item in value.values():
            url = _filing_url_from(item)
            if url:
                return url
    elif isinstance(value, list):
        for item in value:
            url = _filing_url_from(item)
            if url:
                return url
    return None


def _linked_reason(reason: str, url: str | None) -> str:
    escaped = html.escape(reason)
    if not url:
        return escaped
    parts: list[str] = []
    last = 0
    for match in _DISPLAY_NUMBER_RE.finditer(escaped):
        parts.append(escaped[last:match.start()])
        token = match.group(0)
        parts.append(f'<a class="number-link" href="{html.escape(url, quote=True)}">{token}</a>')
        last = match.end()
    parts.append(escaped[last:])
    return "".join(parts)


CHECK_LABELS = {
    "net_issuance_offsets_buyback": "순발행 상쇄",
    "period_end_shares_not_weighted": "기말주식수 사용",
    "debt_funded_return": "부채조달 환원",
    "fcf_covers_buyback": "FCF 충당",
    "pit_acceptance_conflict": "공개시각 충돌",
    "missing_value_null": "누락값 보존",
    "xbrl_html_mismatch": "원문 대조",
    "working_capital_release": "운전자본 회수",
    "one_off_gain": "보조 점검",
    "accrual_spike": "발생액 급증",
}

TEMPLATE_LABELS = {
    "shareholder_return": "주주환원",
    "earnings_quality": "이익의 현금 뒷받침",
}

CLAIM_LABELS = {
    "C1": "순현금환원",
    "C2": "기말 주식수",
    "C3": "현금충당",
    "C4": "부채성 환원",
    "E1": "이익 개선",
    "E2": "현금 동반",
    "E3": "발생액 악화 아님",
    "E4": "운전자본 착시 점검",
    "E5": "보조 점검",
}


def _check_label(name: Any) -> str:
    value = str(name or "")
    return CHECK_LABELS.get(value.lower(), value)


def _merged_counterexamples(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge challenge and plan-flaw mentions into one card per counterexample."""

    redteam = report.get("redteam") or {}
    retrieved = {
        item.get("id"): item
        for item in redteam.get("retrieved_counterexamples", [])
        if isinstance(item, dict) and item.get("id")
    }
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def ensure(counterexample_id: Any) -> dict[str, Any]:
        key = str(counterexample_id or "")
        if key not in merged:
            order.append(key)
            merged[key] = {
                "id": key,
                "item": retrieved.get(key, {}),
                "challenges": [],
                "plan_flaws": [],
                "checks": [],
            }
        return merged[key]

    for challenge in redteam.get("challenges", []):
        if isinstance(challenge, dict) and challenge.get("counterexample_id"):
            record = ensure(challenge["counterexample_id"])
            record["challenges"].append(challenge)
            if challenge.get("extra_check"):
                record["checks"].append(challenge["extra_check"])
    for flaw in redteam.get("plan_flaws", []):
        if isinstance(flaw, dict) and flaw.get("counterexample_id"):
            record = ensure(flaw["counterexample_id"])
            record["plan_flaws"].append(flaw)
    return [merged[key] for key in order]


def _trap_cards(report: dict[str, Any]) -> str:
    cards: list[str] = []
    for record in _merged_counterexamples(report):
        item = record["item"]
        source = item.get("source_file", "출처 미상")
        checks = " · ".join(dict.fromkeys(_check_label(name) for name in record["checks"] if name))
        card_class = "trap-card flaw" if record["plan_flaws"] and not record["challenges"] else "trap-card"
        body: list[str] = []
        for challenge in record["challenges"]:
            trap = item.get("trap_ko") or challenge.get("risk_ko", "")
            body.append(f"<p><b>레드팀 지적</b> {html.escape(str(trap))}</p>")
        for flaw in record["plan_flaws"]:
            body.append(f"<p><b>초기 계획의 허점</b> {html.escape(str(flaw.get('flaw_ko', '')))}</p>")
        cards.append(
            f"<article class=\"{card_class}\">"
            f"<span class=\"eyebrow check-label\">{html.escape(checks or 'counterexample')}</span>"
            f"<h3>{html.escape(record['id'])}</h3>"
            + "".join(body)
            + f"<small>출처: {html.escape(str(source))}</small>"
            + "</article>"
        )
    return "".join(cards) or '<p class="muted">이번 실행에서 등록된 반례가 없습니다.</p>'


def _actual_metric_for_claim(claim: dict[str, Any]) -> str:
    fact_metrics = {
        "repurchase_positive": "기간 중 자사주 매입 > 0",
        "net_buyback_positive": "매입 − 보통주 발행 현금 > 0",
        "shares_decreased": "기말 주식수 변화 < 0",
        "net_income_increased": "동일 공시의 순이익 변화 > 0",
        "cfo_supports_income": f"CFO / 순이익 ≥ {claim.get('threshold') or '1'}",
    }
    if claim.get("check") in fact_metrics:
        return fact_metrics[claim["check"]]
    claim_id = str(claim.get("id", ""))
    text = " ".join(str(claim.get(key, "")) for key in ("statement_ko", "metric", "required_fields"))
    if claim_id == "E1":
        return "TTM 순이익 YoY > 0"
    if claim_id == "E2":
        return "TTM CFO / TTM 순이익 ≥ 0.8"
    if claim_id == "E3":
        return "발생액/평균 총자산 YoY 증가 ≤ 2%p"
    if claim_id == "E4":
        return "운전자본 기여 ≤ CFO 증가분의 70%"
    if claim_id == "E5":
        return "양의 일회성 이익 기여의 YoY 증가분 ≤ 순이익 증가분의 30%"
    if claim_id == "C1":
        return "순현금환원 = 매입 − 발행"
    if claim_id == "C2" or any(term in text for term in ("가중평균", "weighted", "주식수")):
        return "기말 실제 주식수 (CommonStockSharesOutstanding; period-end outstanding)"
    if claim_id == "C3":
        return "CFO − capex ≥ 매입"
    if claim_id == "C4":
        return "순부채/자산 변화 ≤ 2%p"
    if any(term in text for term in ("현금흐름", "FCF", "CFO", "capex", "자본적지출")):
        return "CFO − capex ≥ 매입"
    if any(term in text for term in ("부채", "debt", "순부채")):
        return "순부채/자산 변화 ≤ 2%p"
    return str(claim.get("metric") or "verifier가 등록한 원문 기반 지표")


def _demo_flow(report: dict[str, Any]) -> str:
    plan = report.get("plan") or {}
    claims = [claim for claim in plan.get("fact_claims", plan.get("claims", [])) if isinstance(claim, dict)]
    flawed_claim_ids = {
        str(flaw.get("claim_id"))
        for flaw in (report.get("redteam") or {}).get("plan_flaws", [])
        if isinstance(flaw, dict) and flaw.get("claim_id")
    }
    planner_items = "".join(
        "<article class=\"flow-item\">"
        f"<b>{html.escape(str(claim.get('id', '')))} · {html.escape(str(claim.get('statement_ko', '')))}</b>"
        f"<p class=\"metric{' flawed' if str(claim.get('id')) in flawed_claim_ids else ''}\">"
        f"지표: {html.escape(str(claim.get('metric') or claim.get('check', '')))}</p>"
        "</article>"
        for claim in claims
    ) or '<p class="muted">Planner 결과가 저장되어 있지 않습니다.</p>'
    actual_items = "".join(
        "<article class=\"flow-item\">"
        f"<b>{html.escape(str(claim.get('id', '')))}</b>"
        f"<p class=\"metric actual\">{html.escape(_actual_metric_for_claim(claim))}</p>"
        "</article>"
        for claim in claims
    ) or '<p class="muted">검산 지표가 저장되어 있지 않습니다.</p>'
    trace = report.get("trace") or []
    has_retrieval_trace = any("retrieval.excluded_same_subject" in item for item in trace if isinstance(item, dict))
    compatibility = ""
    if not has_retrieval_trace:
        compatibility = '<small class="compat-note">저장된 실행은 leave-one-out 적용 전 trace입니다. 이 렌더링은 저장된 JSON만 재사용했습니다.</small>'
    return (
        '<section class="demo-flow"><h2>검산 전 → 검산 후</h2>'
        '<div class="flow-grid">'
        f'<div class="flow-column"><h3>① 초기 계획(Planner)</h3>{planner_items}</div>'
        f'<div class="flow-column"><h3>② 레드팀 지적</h3><div class="trap-grid">{_trap_cards(report)}</div>'
        '<p class="leakage-note">검사 대상 기업의 과거 기록은 반례 검색에서 제외(누설 방지)</p>'
        f'{compatibility}</div>'
        f'<div class="flow-column"><h3>③ 실제 검산에 사용한 지표</h3>{actual_items}</div>'
        '</div></section>'
    )


def _timeline(report: dict[str, Any]) -> str:
    trace = report.get("trace") or []
    stage_names = ("planner", "evidence", "redteam", "recollect", "verifier", "reporter")
    by_stage = {str(item.get("stage")): item for item in trace if isinstance(item, dict) and item.get("stage")}
    timing = report.get("timing_ms") or {}
    stage_ms = {
        stage: float((by_stage.get(stage) or {}).get("ms", timing.get(stage, 0)) or 0)
        for stage in stage_names
    }
    total_item = by_stage.get("total") or {}
    total_ms = float(total_item.get("ms", timing.get("total", sum(stage_ms.values()))) or 0)
    total = max(total_ms, sum(stage_ms.values()), 1.0)
    rows = []
    for stage in stage_names:
        ms = stage_ms[stage]
        width = 0.0 if ms <= 0 else max(2.0, min(100.0, ms / total * 100))
        rows.append(
            f"<div class=\"timeline-row\" data-stage=\"{html.escape(stage)}\">"
            f"<div class=\"timeline-label\"><b>{html.escape(stage)}</b><span>{ms / 1000:.2f} s</span></div>"
            f"<div class=\"timeline-track\"><i style=\"width:{width:.2f}%\"></i></div>"
            "</div>"
        )
    usage = total_item.get("llm_usage") if isinstance(total_item, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    numeric_usages = [item.get("llm_usage", {}) for item in trace if isinstance(item, dict)]
    calls = usage.get("calls") if isinstance(usage.get("calls"), (int, float)) else sum(
        item.get("calls", 0) for item in numeric_usages if isinstance(item, dict) and isinstance(item.get("calls"), (int, float))
    )
    input_tokens = usage.get("input_tokens") if isinstance(usage.get("input_tokens"), (int, float)) else sum(
        item.get("input_tokens", 0) for item in numeric_usages if isinstance(item, dict) and isinstance(item.get("input_tokens"), (int, float))
    )
    output_tokens = usage.get("output_tokens") if isinstance(usage.get("output_tokens"), (int, float)) else sum(
        item.get("output_tokens", 0) for item in numeric_usages if isinstance(item, dict) and isinstance(item.get("output_tokens"), (int, float))
    )
    total_tokens = input_tokens + output_tokens
    return "".join(rows) + (
        f'<p class="timeline-total">총 소요시간 <b>{total_ms / 1000:.2f} s</b> · '
        f'LLM 호출 <b>{int(calls)}</b>회 · 총 토큰 <b>{int(total_tokens):,}</b> '
        f'(입력 {int(input_tokens):,} / 출력 {int(output_tokens):,})</p>'
    )


def render_html(report: dict[str, Any]) -> str:
    verdict = html.escape(report["verdict"])
    badge_class = verdict.lower()
    fact_rows = []
    for fact in report.get("fact_claims", []):
        fact_rows.append(
            '<tr>'
            f'<td>{html.escape(str(fact.get("id", "")))} · {html.escape(str(fact.get("statement_ko", "")))}</td>'
            f'<td>{html.escape(str(fact.get("status", "판정불가")))}</td>'
            f'<td>{_linked_reason(str(fact.get("reason_ko", "")), _filing_url_from(fact.get("inputs", {})))}'
            f'<details><summary>원공시 입력</summary>{_value_html(fact.get("inputs", {}))}</details>'
            f'<details><summary>계산</summary>{_value_html(fact.get("outputs", {}))}</details></td></tr>'
        )
    hints = []
    for check in report.get("extra_checks", []):
        for value in (check.get("outputs") or {}).get("values", []) or []:
            if value.get("search_hint"):
                hints.append(
                    f'<li>{html.escape(str(value.get("tag", "")))} = {html.escape(str(value.get("value", "")))} → '
                    f'원문에서 Cmd+F <code>{html.escape(value["search_hint"])}</code> (원문에서 1곳)</li>')
    hint_html = ('<p>원문에서 바로 찾기</p><ul>' + ''.join(dict.fromkeys(hints)) + '</ul>') if hints else ''
    unsupported = ''.join(f'<li>{html.escape(str(claim))}</li>' for claim in report.get("unsupported_claims", []))
    fact_section = (
        '<section><h2>① 원래 주장 사실 검증</h2><div class="panel">'
        f'<p>사실 검증 (원래 주장): <b>{html.escape(str(report.get("fact_verdict", "INCONCLUSIVE")))}</b></p>'
        f'<p>{html.escape(str(report.get("scope_note_ko", "")))}</p>'
        '<table><thead><tr><th>사실</th><th>판정</th><th>숫자 · 원공시 출처</th></tr></thead><tbody>'
        + ''.join(fact_rows) + '</tbody></table>' + hint_html
        + ('<p>공시로 검증할 수 없는 주장</p><ul>' + unsupported + '</ul>' if unsupported else '')
        + ('<p>검증 가능한 원래 사실 주장이 추출되지 않았습니다.</p>' if not fact_rows else '')
        + '</div></section>'
    )
    criteria = report.get("criteria") or {}
    criteria_note = html.escape(str(criteria.get("label_ko", "선별 조건"))) + ' · ' + html.escape(str(criteria.get("source", "")))
    rows = []
    for claim in report["claims"]:
        claim_url = _filing_url_from(claim.get("inputs", {}))
        claim_id = str(claim.get("id", ""))
        claim_label = CLAIM_LABELS.get(claim_id, claim_id)
        rows.append(
            "<tr>"
            f"<td>{html.escape(claim_id)} · {html.escape(claim_label)}</td>"
            f"<td><span class=\"badge {html.escape(claim['status'].lower())}\">{html.escape(claim['status'])}</span></td>"
            f"<td><div class=\"reason\">{_linked_reason(str(claim['reason_ko']), claim_url)}</div><details><summary>입력</summary>{_value_html(claim.get('inputs', {}))}</details><details><summary>출력</summary>{_value_html(claim.get('outputs', {}))}</details></td>"
            "</tr>"
        )
    extra_rows = []
    for check in report.get("extra_checks", []):
        outputs = check.get("outputs", {})
        counts = f" {outputs['found']}/{outputs['total']}" if "found" in outputs and "total" in outputs else ""
        if "strong_matches" in outputs:
            counts += f" (강한 일치 {outputs['strong_matches']})"
        advisory = " · 참고용" if check.get("advisory") else ""
        extra_rows.append(
            '<article class="flow-item">'
            f"<b>{html.escape(_check_label(check.get('check')))}{counts}{advisory}</b>"
            f'<span class="badge {html.escape(check["status"].lower())}">{html.escape(check["status"])}</span>'
            f'<p>{html.escape(str(check.get("reason_ko", "")))}</p>'
            f'<details><summary>출력</summary>{_value_html(outputs)}</details></article>'
        )
    extra_section = '<section><h2>추가 검사</h2><div class="panel">' + "".join(extra_rows) + '</div></section>' if extra_rows else ""
    receipt_rows = "".join(
        f"<tr><td>{html.escape(str(item.get('url_key', ''))[:16])}</td><td>{html.escape(str(item.get('sha256', '')))}</td><td>{item.get('bytes', '')}</td><td>{html.escape(str(item.get('fetched_at', '')))}</td></tr>"
        for item in report.get("receipts", [])
    )
    source_rows = "".join(
        f'<li><a href="{html.escape(source["filing_url"], quote=True)}">{html.escape(source["accession"])}</a></li>'
        for source in report.get("sources", [])
    )
    fact_label = html.escape(str(report.get("fact_verdict", "INCONCLUSIVE")))
    conclusion = report.get("conclusion_ko") or f"원래 주장 사실: {fact_label} · 선별 조건: {verdict}."
    flags = "".join(f'<span class="flag">{html.escape(str(flag))}</span>' for flag in report.get("flags", []))
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>가설 레드팀 · {html.escape(report['ticker'])}</title>
<style>
:root{{color-scheme:light;--ink:#172033;--muted:#667085;--line:#e5e7eb;--panel:#fff;--bg:#f5f7fb;--accent:#4f46e5}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",sans-serif;line-height:1.55;overflow-x:hidden}}main{{max-width:1100px;margin:0 auto;padding:32px 20px 64px}}.hero{{background:linear-gradient(135deg,#18213c,#35308a);color:#fff;border-radius:24px;padding:32px;box-shadow:0 18px 45px #1b205020}}.eyebrow{{font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;color:#98a2ff;font-weight:800;overflow-wrap:anywhere}}h1,h2,h3,p{{margin-top:0}}h1{{font-size:clamp(1.7rem,4vw,3rem);line-height:1.15;margin:8px 0 20px;overflow-wrap:anywhere}}h2{{font-size:1.2rem;margin:34px 0 12px}}.hero-meta{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;color:#ccd1ff;font-size:.92rem}}.hero-bottom{{display:flex;gap:20px;align-items:flex-end;justify-content:space-between;flex-wrap:wrap}}.hero-conclusion{{font-size:1.08rem;max-width:710px;margin:0;color:#f0f1ff}}.badge{{display:inline-flex;align-items:center;border-radius:999px;padding:8px 16px;font-weight:900;letter-spacing:.04em;white-space:nowrap}}.hero .badge{{font-size:1.5rem;padding:12px 22px;background:#fff;color:var(--ink)}}.badge.pass{{background:#d1fae5;color:#065f46}}.badge.fail{{background:#fee2e2;color:#991b1b}}.badge.inconclusive,.badge.warning{{background:#fef3c7;color:#92400e}}.panel{{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:20px;box-shadow:0 8px 24px #10182808}}.flow-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;align-items:stretch}}.flow-column{{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:18px;min-width:0}}.flow-column h3{{font-size:1rem;margin-bottom:14px}}.flow-item{{border:1px solid var(--line);border-radius:12px;padding:12px;margin:8px 0;background:#fff;overflow-wrap:anywhere}}.flow-item b{{display:block}}.metric{{font-size:.88rem;color:#667085;margin:8px 0 0;overflow-wrap:anywhere}}.metric.flawed{{color:#b42318;font-weight:900;background:#fff1f0;border-left:4px solid #f04438;padding:8px}}.metric.actual{{color:#065f46;background:#ecfdf3;border-left:4px solid #12b76a;padding:8px}}.leakage-note{{font-size:.78rem;color:#667085;margin:14px 0 0;overflow-wrap:anywhere}}.compat-note{{display:block;font-size:.75rem;color:#667085;margin-top:8px;overflow-wrap:anywhere}}.trap-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}}.trap-card{{border:1px solid #f0d9a8;background:#fffbeb;border-radius:14px;padding:16px;min-width:0;overflow-wrap:anywhere}}.trap-card.flaw{{background:#f5f3ff;border-color:#ddd6fe}}.trap-card h3{{font-size:1rem;margin:6px 0}}.trap-card p{{font-size:.92rem;margin-bottom:12px}}.trap-card small{{color:var(--muted);overflow-wrap:anywhere}}.muted{{color:var(--muted)}}table{{border-collapse:separate;border-spacing:0;width:100%;table-layout:fixed;overflow:hidden;border:1px solid var(--line);border-radius:14px;background:#fff}}th,td{{border-bottom:1px solid var(--line);padding:13px;text-align:left;vertical-align:top;overflow-wrap:anywhere}}th{{background:#f8fafc;color:#475467;font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}}tr:last-child td{{border-bottom:0}}th:nth-child(1){{width:12%}}th:nth-child(2){{width:16%}}.reason{{font-weight:600}}.number-link,a{{color:#4338ca;text-decoration:underline;text-underline-offset:2px}}.kv{{font-size:.84rem;color:var(--muted);overflow-wrap:anywhere}}details{{margin-top:10px;color:var(--muted)}}summary{{cursor:pointer;font-weight:700}}.timeline-row{{display:grid;grid-template-columns:150px 1fr;gap:12px;align-items:center;margin:11px 0}}.timeline-label{{display:flex;justify-content:space-between;gap:10px;font-size:.86rem}}.timeline-label span{{color:var(--muted);white-space:nowrap}}.timeline-track{{height:9px;background:#eef2ff;border-radius:99px;overflow:hidden}}.timeline-track i{{display:block;height:100%;background:linear-gradient(90deg,#6366f1,#a78bfa);border-radius:99px}}.timeline-total{{margin:16px 0 0;color:var(--muted)}}.flag{{display:inline-block;background:#fff1f2;color:#be123c;border:1px solid #fecdd3;border-radius:999px;padding:3px 8px;font-size:.75rem;margin:4px 4px 0 0}}footer{{color:var(--muted);font-size:.82rem;margin-top:32px}}@media(max-width:760px){{.flow-grid{{grid-template-columns:1fr}}}}@media(max-width:620px){{main{{padding:16px 12px 40px}}.hero{{padding:22px;border-radius:18px}}.timeline-row{{grid-template-columns:1fr;gap:4px}}th,td{{padding:9px;font-size:.88rem}}}}
@media print{{body{{background:#fff}}main{{max-width:none;padding:0}}.hero,.panel{{box-shadow:none;border:1px solid #bbb}}details{{display:block}}a{{color:#000;text-decoration:none}}}}
</style></head>
<body><main>
<header class="hero"><div class="eyebrow">THESIS RED TEAM · {html.escape(report['ticker'])}</div><h1>{html.escape(report.get('thesis') or '가설 레드팀 판정서')}</h1><div class="hero-bottom"><p class="hero-conclusion">{html.escape(str(conclusion))}</p><span class="badge">사실 {fact_label}</span><span class="badge {badge_class}">선별 {verdict}</span></div><div class="hero-meta"><span>기준일 {html.escape(report['asof'])}</span><span>·</span><span>{html.escape(TEMPLATE_LABELS.get(str(report['template']), str(report['template'])))}</span><span>·</span><span>{html.escape(report.get('status', 'COMPLETE'))}</span></div></header>
{_demo_flow(report)}
{fact_section}
<section><h2>② 선별 조건(사용자 승인)</h2><div class="panel"><p>{criteria_note} · 선별 판정: {verdict}</p><table><thead><tr><th>선별 조건</th><th>판정</th><th>근거 / 입력</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>
{extra_section}
<section><h2>실행 타임라인</h2><div class="panel">{_timeline(report)}</div></section>
<section><h2>SEC 원문 출처</h2><div class="panel"><ul>{source_rows or '<li class="muted">인용할 원문 값이 없습니다.</li>'}</ul></div></section>
<section><h2>감사 영수증</h2><details class="panel"><summary>receipts · trace · flags 펼치기</summary><p>{flags or '<span class="muted">추가 경고 없음</span>'}</p><table><thead><tr><th>URL key</th><th>SHA-256</th><th>bytes</th><th>fetched_at</th></tr></thead><tbody>{receipt_rows or '<tr><td colspan="4">없음</td></tr>'}</tbody></table><pre style="white-space:pre-wrap;overflow-wrap:anywhere">{html.escape(json.dumps(report.get('trace', []), ensure_ascii=False, indent=2))}</pre></details></section>
<footer>가설 레드팀 · 결정론적 verifier와 원문 링크를 우선합니다.</footer>
</main></body></html>"""


def write_reports(report: dict[str, Any], out_dir: str | Path) -> tuple[Path, Path]:
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    stem = f"{report['ticker']}_{report['asof']}_{report['template']}"
    json_path = output / f"{stem}.json"
    html_path = output / f"{stem}.html"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return json_path, html_path

