"""Reporter stage plus the verifier-backed numeric hallucination guard."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from ._schema import validate


REPORTER_SCHEMA: dict[str, Any] = {
    "title": "reporter_output",
    "type": "object",
    "properties": {
        "summary_ko": {"type": "string"},
        "conclusion_ko": {"type": "string"},
    },
    "required": ["summary_ko", "conclusion_ko"],
}
REPORTER_OUTPUT_SCHEMA = REPORTER_SCHEMA

_FORBIDDEN_ADVICE_WORDS = (
    "매수",
    "매도",
    "승인",
    "부결",
    "긍정적으로 검토",
    "투자 권유",
    "추천",
    "투자심의",
)

_NUMBER_RE = re.compile(
    r"(?<![A-Za-z가-힣])[-+−]?\$?\d[\d,]*(?:\.\d+)?\s*(?:%p|pp|%|[KMB]|[억만])?",
    re.IGNORECASE,
)
_PARSED_NUMBER_RE = re.compile(
    r"(?P<sign>[-+])?(?P<value>\d[\d,]*(?:\.\d+)?)(?P<unit>%p|pp|%|[KMB]|[억만])?",
    re.IGNORECASE,
)

_MONEY_SCALES = {
    "K": Decimal("1000"),
    "M": Decimal("1000000"),
    "B": Decimal("1000000000"),
    "만": Decimal("10000"),
    "억": Decimal("100000000"),
}


@dataclass(frozen=True)
class _ParsedNumber:
    value: Decimal
    unit: str | None
    tolerance: Decimal


def _number_tokens(text: str) -> list[str]:
    return [match.group(0).replace("−", "-").replace(" ", "") for match in _NUMBER_RE.finditer(text)]


def _parse_number(token: str) -> _ParsedNumber | None:
    cleaned = token.replace("−", "-").replace("$", "").replace(",", "").replace(" ", "")
    match = _PARSED_NUMBER_RE.fullmatch(cleaned)
    if match is None:
        return None
    try:
        value = Decimal(match.group("value"))
    except (InvalidOperation, ValueError):
        return None
    if match.group("sign") == "-":
        value = -value
    raw_unit = match.group("unit")
    unit = raw_unit.upper() if raw_unit and raw_unit.lower() in {"%", "%p", "pp", "k", "m", "b"} else raw_unit
    decimal_places = len(match.group("value").partition(".")[2])
    display_step = Decimal(1).scaleb(-decimal_places)
    scale = _MONEY_SCALES.get(unit or "", Decimal(1))
    tolerance = display_step * scale / Decimal(2)
    if unit in {"%", "%P", "PP"}:
        unit = "%"
        tolerance = display_step / Decimal(2)
    elif unit in {"K", "M", "B", "만", "억"}:
        tolerance = display_step * scale / Decimal(2)
    else:
        unit = None
        tolerance = display_step / Decimal(2)
    return _ParsedNumber(value=value, unit=unit, tolerance=tolerance)


def _parse_token(token: str) -> Decimal | None:
    """Return the token in its displayed unit, retaining the sign.

    This small compatibility helper is intentionally kept separate from the
    guard's absolute, tolerance-aware comparison below.
    """

    parsed = _parse_number(token)
    if parsed is None:
        return None
    return parsed.value * _MONEY_SCALES.get(parsed.unit or "", Decimal(1))


def _numeric_candidates(value: Decimal) -> set[Decimal]:
    """Return unitless verifier representations, including ratio percentages."""

    absolute = abs(value)
    candidates = {absolute}
    if absolute <= 1:
        candidates.add(absolute * Decimal(100))
    return candidates


def _canonical_candidates(parsed: _ParsedNumber) -> set[Decimal]:
    absolute = abs(parsed.value)
    if parsed.unit in _MONEY_SCALES:
        return {absolute * _MONEY_SCALES[parsed.unit]}
    if parsed.unit == "%":
        return {absolute, absolute / Decimal(100)}
    return _numeric_candidates(absolute)


def _leaf_values(value: Any) -> list[Any]:
    if isinstance(value, dict):
        result: list[Any] = []
        for nested in value.values():
            result.extend(_leaf_values(nested))
        return result
    if isinstance(value, list):
        result = []
        for nested in value:
            result.extend(_leaf_values(nested))
        return result
    return [value]


def verifier_numbers(verifier_result: dict[str, Any]) -> set[Decimal]:
    """Collect numeric values from every nested verifier field.

    Verifier ratios are commonly stored as unitless decimals while reporters
    display percentage points, so both representations are retained.
    """

    result: set[Decimal] = set()
    for value in _leaf_values(verifier_result):
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float, Decimal)):
            parsed = _parse_number(str(value))
            if parsed is not None:
                result.update(_canonical_candidates(parsed))
            continue
        if isinstance(value, str):
            for token in _number_tokens(value):
                parsed = _parse_number(token)
                if parsed is not None:
                    result.update(_canonical_candidates(parsed))
    return result


def numeric_hallucinations(text: str, verifier_result: dict[str, Any]) -> list[str]:
    allowed = verifier_numbers(verifier_result)
    missing: list[str] = []
    for token in _number_tokens(text):
        parsed = _parse_number(token)
        if parsed is not None and not any(
            abs(candidate - allowed_value) <= parsed.tolerance
            for candidate in _canonical_candidates(parsed)
            for allowed_value in allowed
        ):
            missing.append(token)
    return missing


def claim_refutation_conclusion(verifier_result: dict[str, Any]) -> str:
    if "fact_verdict" in verifier_result:
        facts = verifier_result.get("fact_claims", [])
        failed = [str(fact.get("statement_ko", fact.get("id"))) for fact in facts if fact.get("status") == "FALSE"]
        verdict = verifier_result["fact_verdict"]
        if failed:
            factual = f"원래 주장의 사실({', '.join(failed)})은 원문 숫자로 반증되었다"
        elif verdict == "SUPPORTED":
            factual = "원래 주장에 명시된 사실은 원문 숫자로 반증되지 않았다(검증 범위 내 지지)"
        else:
            factual = "원래 주장의 사실은 판정불가(미지원 주장 또는 증거 부족)"
        screening = (
            f"선별 조건 결과: {verifier_result.get('verdict', 'INCONCLUSIVE')}"
            if (verifier_result.get("criteria") or {}).get("mode") != "none"
            else "선별 조건 미적용"
        )
        return f"결론: {factual}. {screening}."
    labels = {
        "C1": "순현금환원",
        "C2": "기말 주식수 감소",
        "C3": "현금흐름 충당",
        "C4": "부채성 환원",
        "E1": "이익 개선",
        "E2": "이익의 현금 뒷받침",
        "E3": "발생액 악화 없음",
        "E4": "운전자본 의존 없음",
        "E5": "일회성 이익 의존 없음",
    }
    claims = [claim for claim in verifier_result.get("claims", []) if isinstance(claim, dict)]
    failed = [labels.get(str(claim.get("id")), str(claim.get("id", "청구항"))) for claim in claims if claim.get("status") == "FAIL"]
    if failed:
        return f"결론: 이 가설의 핵심 청구항({', '.join(failed)})은 원문 숫자로 반증되었다"
    return "결론: 검증한 청구항 모두 원문 숫자로 반증되지 않았다(참임을 보증하지 않음)"


def _advice_language(text: str) -> list[str]:
    return [word for word in _FORBIDDEN_ADVICE_WORDS if word in text]


def fallback_report(verifier_result: dict[str, Any], *, reason: str | None = None) -> dict[str, Any]:
    verdict = str(verifier_result.get("verdict", "INCONCLUSIVE"))
    summary = (
        f"검증 코어의 최종 판정은 {verdict}입니다. "
        "청구항별 계산과 원문 입력을 기준으로 결과를 확인했습니다. "
        "검증 결과에 없는 숫자는 요약에 사용하지 않았습니다."
    )
    if "fact_verdict" in verifier_result:
        summary = (
            f"사실 검증 (원래 주장): {verifier_result['fact_verdict']}. "
            f"선별 조건: {verifier_result.get('verdict', 'INCONCLUSIVE')}. "
            "각 층의 원공시 입력과 결정론적 계산을 확인했습니다."
        )
    if reason:
        summary += " " + reason
    return {
        "summary_ko": summary,
        "conclusion_ko": claim_refutation_conclusion(verifier_result),
        "reporter_numeric_hallucination": reason == "reporter_numeric_hallucination",
        "reporter_advice_language": False,
        "used_fallback": True,
    }


def report(*, verifier_result: dict[str, Any], provider: Any, model: str = "sonnet") -> dict[str, Any]:
    system = (
        "stage:reporter\n"
        "검증 JSON만 근거로 3~5문장 한국어 요약과, 청구항이 원문 숫자로 반증되었는지만 말하는 한 줄 결론을 작성하라. "
        "결론은 투자 판단이나 행동 권고가 아니라 가설의 반증 여부만 기술해야 한다. "
        "매수, 매도, 승인, 부결, 긍정적으로 검토, 투자 권유, 추천, 투자심의라는 표현을 결론에 쓰지 말라. "
        "새 숫자나 계산을 만들지 말라."
        " fact_verdict와 fact_claims가 있으면 원래 주장 사실 검증을 결론의 첫 부분에 쓴다. "
        "claims는 선별 조건이며 FAIL은 원래 주장의 반증이 아니다. 미주장 사실을 판정하지 말라."
    )
    user = json.dumps(verifier_result, ensure_ascii=False)
    response = provider.complete_json(system=system, user=user, schema=REPORTER_SCHEMA, model=model)
    errors = validate(response, REPORTER_SCHEMA)
    if errors:
        raise ValueError("reporter schema error: " + "; ".join(errors))
    missing = numeric_hallucinations(
        str(response["summary_ko"]) + " " + str(response["conclusion_ko"]),
        verifier_result,
    )
    advice_words = _advice_language(str(response["conclusion_ko"]))
    if missing:
        fallback = fallback_report(verifier_result, reason="reporter_numeric_hallucination")
        fallback["reporter_advice_language"] = bool(advice_words)
        fallback["hallucinated_numbers"] = missing
        return fallback
    conclusion = str(response["conclusion_ko"]).strip()
    invalid_conclusion = not (conclusion.startswith("결론:") and "반증" in conclusion)
    if advice_words or invalid_conclusion or "fact_verdict" in verifier_result:
        return {
            "summary_ko": response["summary_ko"],
            "conclusion_ko": claim_refutation_conclusion(verifier_result),
            "reporter_numeric_hallucination": False,
            "reporter_advice_language": bool(advice_words),
            "used_fallback": False,
        }
    return {
        "summary_ko": response["summary_ko"],
        "conclusion_ko": response["conclusion_ko"],
        "reporter_numeric_hallucination": False,
        "reporter_advice_language": False,
        "used_fallback": False,
    }


run = report

