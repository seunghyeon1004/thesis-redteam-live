"""Live demo core: run the deterministic verifier (no LLM) for one ticker and summarize it."""
from __future__ import annotations

import csv
import html
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from thesis_redteam.cli import run_case

HERE = Path(__file__).resolve().parent
HOLDINGS = list(csv.DictReader((HERE / "kic_holdings_2026Q2.csv").open()))
BY_TICKER = {row["ticker"].upper(): row for row in HOLDINGS}
# One run at a time: keeps SEC traffic polite and keeps each report's receipts to its own run.
RUN_LOCK = threading.Lock()

# Fixed fact claims for the two demo hypotheses (the LLM Planner is not used in the live demo).
HYPOTHESES = {
    "shareholder_return": {
        "label": "자사주를 사서 주식수를 줄이고 주주에게 현금을 돌려줬다",
        "facts": [
            ("F1", "repurchase_positive", "최근 회계연도에 자사주를 매입했다", "자사주를 사서"),
            ("F2", "net_buyback_positive", "주주에게 현금을 돌려줬다(매입 > 보통주 발행)", "주주에게 현금을 돌려줬다"),
            ("F3", "shares_decreased", "기말 주식수를 줄였다", "주식수를 줄이고"),
        ],
    },
    "earnings_quality": {
        "label": "순이익이 늘었고 영업현금흐름이 이익을 뒷받침한다",
        "facts": [
            ("F1", "net_income_increased", "순이익이 증가했다", "순이익이 늘었고"),
            ("F2", "cfo_supports_income", "영업현금흐름이 순이익을 뒷받침한다", "영업현금흐름이 이익을 뒷받침한다"),
        ],
    },
}
STATUS_KO = {"TRUE": "참", "FALSE": "거짓(반증)", "판정불가": "판정불가"}
VERDICT_KO = {"SUPPORTED": "원문으로 지지됨", "REFUTED": "원문으로 반증됨", "INCONCLUSIVE": "판정불가(멈춤)"}


def _claims(ticker: str, template: str) -> list[dict[str, Any]]:
    return [{"id": i, "check": check, "statement_ko": f"{ticker}는 {text}", "original_quote": quote,
             "period_year": "", "threshold": ""} for i, check, text, quote in HYPOTHESES[template]["facts"]]


def _money(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    a = abs(v)
    s = f"${a / 1e9:,.2f}B" if a >= 1e9 else f"${a / 1e6:,.1f}M"
    return ("−" if v < 0 else "") + s


def verify(ticker: str, template: str, asof: str, out_dir: str | None = None) -> dict[str, Any]:
    ticker = re.sub(r"[^A-Z.\-]", "", ticker.upper())[:10]
    if not ticker:
        raise ValueError("종목 코드를 입력하세요.")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", asof):
        raise ValueError("기준일은 YYYY-MM-DD 형식입니다.")
    holding = BY_TICKER.get(ticker)
    out = out_dir or tempfile.mkdtemp(prefix="trt-")
    started = time.perf_counter()
    with RUN_LOCK:
        _, html_path, report = run_case(
            ticker=ticker, template=template, asof=asof, offline=False, out_dir=out,
            cik=holding["cik"] if holding else None, no_prices=True, fact_claims=_claims(ticker, template),
        )
    seconds = time.perf_counter() - started
    hints = {}
    for check in report.get("extra_checks", []):
        for value in (check.get("outputs") or {}).get("values", []) or []:
            if value.get("search_hint") and float(value.get("value") or 0) != 0:
                hints[str(value.get("value"))] = value["search_hint"]
    return {"ticker": ticker, "holding": holding, "template": template, "asof": asof, "seconds": seconds,
            "report": report, "html_path": str(html_path), "hints": hints}


def summary_markdown(result: dict[str, Any]) -> str:
    report, holding = result["report"], result["holding"]
    lines = [f"### {result['ticker']}" + (f" · {holding['name']} · KIC 13F 비중 {float(holding['weight_pct']):.2f}%" if holding else " · KIC 13F 보유 목록 밖"),
             f"가설: “{result['ticker']}는 {HYPOTHESES[result['template']]['label']}” · 기준일 {result['asof']} (그날까지 공개된 공시만 사용) · 실행 {result['seconds']:.1f}초",
             "",
             f"**원래 주장 사실 판정: {VERDICT_KO.get(report.get('fact_verdict'), report.get('fact_verdict'))}**",
             "",
             "| 사실 | 판정 | 원문 숫자와 근거 |", "|---|---|---|"]
    for fact in report.get("fact_claims", []):
        reason = re.sub(r"(?<![\d.])(\d{7,})(?![\d.])", lambda m: _money(m.group(1)) if "주식수" not in fact.get("statement_ko", "") else f"{int(m.group(1)):,}", fact.get("reason_ko", ""))
        reason = re.sub(r"(\d+\.\d{3})\d+", r"\1", reason)
        url = next((v.get("filing_url") for v in (fact.get("inputs") or {}).values() if isinstance(v, dict) and v.get("filing_url")), None)
        link = f" · [원문]({url})" if url else ""
        lines.append(f"| {fact['id']} {fact.get('statement_ko', '')} | {STATUS_KO.get(fact.get('status'), fact.get('status'))} | {reason}{link} |")
    if result["hints"]:
        lines += ["", "**원문에서 바로 찾기** (Cmd/Ctrl+F로 검색하면 원문에서 한 곳만 나오는 문구)"]
        lines += [f"- {_money(v)} → `{h}`" for v, h in list(result["hints"].items())[:6]]
    lines += ["", "판정불가는 데이터가 없거나 교차확인에 실패해 억지 결론을 내지 않은 경우입니다. 이 데모는 LLM 없이 코드 검산만 실행합니다(가격 미사용). 투자 권유가 아닙니다."]
    # Streamlit markdown treats paired "$" as LaTeX math; escape every dollar sign.
    return "\n".join(lines).replace("$", "\\$")


def report_iframe(result: dict[str, Any]) -> str:
    body = Path(result["html_path"]).read_text(encoding="utf-8")
    return f'<iframe style="width:100%;height:900px;border:1px solid #ccd" srcdoc="{html.escape(body, quote=True)}"></iframe>'
