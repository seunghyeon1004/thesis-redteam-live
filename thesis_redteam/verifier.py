"""Decimal arithmetic and deterministic verification primitives."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, getcontext
from html.parser import HTMLParser
import re
from typing import Any, Callable, Iterable

from .sec_client import OfflineDataUnavailable, SecClient, SecNetworkError


getcontext().prec = 50


def decimal(value: Any) -> Decimal:
    if value is None:
        raise ValueError("missing value cannot be converted to Decimal")
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc


def decimal_string(value: Decimal | Any) -> str:
    return str(value if isinstance(value, Decimal) else decimal(value))


def require_same_unit(records: Iterable[dict[str, Any]]) -> str:
    rows = list(records)
    units = {row.get("unit") for row in rows}
    if len(units) != 1 or None in units:
        raise ValueError(f"unit mismatch: {sorted(units, key=str)}")
    return next(iter(units))


def require_usd_unit(records: Iterable[dict[str, Any]]) -> str:
    """Assert that every monetary input is present and reported in USD."""
    unit = require_same_unit(records)
    if unit != "USD":
        raise ValueError(f"monetary unit must be USD: {unit!r}")
    return unit


def require_same_period(records: Iterable[dict[str, Any]]) -> tuple[str | None, str | None]:
    rows = list(records)
    periods = {(row.get("period_start"), row.get("period_end")) for row in rows}
    if len(periods) != 1:
        raise ValueError(f"period mismatch: {sorted(periods, key=str)}")
    return next(iter(periods))


def share_roll_forward(
    begin: Any,
    repurchased: Any,
    issued: Any,
    end: Any,
) -> dict[str, str | bool]:
    expected = decimal(begin) - decimal(repurchased) + decimal(issued)
    actual = decimal(end)
    residual = actual - expected
    return {
        "expected_end": decimal_string(expected),
        "actual_end": decimal_string(actual),
        "residual": decimal_string(residual),
        "matches": residual == 0,
    }


def overall_verdict(claims: Iterable[dict[str, Any]]) -> str:
    # Required template claims retain their own semantics (including E5 FAIL).
    # Only additional, advisory checks are excluded from aggregation.
    statuses = {
        claim.get("status") for claim in claims
        if not (claim.get("advisory") and (claim.get("check") or claim.get("extra_check")))
    }
    if "FAIL" in statuses:
        return "FAIL"
    if "INCONCLUSIVE" in statuses:
        return "INCONCLUSIVE"
    if statuses and statuses <= {"NOT_APPLICABLE"}:
        return "NOT_APPLICABLE"
    return "PASS"


# The red-team LLM is constrained to this registry.
REGISTERED_EXTRA_CHECKS = frozenset(
    {
        "net_issuance_offsets_buyback",
        "period_end_shares_not_weighted",
        "debt_funded_return",
        "fcf_covers_buyback",
        "pit_acceptance_conflict",
        "xbrl_html_mismatch",
        "missing_value_null",
        "working_capital_release",
        "one_off_gain",
        "accrual_spike",
    }
)
EXTRA_CHECKS = REGISTERED_EXTRA_CHECKS
ADVISORY_EXTRA_CHECKS = frozenset({"xbrl_html_mismatch", "one_off_gain"})


def registered_checks() -> tuple[str, ...]:
    return tuple(sorted(REGISTERED_EXTRA_CHECKS))


def _check_result(
    name: str,
    status: str,
    reason_ko: str,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "check": name,
        "extra_check": name,
        "status": status,
        "reason_ko": reason_ko,
        "inputs": inputs or {},
        "advisory": name in ADVISORY_EXTRA_CHECKS,
    }


class _DocumentText(HTMLParser):
    """Keep visible text and table boundaries without separating inline digits."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden: str | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"script", "style"}:
            self.hidden = tag
        if tag in {"p", "div", "td", "th", "tr", "br", "li", "table"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == self.hidden:
            self.hidden = None
        if tag in {"p", "div", "td", "th", "tr", "li", "table"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _monetary_inputs(evaluation: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk claim inputs, using source components instead of computed totals."""
    found: dict[tuple[Any, ...], dict[str, Any]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            components = value.get("ttm_components") or value.get("components")
            if components:
                visit(components)
                return
            if (re.fullmatch(r"[A-Z]{3}", str(value.get("unit", "")))
                    and value.get("value") is not None and value.get("accession")):
                key = tuple(str(value.get(key, "")) for key in
                            ("cik", "accession", "tag", "unit", "period_start", "period_end", "value"))
                found[key] = value
            for item in value.values():
                visit(item)

    for claim in evaluation.get("claims", []):
        visit(claim.get("inputs", {}))
    return list(found.values())


def _money_candidates(value: Any, scales: list[int]) -> list[str]:
    amount = decimal(value)
    candidates: list[str] = []
    for scale in dict.fromkeys([*scales, 1000, 1000000, 1]):
        scaled = amount / scale
        # A tiny nonzero amount rounded to zero is not corroboration.
        if amount and abs(scaled) < 1:
            continue
        for places in (0, 1):
            number = format(abs(scaled), f",.{places}f")
            if scaled < 0:
                candidates.extend([f"({number})", f"-{number}", f"−{number}"])
            else:
                candidates.append(number)
    return list(dict.fromkeys(candidates))


def issuance_notes(record: dict[str, Any]) -> list[str]:
    """Keep source assumptions visible even when FY/YTD components are combined."""
    notes = [record["issuance_note"]] if record.get("issuance_note") else []
    for component in (record.get("ttm_components") or {}).values():
        notes.extend(issuance_notes(component))
    return list(dict.fromkeys(notes))


def _unique_search_hint(text: str, number: str) -> dict[str, Any] | None:
    """Shortest 'preceding words + number' phrase that occurs exactly once in the document.

    A human checking the report should land on one place with Cmd+F; a bare label such as
    "Repurchases of common stock" can occur several times in a 10-K.
    """
    pattern = r"(?<![\w.,−-])" + re.escape(number) + r"(?![\w,]|\.\d)"
    starts = [m.start() for m in re.finditer(pattern, text)]
    if not starts:
        return None
    best = None
    for start in starts:
        words = text[max(0, start - 200):start].split()
        for size in range(0, min(len(words), 10) + 1):
            phrase = (" ".join(words[len(words) - size:]) + " " + number).strip() if size else number
            if text.count(phrase) == 1:
                if best is None or len(phrase) < len(best):
                    best = phrase
                break
    return {"search_hint": best, "number_occurrences": len(starts)} if best else {"number_occurrences": len(starts)}


def _xbrl_html_check(
    evaluation: dict[str, Any],
    filings: Iterable[dict[str, Any]],
    client: SecClient,
    before_fetch: Callable[[], None] | None,
) -> dict[str, Any]:
    documents = {
        str(row.get("accessionNumber") or row.get("accession") or "").replace("-", ""): row
        for row in filings
        if row.get("form") in {"10-K", "10-K/A", "10-Q", "10-Q/A"}
    }
    loaded: dict[tuple[str, str], dict[str, Any]] = {}
    values: list[dict[str, Any]] = []
    assumptions: list[dict[str, Any]] = []
    for row in _monetary_inputs(evaluation):
        if row.get("issuance_scope") == "absent_in_cash_flow_statement":
            # This zero is an explicit source-based assumption, not an amount
            # that can be corroborated by finding an unrelated zero in HTML.
            assumptions.append(row)
            continue
        cik = str(row.get("cik") or "")
        accession = str(row["accession"])
        key = (cik, accession)
        if key not in loaded:
            filing = documents.get(accession.replace("-", ""), {})
            document = filing.get("primaryDocument")
            text = None
            scales: list[int] = []
            if cik and document:
                try:
                    if before_fetch:
                        before_fetch()
                    body = client.primary_document(cik, accession, document)
                    parser = _DocumentText()
                    parser.feed(body.decode("utf-8", errors="replace"))
                    parser.close()
                    text = " ".join("".join(parser.parts).split())
                    text = re.sub(r"\(\s+", "(", text)
                    text = re.sub(r"\s+\)", ")", text)
                    scales = list(dict.fromkeys(
                        1000 if match.lower() == "thousands" else 1000000
                        for match in re.findall(r"\bin\s+(thousands|millions)\b", text, re.I)
                    ))
                except (OfflineDataUnavailable, SecNetworkError, OSError):
                    pass
            loaded[key] = {"accession": accession, "cik": cik, "primary_document": document,
                           "scales": scales, "available": bool(text), "text": text}
        source = loaded[key]
        matched = None
        match_strength = None
        matched_digits = 0
        if source["available"]:
            for candidate in _money_candidates(row["value"], source["scales"]):
                if re.search(r"(?<![\w.,−-])" + re.escape(candidate) + r"(?![\w,]|\.\d)", source["text"]):
                    number = re.sub(r"[^\d.]", "", candidate)
                    integer, _, fraction = number.partition(".")
                    # Formatting's trailing .0 adds no evidence to a short match.
                    digits = (integer + fraction.rstrip("0")).lstrip("0")
                    strength = "strong" if len(digits) >= 4 else "weak"
                    if matched is not None and strength == "weak" and len(digits) <= matched_digits:
                        continue
                    matched_digits = len(digits)
                    matched = candidate
                    match_strength = strength
                    if strength == "strong":
                        break
        hint = _unique_search_hint(source["text"], matched) if matched else None
        values.append({**row, "found": matched is not None, "matched": matched,
                       "match_strength": match_strength,
                       "html_available": source["available"], **(hint or {})})
    missing = [row for row in values if not row["found"] and row["html_available"]]
    unavailable = [row for row in values if not row["html_available"]]
    found = sum(row["found"] for row in values)
    strong = sum(row["match_strength"] == "strong" for row in values)
    weak = sum(row["match_strength"] == "weak" for row in values)
    if unavailable:
        status, reason = "INCONCLUSIVE", "일부 입력의 원문 HTML을 사용할 수 없습니다."
    elif missing:
        status, reason = "WARNING", "원문 HTML에서 찾지 못한 값: " + "; ".join(
            f"{row.get('tag', '')}={row['value']} ({row['accession']})" for row in missing)
    else:
        status, reason = "PASS", f"원문 HTML에서 금액 {found}/{len(values)}개를 확인했습니다. (강한 일치 {strong})"
    if assumptions:
        reason += " · " + " · ".join(dict.fromkeys(row["issuance_note"] for row in assumptions))
    result = _check_result("xbrl_html_mismatch", status, reason)
    result["outputs"] = {
        "found": found, "total": len(values), "values": values,
        "strong_matches": strong, "weak_matches": weak,
        "missing": missing, "unavailable": unavailable,
        "assumptions": assumptions,
        "documents": [{k: v for k, v in doc.items() if k != "text"} for doc in loaded.values()],
    }
    return result


def _evaluation_for(fields: dict[str, Any], evaluation: dict[str, Any] | None) -> dict[str, Any]:
    if evaluation is not None:
        return evaluation
    # Lazy import avoids a module cycle while still making the standalone
    # check API useful to tests and callers.
    if any(key in fields for key in ("net_income", "net_income_comparative", "one_off_items")):
        from .templates.earnings_quality import evaluate
    else:
        from .templates.shareholder_return import evaluate
    return evaluate(fields)


def _claim(evaluation: dict[str, Any], claim_id: str) -> dict[str, Any] | None:
    return next((claim for claim in evaluation.get("claims", []) if claim.get("id") == claim_id), None)


def _check_claim(
    name: str,
    claim: dict[str, Any] | None,
    *,
    missing_reason: str,
    pass_reason: str,
    fail_reason: str | None = None,
) -> dict[str, Any]:
    if not claim:
        return _check_result(name, "INCONCLUSIVE", missing_reason)
    status = claim.get("status")
    if status == "FAIL":
        return _check_result(name, "FAIL", fail_reason or claim.get("reason_ko", "검증 실패"), claim.get("inputs"))
    if status == "PASS":
        return _check_result(name, "PASS", pass_reason, claim.get("inputs"))
    return _check_result(name, "INCONCLUSIVE", claim.get("reason_ko", missing_reason), claim.get("inputs"))


def run_extra_check(
    name: str,
    fields: dict[str, Any],
    *,
    evaluation: dict[str, Any] | None = None,
    filings: Iterable[dict[str, Any]] | None = None,
    sec_client: SecClient | None = None,
    before_fetch: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Run one verifier-owned red-team check by its registered name."""

    if name not in REGISTERED_EXTRA_CHECKS:
        raise ValueError(f"unregistered extra check: {name}")
    if evaluation is None and name in {"working_capital_release", "one_off_gain", "accrual_spike"}:
        from .templates.earnings_quality import evaluate

        result = evaluate(fields)
    else:
        result = _evaluation_for(fields, evaluation)
    if name == "xbrl_html_mismatch":
        return _xbrl_html_check(result, filings or [], sec_client or SecClient(offline=True), before_fetch)
    if name == "net_issuance_offsets_buyback":
        repurchase = fields.get("repurchase") or []
        issuance = fields.get("common_issuance") or []
        if repurchase and issuance:
            try:
                pairs = [
                    (buyback, issued)
                    for buyback in repurchase
                    for issued in issuance
                    if (buyback.get("period_start"), buyback.get("period_end"))
                    == (issued.get("period_start"), issued.get("period_end"))
                ]
                if not pairs:
                    raise ValueError("no common period")
                repurchase_row, issuance_row = pairs[-1]
                require_usd_unit((repurchase_row, issuance_row))
                net = decimal(repurchase_row["value"]) - decimal(issuance_row["value"])
                status = "PASS" if net > 0 else "FAIL"
                return _check_result(
                    name,
                    status,
                    "발행 현금을 차감한 순현금환원이 양수입니다." if status == "PASS" else "발행 현금이 매입을 초과해 순현금환원이 음수입니다.",
                    {"net_return": decimal_string(net)},
                )
            except (KeyError, ValueError):
                pass
        return _check_claim(
            name,
            _claim(result, "C1"),
            missing_reason="매입·발행 현금이 없어 순환원을 확인할 수 없습니다.",
            pass_reason="발행 현금을 차감한 순현금환원이 양수입니다.",
        )
    if name == "fcf_covers_buyback":
        return _check_claim(
            name,
            _claim(result, "C3"),
            missing_reason="CFO·설비투자·매입의 동일 기간 자료가 없습니다.",
            pass_reason="CFO-설비투자가 보통주 매입을 충당합니다.",
        )
    if name == "debt_funded_return":
        return _check_claim(
            name,
            _claim(result, "C4"),
            missing_reason="부채·현금·자산의 동일 비교열 자료가 없습니다.",
            pass_reason="순부채/자산 비율 증가가 2%p 이하입니다.",
            fail_reason="순부채/자산 비율 증가가 2%p를 초과해 부채성 환원 위험이 확인됩니다.",
        )
    if name == "period_end_shares_not_weighted":
        raw_rows = fields.get("shares") or []
        raw_tags = {str(row.get("tag", "")) for row in raw_rows if isinstance(row, dict)}
        if raw_tags & {"WeightedAverageNumberOfSharesOutstandingBasic", "WeightedAverageNumberOfDilutedSharesOutstanding"}:
            return _check_result(name, "FAIL", "가중평균 주식수가 사용되어 기간말 실제 주식수와 다릅니다.", {"tags": sorted(raw_tags)})
        if raw_tags and raw_tags <= {"CommonStockSharesOutstanding"}:
            return _check_result(name, "PASS", "기간말 CommonStockSharesOutstanding를 사용했습니다.", {"tags": sorted(raw_tags)})
        claim = _claim(result, "C2")
        if not claim:
            return _check_result(name, "INCONCLUSIVE", "기간말 주식수 청구항이 없습니다.")
        inputs = claim.get("inputs") or {}
        rows = [inputs.get("value_1"), inputs.get("value_2")]
        if any(not isinstance(row, dict) for row in rows):
            return _check_result(name, "INCONCLUSIVE", "기간말 주식수 자료가 없습니다.", inputs)
        tags = {str(row.get("tag", "")) for row in rows}
        if tags & {"WeightedAverageNumberOfSharesOutstandingBasic", "WeightedAverageNumberOfDilutedSharesOutstanding"}:
            return _check_result(name, "FAIL", "가중평균 주식수가 사용되어 기간말 실제 주식수와 다릅니다.", inputs)
        if "CommonStockSharesOutstanding" not in tags:
            return _check_result(name, "INCONCLUSIVE", "기간말 outstanding 태그를 확인할 수 없습니다.", inputs)
        return _check_result(name, "PASS", "기간말 CommonStockSharesOutstanding를 사용했습니다.", inputs)
    if name == "pit_acceptance_conflict":
        rows = list(filings or [])
        if not rows:
            return _check_result(name, "INCONCLUSIVE", "PIT filing acceptance 자료가 없습니다.")
        from .pit import normalize_filing, parse_timestamp

        conflicts: list[dict[str, Any]] = []
        for row in rows:
            normalized = normalize_filing(row)
            json_dt = parse_timestamp(normalized.get("acceptance_json"))
            sgml_dt = parse_timestamp(normalized.get("acceptance_sgml"))
            if json_dt and sgml_dt and json_dt != sgml_dt:
                conflicts.append(
                    {
                        "accession": normalized.get("accession"),
                        "acceptance_json": normalized.get("acceptance_json"),
                        "acceptance_sgml": normalized.get("acceptance_sgml"),
                    }
                )
        if conflicts:
            return _check_result(name, "INCONCLUSIVE", "PIT acceptance 시각 원천이 충돌해 판정할 수 없습니다.", {"conflicts": conflicts})
        return _check_result(name, "PASS", "PIT acceptance 시각 원천 간 충돌이 없습니다.")
    if name == "missing_value_null":
        placeholders = [key for key, value in fields.items() if value == 0]
        if placeholders:
            return _check_result(name, "FAIL", "누락값이 null 대신 0으로 대체되었습니다.", {"fields": placeholders})
        return _check_result(name, "PASS", "누락값은 null로 보존되었습니다.", {"missing_fields": [key for key, value in fields.items() if value is None]})
    if name == "working_capital_release":
        return _check_claim(
            name,
            _claim(result, "E4"),
            missing_reason="CFO와 운전자본 3개 태그의 비교열이 없어 회수 여부를 확인할 수 없습니다.",
            pass_reason="CFO 개선이 운전자본 회수에 대부분 의존하지 않습니다.",
            fail_reason="CFO 개선의 대부분이 운전자본 회수(지속성 의심)",
        )
    if name == "one_off_gain":
        result = _check_claim(
            name,
            _claim(result, "E5"),
            missing_reason="순이익 비교열 또는 일회성 항목 태그가 없어 점검할 수 없습니다.",
            pass_reason="양의 일회성 이익 기여의 증가분이 순이익 증가분의 30% 이하입니다.",
            fail_reason="이익 개선의 상당 부분이 일회성 항목",
        )
        result["advisory"] = True
        return result
    if name == "accrual_spike":
        return _check_claim(
            name,
            _claim(result, "E3"),
            missing_reason="순이익·CFO·평균 총자산 비교열이 없어 발생액 변화를 확인할 수 없습니다.",
            pass_reason="발생액/평균자산 증가가 2%p 이하입니다.",
            fail_reason="발생액/평균자산 증가가 2%p를 초과했습니다.",
        )
    # Future registry entries without implementations are advisory by default.
    result = _check_result(name, "INCONCLUSIVE", "미구현: 추가 검사를 수행하지 않았습니다.")
    result["advisory"] = True
    return result


def run_extra_checks(
    names: Iterable[str],
    fields: dict[str, Any],
    *,
    evaluation: dict[str, Any] | None = None,
    filings: Iterable[dict[str, Any]] | None = None,
    sec_client: SecClient | None = None,
    before_fetch: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    rows = list(filings or [])
    return [run_extra_check(name, fields, evaluation=evaluation, filings=rows,
                           sec_client=sec_client, before_fetch=before_fetch) for name in names]


CHECK_REGISTRY = REGISTERED_EXTRA_CHECKS
run_check = run_extra_check

