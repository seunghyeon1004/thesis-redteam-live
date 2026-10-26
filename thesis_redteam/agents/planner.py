"""Planner stage: map a free-form thesis to a supported template and claims."""

from __future__ import annotations

import json
import re
from typing import Any

from ._schema import validate
from ..facts import FACT_CHECKS


PLANNER_SCHEMA: dict[str, Any] = {
    "title": "planner_output",
    "type": "object",
    "properties": {
        "template": {"type": "string", "enum": ["shareholder_return", "earnings_quality", "unsupported"]},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "statement_ko": {"type": "string"},
                    "metric": {"type": "string"},
                    "required_fields": {"type": "array", "items": {"type": "string"}},
                    "falsify_if": {"type": "string"},
                },
                "required": ["id", "statement_ko", "metric", "required_fields", "falsify_if"],
            },
        },
        "fact_claims": {
            "type": "array", "items": {
                "type": "object", "properties": {
                    "id": {"type": "string"},
                    "check": {"type": "string", "enum": list(FACT_CHECKS)},
                    "statement_ko": {"type": "string"},
                    "original_quote": {"type": "string"},
                    "threshold": {"type": "string"},
                    "period_year": {"type": "string"},
                }, "required": ["id", "check", "statement_ko", "original_quote"],
            },
        },
        "assumptions_ko": {"type": "string"},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["template", "claims", "assumptions_ko"],
}
PLANNER_OUTPUT_SCHEMA = PLANNER_SCHEMA


def build_prompt(*, thesis: str, ticker: str, asof: str, templates: list[str]) -> tuple[str, str]:
    system = (
        "stage:planner\n"
        "당신은 투자 가설을 검증 가능한 청구항으로 분해하는 planner다. "
        "숫자를 계산하거나 판정하지 말고 JSON schema만 따르라. "
        "fact_claims는 원래 주장에 실제로 있는 사실만 추출한다. original_quote는 원문 그대로 인용한다. "
        "C1–C4/E1–E5는 원 연구의 선별 조건이며 원래 주장에 추가하지 말라. "
        "shareholder_return: F1 repurchase_positive(매입 > 0)는 매입을 주장할 때만, "
        "F2 net_buyback_positive(매입 > 보통주 발행)는 현금 환원·순환원·주식수 감소를 뜻할 때만, "
        "F3 shares_decreased(기말 주식수 감소)는 주식수 감소를 명시할 때만 추출한다. "
        "단순 매입 사실은 순환원이나 주식수 감소를 함의하지 않는다. "
        "earnings_quality: F1 net_income_increased(순이익 증가), F2 cfo_supports_income(CFO/NI ≥ 1, "
        "사용자가 수치 기준을 명시한 경우에만 threshold를 변경) 중 주장된 사실만 추출한다. "
        "연도가 명시되면 period_year에 기록한다. 미명시 threshold/period_year는 빈 문자열이다. "
        "미래 예측, 시장 점유율, 경영진 신뢰, ESG 평가는 공시 숫자로 검증할 수 없다. "
        "미지원 주장은 unsupported_claims에 원문 그대로 보존한다. 매핑할 수 없으면 "
        "template=unsupported, claims=[], fact_claims=[]로 반환한다. "
        "claims는 기존 탐색 계획용이며 fact_claims와 혼합하지 말라."
    )
    user = json.dumps(
        {"thesis": thesis, "ticker": ticker, "asof": asof, "available_templates": templates},
        ensure_ascii=False,
    )
    return system, user


