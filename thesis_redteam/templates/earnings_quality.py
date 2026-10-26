"""Deterministic cash-backed earnings-quality claims."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from typing import Any, Iterable, Mapping

from ..pit import filter_filings
from ..verifier import (
    decimal,
    decimal_string,
    overall_verdict,
    require_same_period,
    require_same_unit,
    require_usd_unit,
)


FOREIGN_ANNUAL_FORMS = {"20-F", "20-F/A", "40-F", "40-F/A"}
_NET_INCOME_NAMES = ("net_income", "net_income_comparative", "net_income_yoy")
_CFO_NAMES = ("cfo", "cfo_comparative", "cfo_yoy")
_ASSET_NAMES = ("assets_comparative", "assets", "total_assets")
_WORKING_CAPITAL_NAMES = {
    "accounts_receivable": (
        "accounts_receivable_change",
        "ar_change",
        "IncreaseDecreaseInAccountsReceivable",
    ),
    "inventories": (
        "inventory_change",
        "inventories_change",
        "IncreaseDecreaseInInventories",
    ),
    "accounts_payable": (
        "accounts_payable_change",
        "ap_change",
        "IncreaseDecreaseInAccountsPayable",
    ),
}
_ONE_OFF_TAGS = (
    "GainLossOnSaleOfPropertyPlantEquipment",
    "GainLossOnDispositionOfAssets",
    "GainsLossesOnExtinguishmentOfDebt",
    "IncomeLossFromDiscontinuedOperationsNetOfTax",
)


def _claim(
    claim_id: str,
    status: str,
    inputs: dict[str, Any],
    reason: str,
    outputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    claim = {"id": claim_id, "status": status, "inputs": inputs, "outputs": outputs or {}, "reason_ko": reason}
    if claim_id == "E5":
        claim["advisory"] = True
    return claim


def _latest_annual_form(
    fields: dict[str, Any],
    filings: Iterable[dict[str, Any]] | None,
    asof: Any | None,
) -> str | None:
    form = fields.get("latest_annual_form")
    if form:
        return str(form)
    if filings is None or asof is None:
        return None
    candidates = [
        row
        for row in filter_filings(filings, asof)
        if row.get("form") in {"10-K", "10-K/A", *FOREIGN_ANNUAL_FORMS}
    ]
    if not candidates:
        return None
    latest = max(
        candidates,
        key=lambda row: (
            row.get("conservative_acceptance") or row.get("filing_date") or row.get("filed") or "",
            row.get("filing_date") or row.get("filed") or "",
            row.get("accession") or row.get("accessionNumber") or "",
        ),
    )
    return str(latest.get("form")) if latest.get("form") else None


def _financial_sic(sic: Any) -> bool:
    try:
        value = int(str(sic).strip())
    except (TypeError, ValueError):
        return False
    return 6000 <= value <= 6799


def _rows(fields: Mapping[str, Any], names: Iterable[str]) -> list[dict[str, Any]]:
    for name in names:
        value = fields.get(name)
        if isinstance(value, list) and value:
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            current = value.get("current")
            if isinstance(current, dict):
                return [current]
    return []


def _explicit_pair(fields: Mapping[str, Any], field: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    groups = fields.get("earnings_quality_comparatives")
    if isinstance(groups, Mapping):
        value = groups.get(field)
        if isinstance(value, Mapping) and isinstance(value.get("current"), dict) and isinstance(value.get("prior"), dict):
            return value["current"], value["prior"]
    for name in (f"{field}_comparative", f"{field}_yoy"):
        value = fields.get(name)
        if isinstance(value, Mapping) and isinstance(value.get("current"), dict) and isinstance(value.get("prior"), dict):
            return value["current"], value["prior"]
    return None


def _same_filing_pair(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if len(rows) < 2:
        return None
    by_accession: dict[str | None, list[dict[str, Any]]] = {}
    for row in rows:
        accession = row.get("accession") or row.get("filing_accession")
        by_accession.setdefault(accession, []).append(row)

    grouped: dict[str | None, dict[str, dict[str, Any]]] = {}
    for accession, accession_rows in by_accession.items():
        selected_rows = _preferred_duration_rows(accession_rows)
        for row in selected_rows:
            end = row.get("period_end")
            if not end:
                continue
            periods = grouped.setdefault(accession, {})
            current = periods.get(str(end))
            if current is None or (row.get("filed") or "") > (current.get("filed") or ""):
                periods[str(end)] = row
    candidates: list[tuple[str, dict[str, dict[str, Any]]]] = []
    for accession, periods in grouped.items():
        if len(periods) >= 2:
            candidates.append((str(accession or ""), periods))
    if not candidates:
        return None
    _, periods = max(candidates, key=lambda item: max(item[1]))
    ordered = sorted(periods.values(), key=lambda row: str(row.get("period_end") or ""), reverse=True)
    return ordered[0], ordered[1]


def _pair(fields: Mapping[str, Any], field: str, names: Iterable[str]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    explicit = _explicit_pair(fields, field)
    if explicit:
        return explicit
    for name in names:
        for candidate_name in (f"{name}_comparative", f"{name}_yoy", name):
            value = fields.get(candidate_name)
            if isinstance(value, list):
                pair = _same_filing_pair([row for row in value if isinstance(row, dict)])
                if pair:
                    return pair
    return None


def _current(fields: Mapping[str, Any], names: Iterable[str]) -> dict[str, Any] | None:
    rows = _rows(fields, names)
    return max(rows, key=lambda row: str(row.get("period_end") or "")) if rows else None


def _same_period_if_present(*records: dict[str, Any]) -> None:
    populated = [record for record in records if record.get("period_start") is not None or record.get("period_end") is not None]
    if len(populated) >= 2:
        require_same_period(populated)


def _same_filing_if_present(*records: dict[str, Any]) -> None:
    if any(record.get("ttm_method") == "fy_plus_ytd" for record in records):
        return
    accessions = {
        record.get("accession") or record.get("filing_accession")
        for record in records
        if record.get("accession") or record.get("filing_accession")
    }
    if len(accessions) > 1:
        raise ValueError(f"filing mismatch: {sorted(accessions)}")


def _same_period_end_if_present(*records: dict[str, Any]) -> None:
    ends = {record.get("period_end") for record in records if record.get("period_end") is not None}
    if len(ends) > 1:
        raise ValueError(f"period-end mismatch: {sorted(ends)}")


def _duration_days(record: dict[str, Any]) -> int:
    try:
        start = date.fromisoformat(str(record.get("period_start"))[:10])
        end = date.fromisoformat(str(record.get("period_end"))[:10])
    except (TypeError, ValueError):
        return -1
    return (end - start).days


def _preferred_duration_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    duration_rows = [row for row in rows if row.get("period_start") and row.get("period_end")]
    if not duration_rows:
        return rows
    if not any(row.get("form") for row in duration_rows):
        return [
            row for row in duration_rows
            if 150 <= _duration_days(row) <= 380
        ]
    annual = [
        row for row in duration_rows
        if row.get("form") in {"10-K", "10-K/A"} and 350 <= _duration_days(row) <= 380
    ]
    if annual:
        return annual
    ytd = [
        row for row in duration_rows
        if row.get("form") in {"10-Q", "10-Q/A"} and 150 <= _duration_days(row) < 350
    ]
    return ytd


def _record(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return dict(record)


def _fmt_percent(value: Decimal, *, suffix: str = "%") -> str:
    percent = value * Decimal(100)
    sign = "−" if percent < 0 else "+" if percent > 0 else ""
    return f"{sign}{abs(percent).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}{suffix}"


def _fmt_ratio(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}배"


def _fmt_percent_one_decimal(value: Decimal) -> str:
    percent = value * Decimal(100)
    sign = "−" if percent < 0 else "+" if percent > 0 else ""
    return f"{sign}{abs(percent).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"


def _asset_averages(fields: Mapping[str, Any]) -> tuple[Decimal, Decimal, dict[str, Any]] | None:
    explicit = fields.get("average_total_assets") or fields.get("average_assets")
    if isinstance(explicit, Mapping):
        current = explicit.get("current")
        prior = explicit.get("prior")
        if current is not None and prior is not None:
            current_value = current.get("value") if isinstance(current, Mapping) else current
            prior_value = prior.get("value") if isinstance(prior, Mapping) else prior
            return decimal(current_value), decimal(prior_value), {"method": "provided_average_total_assets", "current": _record(current) if isinstance(current, dict) else current, "prior": _record(prior) if isinstance(prior, dict) else prior}

    pair = _pair(fields, "assets", _ASSET_NAMES)
    rows = _rows(fields, _ASSET_NAMES)
    if pair is None:
        return None
    current, prior = pair
    try:
        require_usd_unit((current, prior))
        current_average = (decimal(current["value"]) + decimal(prior["value"])) / Decimal(2)
        prior_average = current_average
    except (KeyError, ValueError, DecimalException):
        return None
    return current_average, prior_average, {
        "method": "same_filing_two_period_average",
        "current_assets": _record(current),
        "prior_assets": _record(prior),
    }


def _working_capital_pair(fields: Mapping[str, Any], key: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    working_capital = fields.get("working_capital")
    if isinstance(working_capital, Mapping):
        for candidate in (key, *_WORKING_CAPITAL_NAMES[key]):
            pair = _explicit_pair({"earnings_quality_comparatives": working_capital}, candidate)
            if pair:
                return pair
            rows = working_capital.get(candidate)
            if isinstance(rows, list):
                pair = _same_filing_pair([row for row in rows if isinstance(row, dict)])
                if pair:
                    return pair
    for candidate in (key, *_WORKING_CAPITAL_NAMES[key]):
        explicit = _explicit_pair(fields, candidate)
        if explicit:
            return explicit
    return _pair(fields, key, _WORKING_CAPITAL_NAMES[key])


def _one_off_pair(fields: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    explicit = _explicit_pair(fields, "one_off_items") or _explicit_pair(fields, "one_off")
    if explicit:
        return explicit
    rows: list[dict[str, Any]] = []
    for name in ("one_off_items", "one_off", "one_off_gain"):
        value = fields.get(name)
        if isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, dict))
        elif isinstance(value, Mapping):
            if isinstance(value.get("current"), dict) and isinstance(value.get("prior"), dict):
                return value["current"], value["prior"]
            for item in value.values():
                if isinstance(item, list):
                    rows.extend(row for row in item if isinstance(row, dict))
    for tag in _ONE_OFF_TAGS:
        value = fields.get(tag)
        if isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, dict))
    if not rows:
        return None
    by_accession: dict[str | None, list[dict[str, Any]]] = {}
    for row in rows:
        accession = row.get("accession") or row.get("filing_accession")
        by_accession.setdefault(accession, []).append(row)
    rows = [row for accession_rows in by_accession.values() for row in _preferred_duration_rows(accession_rows)]
    grouped: dict[str | None, dict[str, dict[str, Any]]] = {}
    for row in rows:
        end = row.get("period_end")
        if not end:
            continue
        accession = row.get("accession") or row.get("filing_accession")
        by_end = grouped.setdefault(accession, {})
        bucket = by_end.setdefault(str(end), {"value": Decimal(0), "unit": row.get("unit"), "period_start": row.get("period_start"), "period_end": end, "accession": accession, "form": row.get("form"), "filed": row.get("filed"), "tags_used": [], "components": []})
        try:
            bucket["value"] += decimal(row["value"])
            bucket["components"].append(row)
        except (KeyError, ValueError, DecimalException):
            return None
        tag = row.get("tag")
        if tag and tag not in bucket["tags_used"]:
            bucket["tags_used"].append(tag)
    candidates = [(accession, periods) for accession, periods in grouped.items() if len(periods) >= 2]
    if not candidates:
        return None
    _, periods = max(candidates, key=lambda item: max(item[1]))
    ordered = sorted(periods.values(), key=lambda row: str(row.get("period_end") or ""), reverse=True)
    for row in ordered[:2]:
        row["value"] = decimal_string(row["value"])
    return ordered[0], ordered[1]


def _signed_one_off_by_tag(row: Mapping[str, Any]) -> dict[str, Decimal]:
    """Assemble signed TTM amounts per tag before excluding any losses."""
    ttm = row.get("ttm_components")
    components = row.get("components")
    if ttm:
        terms = [(ttm[name], sign) for name, sign in
                 (("fy", 1), ("current_ytd", 1), ("prior_ytd", -1)) if name in ttm]
    elif components:
        terms = [(item, 1) for item in
                 (components.values() if isinstance(components, Mapping) else components)]
    else:
        return {str(row.get("tag") or "one_off"): decimal(row["value"])}
    amounts: dict[str, Decimal] = {}
    for component, sign in terms:
        for tag, value in _signed_one_off_by_tag(component).items():
            amounts[tag] = amounts.get(tag, Decimal(0)) + sign * value
    return amounts


def _positive_one_off_contribution(row: Mapping[str, Any]) -> Decimal:
    return sum((max(Decimal(0), value) for value in _signed_one_off_by_tag(row).values()), Decimal(0))


def _ttm_method(fields: Mapping[str, Any], *records: dict[str, Any] | None) -> str:
    record_methods = {
        str(record["ttm_method"])
        for record in records
        if isinstance(record, dict) and record.get("ttm_method")
    }
    # A field can fall back independently when one of its YTD components is
    # absent. Prefer the evidence attached to the rows used by this claim;
    # if a claim mixes methods, report the conservative FY-only fallback.
    if "transition_period" in record_methods:
        return "transition_period"
    if "fy_only" in record_methods:
        return "fy_only"
    if record_methods:
        return sorted(record_methods)[0]
    configured = fields.get("earnings_quality_ttm_method")
    if configured:
        return str(configured)
    return "fy_only"


def _pair_value(pair: tuple[dict[str, Any], dict[str, Any]] | None, index: int) -> Any:
    return pair[index].get("value") if pair and len(pair) > index else None


def _working_capital_output_values(
    pairs: Mapping[str, tuple[dict[str, Any], dict[str, Any]] | None],
) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "current": pair[0].get("value"),
            "prior": pair[1].get("value"),
        }
        for key, pair in pairs.items()
        if pair
    }


def _inconclusive(
    claim_id: str,
    reason: str,
    inputs: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _claim(claim_id, "INCONCLUSIVE", inputs or {}, reason, outputs or {})


def evaluate(
    fields: dict[str, Any],
    *,
    sic: Any | None = None,
    filings: Iterable[dict[str, Any]] | None = None,
    asof: Any | None = None,
    **_: Any,
) -> dict[str, Any]:
    latest_annual_form = _latest_annual_form(fields, filings, asof)
    if latest_annual_form in FOREIGN_ANNUAL_FORMS:
        reason = "해외 발행인(20-F/40-F)은 earnings_quality 대상 아님"
        claims = [_claim(claim_id, "NOT_APPLICABLE", {"latest_annual_form": latest_annual_form}, reason, {"ttm_method": None}) for claim_id in ("E1", "E2", "E3", "E4", "E5")]
        return {"verdict": "NOT_APPLICABLE", "claims": claims, "flags": []}
    if _financial_sic(sic):
        reason = "금융업은 earnings_quality 대상 아님"
        claims = [_claim(claim_id, "NOT_APPLICABLE", {"sic": str(sic)}, reason, {"ttm_method": None}) for claim_id in ("E1", "E2", "E3", "E4", "E5")]
        return {"verdict": "NOT_APPLICABLE", "claims": claims, "flags": []}

    claims: list[dict[str, Any]] = []
    net_pair = _pair(fields, "net_income", _NET_INCOME_NAMES)
    cfo_pair = _pair(fields, "cfo", _CFO_NAMES)
    current_net = _current(fields, _NET_INCOME_NAMES)
    current_cfo = _current(fields, _CFO_NAMES)
    ttm_method = _ttm_method(fields, current_net, current_cfo)

    if net_pair is None:
        claims.append(_inconclusive(
            "E1",
            "동일 공시의 TTM/FY 순이익 비교열이 없어 이익 개선을 계산할 수 없습니다.",
            outputs={"net_income_current": None, "net_income_prior": None, "ttm_method": ttm_method},
        ))
    elif _ttm_method(fields, *net_pair) == "transition_period":
        current, prior = net_pair
        claims.append(_inconclusive(
            "E1", "회계연도 변경(전환기간)으로 전년 비교 불가",
            {"current": _record(current), "prior": _record(prior)},
            {"net_income_current": current.get("value"), "net_income_prior": prior.get("value"),
             "current": _record(current), "prior": _record(prior), "ttm_method": "transition_period"},
        ))
    else:
        current, prior = net_pair
        try:
            require_usd_unit((current, prior))
            _same_filing_if_present(current, prior)
            current_value, prior_value = decimal(current["value"]), decimal(prior["value"])
            _same_period_if_present(current, prior) if current.get("period_start") == prior.get("period_start") and current.get("period_end") == prior.get("period_end") else None
            change = current_value - prior_value
            status = "PASS" if change > 0 else "FAIL"
            reason = f"TTM/FY 순이익 YoY 변화 {_fmt_percent(change / abs(prior_value)) if prior_value else decimal_string(change)}로 0보다 큽니다." if status == "PASS" else f"순이익이 전년 대비 {_fmt_percent(change / abs(prior_value)) if prior_value else decimal_string(change)}로 감소해 이익 개선이 확인되지 않습니다."
            rate = decimal_string(change / abs(prior_value)) if prior_value else None
            claims.append(_claim("E1", status, {"current": _record(current), "prior": _record(prior), "yoy_change": decimal_string(change), "yoy_rate": rate}, reason, {
                "net_income_current": current.get("value"),
                "net_income_prior": prior.get("value"),
                "yoy_change": decimal_string(change),
                "yoy_rate": rate,
                "current": _record(current),
                "prior": _record(prior),
                "ttm_method": _ttm_method(fields, current, prior),
            }))
        except (KeyError, ValueError, DecimalException) as exc:
            claims.append(_inconclusive("E1", "순이익 단위가 USD가 아니거나 비교값이 유효하지 않아 판정할 수 없습니다.", {"error": str(exc)}, {"net_income_current": current.get("value"), "net_income_prior": prior.get("value"), "ttm_method": _ttm_method(fields, current, prior)}))

    if current_net is None or current_cfo is None:
        claims.append(_inconclusive("E2", "동일 기간의 TTM 순이익·CFO가 모두 확인되지 않아 현금 동반을 계산할 수 없습니다.", outputs={
            "net_income_current": current_net.get("value") if current_net else None,
            "net_income_prior": _pair_value(net_pair, 1),
            "cfo_current": current_cfo.get("value") if current_cfo else None,
            "cfo_prior": _pair_value(cfo_pair, 1),
            "cfo_ni_ratio": None,
            "ratio": None,
            "ttm_method": ttm_method,
        }))
    else:
        try:
            require_usd_unit((current_net, current_cfo))
            _same_period_if_present(current_net, current_cfo)
            _same_filing_if_present(current_net, current_cfo)
            net_value, cfo_value = decimal(current_net["value"]), decimal(current_cfo["value"])
            if net_value <= 0:
                claims.append(_inconclusive("E2", "이익이 음수라 비율 무의미", {"net_income": _record(current_net), "cfo": _record(current_cfo)}, {
                    "net_income": current_net.get("value"),
                    "net_income_current": current_net.get("value"),
                    "net_income_prior": _pair_value(net_pair, 1),
                    "cfo": current_cfo.get("value"),
                    "cfo_current": current_cfo.get("value"),
                    "cfo_prior": _pair_value(cfo_pair, 1),
                    "cfo_ni_ratio": None,
                    "ratio": None,
                    "ttm_method": _ttm_method(fields, current_net, current_cfo),
                }))
            else:
                ratio = cfo_value / net_value
                status = "PASS" if ratio >= Decimal("0.8") else "FAIL"
                reason = f"CFO/순이익 {_fmt_ratio(ratio)}로 0.8 이상입니다." if status == "PASS" else f"CFO/순이익 {_fmt_ratio(ratio)}로 0.8 미만입니다."
                claims.append(_claim("E2", status, {"net_income": _record(current_net), "cfo": _record(current_cfo), "ratio": decimal_string(ratio)}, reason, {
                    "cfo_ni_ratio": decimal_string(ratio),
                    "ratio": decimal_string(ratio),
                    "cfo": current_cfo.get("value"),
                    "net_income": current_net.get("value"),
                    "cfo_current": current_cfo.get("value"),
                    "cfo_prior": _pair_value(cfo_pair, 1),
                    "net_income_current": current_net.get("value"),
                    "net_income_prior": _pair_value(net_pair, 1),
                    "ttm_method": _ttm_method(fields, current_net, current_cfo),
                }))
        except (KeyError, ValueError, DecimalException) as exc:
            claims.append(_inconclusive("E2", "순이익·CFO의 단위 또는 기간이 일치하지 않아 비율을 계산할 수 없습니다.", {"error": str(exc)}, {
                "net_income_current": current_net.get("value"),
                "cfo_current": current_cfo.get("value"),
                "cfo_ni_ratio": None,
                "ttm_method": _ttm_method(fields, current_net, current_cfo),
            }))

    assets = _asset_averages(fields)
    if net_pair is None or cfo_pair is None or assets is None:
        claims.append(_inconclusive("E3", "동일 공시의 순이익·CFO 비교열과 평균 총자산이 모두 확인되지 않아 발생액 변화를 계산할 수 없습니다.", outputs={
            "net_income_current": net_pair[0].get("value") if net_pair else None,
            "net_income_prior": net_pair[1].get("value") if net_pair else None,
            "cfo_current": cfo_pair[0].get("value") if cfo_pair else None,
            "cfo_prior": cfo_pair[1].get("value") if cfo_pair else None,
            "accruals_current": None,
            "accruals_prior": None,
            "avg_assets_current": None,
            "avg_assets_prior": None,
            "delta": None,
            "delta_pp": None,
            "ttm_method": ttm_method,
        }))
    else:
        current_net_row, prior_net_row = net_pair
        current_cfo_row, prior_cfo_row = cfo_pair
        current_average, prior_average, asset_inputs = assets
        try:
            require_usd_unit((current_net_row, prior_net_row, current_cfo_row, prior_cfo_row))
            _same_period_if_present(current_net_row, current_cfo_row)
            _same_period_if_present(prior_net_row, prior_cfo_row)
            asset_records = [
                asset_inputs.get(name)
                for name in ("current_assets", "prior_assets")
                if isinstance(asset_inputs.get(name), dict)
            ]
            _same_filing_if_present(current_net_row, prior_net_row, current_cfo_row, prior_cfo_row, *asset_records)
            current_accrual = (decimal(current_net_row["value"]) - decimal(current_cfo_row["value"])) / current_average
            prior_accrual = (decimal(prior_net_row["value"]) - decimal(prior_cfo_row["value"])) / prior_average
            delta = current_accrual - prior_accrual
            status = "FAIL" if delta > Decimal("0.02") else "PASS"
            reason = f"발생액/평균자산 변화 {_fmt_percent(delta, suffix='%p')}로 2%p 이하입니다." if status == "PASS" else f"발생액/평균자산 변화 {_fmt_percent(delta, suffix='%p')}로 2%p를 초과했습니다."
            claims.append(_claim("E3", status, {"current_accrual": decimal_string(current_accrual), "prior_accrual": decimal_string(prior_accrual), "delta_pp": decimal_string(delta), "current_average_assets": decimal_string(current_average), "prior_average_assets": decimal_string(prior_average), "assets": asset_inputs}, reason, {
                "current_accrual": decimal_string(current_accrual),
                "prior_accrual": decimal_string(prior_accrual),
                "accruals_current": decimal_string(current_accrual),
                "accruals_prior": decimal_string(prior_accrual),
                "delta": decimal_string(delta),
                "delta_pp": decimal_string(delta),
                "current_average_assets": decimal_string(current_average),
                "prior_average_assets": decimal_string(prior_average),
                "avg_assets_current": decimal_string(current_average),
                "avg_assets_prior": decimal_string(prior_average),
                "net_income_current": current_net_row.get("value"),
                "net_income_prior": prior_net_row.get("value"),
                "cfo_current": current_cfo_row.get("value"),
                "cfo_prior": prior_cfo_row.get("value"),
                "ttm_method": _ttm_method(fields, current_net_row, prior_net_row, current_cfo_row, prior_cfo_row),
            }))
        except (KeyError, ValueError, DecimalException) as exc:
            claims.append(_inconclusive("E3", "발생액 또는 평균 총자산의 단위·분모가 유효하지 않아 판정할 수 없습니다.", {"error": str(exc)}, {
                "net_income_current": current_net_row.get("value"),
                "net_income_prior": prior_net_row.get("value"),
                "cfo_current": current_cfo_row.get("value"),
                "cfo_prior": prior_cfo_row.get("value"),
                "accruals_current": None,
                "accruals_prior": None,
                "avg_assets_current": decimal_string(current_average),
                "avg_assets_prior": decimal_string(prior_average),
                "delta": None,
                "delta_pp": None,
                "ttm_method": _ttm_method(fields, current_net_row, prior_net_row, current_cfo_row, prior_cfo_row),
            }))

    if cfo_pair is None:
        claims.append(_inconclusive("E4", "동일 공시의 CFO 비교열이 없어 운전자본 회수 여부를 계산할 수 없습니다.", outputs={
            "delta_cfo": None,
            "cfo_current": None,
            "cfo_prior": None,
            "working_capital_contribution": None,
            "working_capital_share": None,
            "working_capital_components": {},
            "ttm_method": ttm_method,
        }))
    else:
        wc_pairs = {key: _working_capital_pair(fields, key) for key in _WORKING_CAPITAL_NAMES}
        if any(pair is None for pair in wc_pairs.values()):
            claims.append(_inconclusive("E4", "운전자본 3개 태그(매출채권·재고·매입채무) 중 누락값이 있어 판정할 수 없습니다.", outputs={
                "delta_cfo": None,
                "cfo_current": cfo_pair[0].get("value"),
                "cfo_prior": cfo_pair[1].get("value"),
                "working_capital_contribution": None,
                "working_capital_share": None,
                "working_capital_components": _working_capital_output_values(wc_pairs),
                "ttm_method": _ttm_method(fields, cfo_pair[0], cfo_pair[1]),
            }))
        else:
            current_cfo_row, prior_cfo_row = cfo_pair
            rows = [current_cfo_row, prior_cfo_row]
            rows.extend(row for pair in wc_pairs.values() for row in pair or ())
            try:
                require_usd_unit(rows)
                for pair in wc_pairs.values():
                    if pair:
                        _same_period_if_present(current_cfo_row, pair[0])
                        _same_period_if_present(prior_cfo_row, pair[1])
                _same_filing_if_present(*rows)
                delta_cfo = decimal(current_cfo_row["value"]) - decimal(prior_cfo_row["value"])
                contribution = sum((decimal(pair[0]["value"]) - decimal(pair[1]["value"]) for pair in wc_pairs.values() if pair), Decimal(0))
                share = contribution / delta_cfo if delta_cfo > 0 else Decimal(0)
                status = "FAIL" if delta_cfo > 0 and contribution > Decimal("0.7") * delta_cfo else "PASS"
                reason = "CFO 개선의 대부분이 운전자본 회수(지속성 의심)" if status == "FAIL" else f"운전자본 기여 {_fmt_percent_one_decimal(share)}로 70% 이하입니다."
                working_capital_components = {
                    key: {
                        "current": pair[0].get("value"),
                        "prior": pair[1].get("value"),
                        "change": decimal_string(decimal(pair[0]["value"]) - decimal(pair[1]["value"])),
                    }
                    for key, pair in wc_pairs.items()
                    if pair
                }
                claims.append(_claim("E4", status, {"delta_cfo": decimal_string(delta_cfo), "working_capital_contribution": decimal_string(contribution), "working_capital_share": decimal_string(share), "working_capital": {key: {"current": _record(pair[0]), "prior": _record(pair[1])} for key, pair in wc_pairs.items() if pair}}, reason, {
                    "delta_cfo": decimal_string(delta_cfo),
                    "working_capital_contribution": decimal_string(contribution),
                    "working_capital_share": decimal_string(share),
                    "cfo_current": current_cfo_row.get("value"),
                    "cfo_prior": prior_cfo_row.get("value"),
                    "working_capital_components": working_capital_components,
                    "working_capital": working_capital_components,
                    "ttm_method": _ttm_method(
                        fields,
                        current_cfo_row,
                        prior_cfo_row,
                        *(row for pair in wc_pairs.values() if pair for row in pair),
                    ),
                }))
            except (KeyError, ValueError, DecimalException) as exc:
                claims.append(_inconclusive("E4", "CFO 또는 운전자본 태그의 단위·기간이 일치하지 않아 판정할 수 없습니다.", {"error": str(exc)}, {
                    "delta_cfo": None,
                    "cfo_current": current_cfo_row.get("value"),
                    "cfo_prior": prior_cfo_row.get("value"),
                    "working_capital_contribution": None,
                    "working_capital_share": None,
                    "working_capital_components": _working_capital_output_values(wc_pairs),
                    "ttm_method": _ttm_method(
                        fields,
                        current_cfo_row,
                        prior_cfo_row,
                        *(row for pair in wc_pairs.values() if pair for row in pair),
                    ),
                }))

    one_off_pair = _one_off_pair(fields)
    if net_pair is None or one_off_pair is None:
        claims.append(_inconclusive("E5", "동일 공시의 순이익 비교열 또는 일회성 이익 태그가 없어 점검할 수 없습니다.", outputs={
            "net_income_current": net_pair[0].get("value") if net_pair else None,
            "net_income_prior": net_pair[1].get("value") if net_pair else None,
            "one_off_current": one_off_pair[0].get("value") if one_off_pair else None,
            "one_off_prior": one_off_pair[1].get("value") if one_off_pair else None,
            "one_off_items": None,
            "ttm_method": ttm_method,
        }))
    else:
        current_net_row, prior_net_row = net_pair
        current_one_off, prior_one_off = one_off_pair
        try:
            require_usd_unit((current_net_row, prior_net_row, current_one_off, prior_one_off))
            _same_period_if_present(current_net_row, current_one_off)
            _same_period_if_present(prior_net_row, prior_one_off)
            _same_filing_if_present(current_net_row, prior_net_row, current_one_off, prior_one_off)
            increase = decimal(current_net_row["value"]) - decimal(prior_net_row["value"])
            current_positive = _positive_one_off_contribution(current_one_off)
            prior_positive = _positive_one_off_contribution(prior_one_off)
            one_off_increase = current_positive - prior_positive
            share = one_off_increase / increase if increase > 0 else Decimal(0)
            status = "FAIL" if increase > 0 and one_off_increase > Decimal("0.3") * increase else "PASS"
            reason = "이익 개선의 상당 부분이 일회성 항목" if status == "FAIL" else f"일회성 이익 기여 증가분/순이익 증가분 {_fmt_percent_one_decimal(share)}로 30% 이하입니다."
            contribution = {"one_off_positive_current": decimal_string(current_positive), "one_off_positive_prior": decimal_string(prior_positive), "one_off_increase": decimal_string(one_off_increase)}
            claims.append(_claim("E5", status, {"net_income_increase": decimal_string(increase), **contribution, "one_off_share": decimal_string(share), "one_off_current": _record(current_one_off), "one_off_prior": _record(prior_one_off)}, reason, {
                **contribution,
                "net_income_current": current_net_row.get("value"),
                "net_income_prior": prior_net_row.get("value"),
                "one_off_current": current_one_off.get("value"),
                "one_off_prior": prior_one_off.get("value"),
                "one_off_items": {"current": current_one_off.get("value"), "prior": prior_one_off.get("value")},
                "net_income_increase": decimal_string(increase),
                "one_off_share": decimal_string(share),
                "ttm_method": _ttm_method(fields, current_net_row, prior_net_row, current_one_off, prior_one_off),
            }))
        except (KeyError, ValueError, DecimalException) as exc:
            claims.append(_inconclusive("E5", "일회성 항목의 단위 또는 비교기간이 유효하지 않아 판정할 수 없습니다.", {"error": str(exc)}, {
                "net_income_current": current_net_row.get("value"),
                "net_income_prior": prior_net_row.get("value"),
                "one_off_current": current_one_off.get("value"),
                "one_off_prior": prior_one_off.get("value"),
                "one_off_items": {"current": current_one_off.get("value"), "prior": prior_one_off.get("value")},
                "ttm_method": _ttm_method(fields, current_net_row, prior_net_row, current_one_off, prior_one_off),
            }))

    substantive_claims = [claim for claim in claims if not (claim.get("id") == "E5" and claim.get("status") == "INCONCLUSIVE")]
    return {"verdict": overall_verdict(substantive_claims), "claims": claims, "flags": []}