def plan(
    *,
    thesis: str,
    ticker: str,
    asof: str,
    templates: list[str],
    provider: Any,
    model: str = "sonnet",
) -> dict[str, Any]:
    system, user = build_prompt(thesis=thesis, ticker=ticker, asof=asof, templates=templates)
    result = provider.complete_json(system=system, user=user, schema=PLANNER_SCHEMA, model=model)
    errors = validate(result, PLANNER_SCHEMA)
    if errors:
        raise ValueError("planner schema error: " + "; ".join(errors))
    normalized = dict(result)
    normalized.setdefault("unsupported_claims", [])
    # Older providers may supply only a screening plan. Never treat it as facts.
    if "fact_claims" not in normalized:
        literal = literal_plan(thesis, preferred_template=normalized["template"])
        normalized.update(fact_claims=literal["fact_claims"],
                          unsupported_claims=list(dict.fromkeys(normalized["unsupported_claims"] + literal["unsupported_claims"])))
        if literal["template"] == "unsupported":
            normalized["template"] = "unsupported"
    for fact in normalized["fact_claims"]:
        if not fact.get("original_quote") or fact["original_quote"] not in thesis:
            raise ValueError("fact original_quote must occur in thesis")
    if normalized["template"] not in templates and normalized["template"] != "unsupported":
        raise ValueError("planner chose unavailable template")
    if not normalized["fact_claims"] and normalized["unsupported_claims"]:
        normalized["template"] = "unsupported"
    if normalized["template"] == "unsupported":
        normalized["fact_claims"] = []
        normalized["unsupported_claims"] = normalized["unsupported_claims"] or [thesis]
    return normalized



def literal_plan(thesis: str, *, preferred_template: str = "shareholder_return") -> dict[str, Any]:
    """Conservative offline/legacy extraction; live providers use semantic claims."""
    lower = thesis.lower()
    if re.search(r"할 것이다|유지할 것이다|앞으로|will\b|forecast", lower):
        return {'template': 'unsupported', 'claims': [], 'fact_claims': [],
                'unsupported_claims': [thesis], 'assumptions_ko': '미래 예측은 과거 공시로 판정할 수 없다.'}
    facts: list[dict[str, Any]] = []
    unsupported = []
    shareholder = bool(re.search(r"자사주|주주환원|순환원|주식수|주식 수|buyback|repurchas|shares|cash return", lower))
    earnings = bool(re.search(r"순이익|이익|현금흐름|cfo|earnings|net income", lower))
    template = 'shareholder_return' if shareholder else 'earnings_quality' if earnings else 'unsupported'
    if template == 'unsupported':
        return {'template': template, 'claims': [], 'fact_claims': [],
                'unsupported_claims': [thesis], 'assumptions_ko': ''}
    year_match = re.search(r"\b(20\d{2})(?:년|\b)", thesis)
    year = year_match.group(1) if year_match else ''
    def add(check: str, threshold: str = '') -> None:
        identifier, statement, _ = FACT_CHECKS[check]
        facts.append({'id': identifier, 'check': check, 'statement_ko': statement,
                      'original_quote': thesis, 'threshold': threshold, 'period_year': year})
    if template == 'shareholder_return':
        if re.search(r"자사주.*(?:매입|사(?:는|들|고|서|왔다))|buyback|repurchas", lower):
            add('repurchase_positive')
        if re.search(r"순자사주|순환원|순현금|현금.*환원|주주.*현금|cash.*return|net.*buyback", lower):
            add('net_buyback_positive')
        if re.search(r"(?:주식\s*수|shares).*(?:감소|줄|decreas|reduc)", lower):
            add('shares_decreased')
    else:
        if re.search(r"(?:순이익|이익|earnings|net income).*(?:증가|개선|늘|increas|improv)", lower):
            add('net_income_increased')
        if re.search(r"뒷받침|현금.*동반|cfo\s*/\s*(?:ni|순이익)|cash.back", lower):
            threshold = re.search(r"(?:cfo\s*/\s*(?:ni|순이익))\s*(?:≥|>=|은|가|이)?\s*(\d+(?:\.\d+)?)", lower)
            add('cfo_supports_income', threshold.group(1) if threshold else '')
    # Keep qualitative/forward claims visible rather than granting factual support.
    if re.search(r"점유율|경영진.*신뢰|esg|유지할 것이다|주주친화", lower):
        unsupported.append(thesis)
    return {'template': template, 'claims': [], 'fact_claims': facts,
            'unsupported_claims': unsupported, 'assumptions_ko': '원래 주장에 명시된 사실만 검증한다.'}
