"""B03 shareholder-return claims."""

from __future__ import annotations

from decimal import Decimal, DecimalException, ROUND_HALF_UP
from typing import Any, Iterable

from ..pit import filter_filings
from ..verifier import decimal, decimal_string, issuance_notes, overall_verdict, require_same_period, require_same_unit, require_usd_unit


DEBT_DEFINITION = "CommercialPaper + LongTermDebtCurrent + LongTermDebtNoncurrent; cash and assets as reported"
FOREIGN_ANNUAL_FORMS = {"20-F", "20-F/A", "40-F", "40-F/A"}


def _claim(
    claim_id: str,
    status: str,
    inputs: dict[str, Any],
    reason: str,
    outputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {"id": claim_id, "status": status, "inputs": inputs, "outputs": outputs or {}, "reason_ko": reason}


def _by_period(fields: dict[str, Any], names: tuple[str, ...]) -> tuple[dict[str, Any], ...] | None:
    lists = [fields.get(name) for name in names]
    if any(not rows for rows in lists):
        return None
    maps = []
    for rows in lists:
        maps.append({(row.get("period_start"), row.get("period_end")): row for row in rows})
    common = set(maps[0])
    for mapping in maps[1:]:
        common &= set(mapping)
    if not common:
        return None
    period = sorted(common, key=lambda item: item[1] or "")[-1]
    return tuple(mapping[period] for mapping in maps)


def _record_inputs(*records: dict[str, Any], **derived: Any) -> dict[str, Any]:
    result: dict[str, Any] = {f"value_{index + 1}": record for index, record in enumerate(records)}
    result.update({key: decimal_string(value) if isinstance(value, Decimal) else value for key, value in derived.items()})
    return result


def _format_money(value: Any) -> str:
    amount = decimal(value)
    sign = "−" if amount < 0 else ""
    absolute = abs(amount)
    if absolute >= Decimal("1000000000"):
        billions = absolute / Decimal("1000000000")
        return f"{sign}${billions.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}B"
    millions = absolute / Decimal("1000000")
    return f"{sign}${millions.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}M"


def _format_percent(value: Any, *, suffix: str = "%") -> str:
    percent = decimal(value) * Decimal(100)
    if percent < 0:
        sign = "−"
    elif percent > 0:
        sign = "+"
    else:
        sign = ""
    return f"{sign}{abs(percent).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}{suffix}"


def _latest_10k_accession(*rows: dict[str, Any]) -> str | None:
    candidates = [
        row
        for row in rows
        if row.get("accession") and row.get("form") in {"10-K", "10-K/A"}
    ]
    if not candidates:
        return None
    latest = max(
        candidates,
        key=lambda row: (
            row.get("filed") or "",
            1 if row.get("form") == "10-K/A" else 0,
            row.get("accession") or "",
        ),
    )
    return latest["accession"]


def _latest_two_from_filing(
    rows: list[dict[str, Any]] | None,
    accession: str | None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if not rows or not accession:
        return None
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("accession") != accession or not row.get("period_end"):
            continue
        end = row["period_end"]
        current = grouped.get(end)
        if current is None or (row.get("filed") or "") > (current.get("filed") or ""):
            grouped[end] = row
    ends = sorted(grouped, reverse=True)
    if len(ends) < 2:
        return None
    return grouped[ends[0]], grouped[ends[1]]


def _by_filing_period(rows: list[dict[str, Any]], accession: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("accession") != accession or not row.get("period_end"):
            continue
        end = row["period_end"]
        current = result.get(end)
        if current is None or (row.get("filed") or "") > (current.get("filed") or ""):
            result[end] = row
    return result


def _public_float_row(fields: dict[str, Any], *c1_rows: dict[str, Any]) -> dict[str, Any] | None:
    rows = [
        row
        for row in (fields.get("public_float") or [])
        if row.get("form") in {"10-K", "10-K/A"}
    ]
    if not rows:
        return None
    accessions = {row.get("accession") for row in c1_rows if row.get("accession")}
    matching = [row for row in rows if not accessions or row.get("accession") in accessions]
    return max(matching or rows, key=lambda row: (row.get("period_end") or "", row.get("filed") or ""))


def _share_row_for_period(fields: dict[str, Any], period_end: Any, *c1_rows: dict[str, Any]) -> dict[str, Any] | None:
    accessions = {row.get("accession") for row in c1_rows if row.get("accession")}
    rows = [
        row
        for row in (fields.get("shares") or [])
        if row.get("period_end") == period_end
        and row.get("tag") == "CommonStockSharesOutstanding"
        and (not accessions or row.get("accession") in accessions)
    ]
    return max(rows, key=lambda row: (row.get("filed") or "", row.get("accession") or "")) if rows else None


def _financial_sic(sic: Any) -> bool:
    try:
        value = int(str(sic).strip())
    except (TypeError, ValueError):
        return False
    return 6000 <= value <= 6799


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
    annual_forms = {"10-K", "10-K/A", *FOREIGN_ANNUAL_FORMS}
    candidates = [row for row in filter_filings(filings, asof) if row.get("form") in annual_forms]
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


def _split_info(fields: dict[str, Any]) -> tuple[bool, str | None]:
    rows = fields.get("shares") or []
    accession = _latest_10k_accession(*rows)
    pair = _latest_two_from_filing(rows, accession)
    if pair is None:
        return False, None
    try:
        current = decimal(pair[0]["value"])
        prior = decimal(pair[1]["value"])
        if current <= 0 or prior <= 0:
            return False, None
        ratio = current / prior
    except (KeyError, ValueError, DecimalException):
        return False, None
    for factor in (Decimal("2"), Decimal("3"), Decimal("4"), Decimal("5"), Decimal("10"), Decimal("0.5"), Decimal("0.3333333333333333333333333333"), Decimal("0.25"), Decimal("0.2"), Decimal("0.1")):
        if abs(ratio - factor) / factor <= Decimal("0.01"):
            return True, decimal_string(factor)
    return False, None


def evaluate(
    fields: dict[str, Any],
    *,
    market_cap: Any | None = None,
    sic: Any | None = None,
    price_info: dict[str, Any] | None = None,
    filings: Iterable[dict[str, Any]] | None = None,
    asof: Any | None = None,
) -> dict[str, Any]:
    latest_annual_form = _latest_annual_form(fields, filings, asof)
    if latest_annual_form in FOREIGN_ANNUAL_FORMS:
        reason = "해외 발행인(20-F/40-F)은 B03 대상 아님"
        claims = [
            _claim(claim_id, "NOT_APPLICABLE", {"latest_annual_form": latest_annual_form}, reason)
            for claim_id in ("C1", "C2", "C3", "C4")
        ]
        return {"verdict": "NOT_APPLICABLE", "claims": claims, "flags": []}

    if _financial_sic(sic):
        reason = "금융업은 B03 대상 아님"
        claims = [_claim(claim_id, "NOT_APPLICABLE", {"sic": str(sic)}, reason) for claim_id in ("C1", "C2", "C3", "C4")]
        return {"verdict": "NOT_APPLICABLE", "claims": claims, "flags": []}

    claims: list[dict[str, Any]] = []
    flags: list[str] = []

    if fields.get("issuance_unconfirmed"):
        flags.append("possible_dilution_unconfirmed")
        claims.append(_claim("C1", "INCONCLUSIVE", {"issuance_unconfirmed": fields["issuance_unconfirmed"], "method": "none"}, "발행 현금 교차확인 실패(우선주/비현금 가능)"))
    elif not fields.get("common_issuance"):
        claims.append(_claim("C1", "INCONCLUSIVE", {"method": "none"}, "발행 현금 미확인: 공통주 발행 태그가 없어 순현금환원을 판정할 수 없습니다."))
    else:
        c1_rows = _by_period(fields, ("repurchase", "common_issuance"))
        if c1_rows is None:
            claims.append(_claim("C1", "INCONCLUSIVE", {"method": "none"}, "TTM 보통주 매입·발행 현금이 같은 기간으로 모두 확인되지 않아 판정할 수 없습니다."))
        else:
            repurchase, issuance = c1_rows
            try:
                require_usd_unit(c1_rows)
                require_same_period(c1_rows)
                net = decimal(repurchase["value"]) - decimal(issuance["value"])
                inputs = _record_inputs(
                    repurchase,
                    issuance,
                    net_return=net,
                    issuance_tag=issuance.get("tag"),
                    issuance_tags_used=issuance.get("tags_used", [issuance.get("tag")]),
                    issuance_scope=issuance.get("issuance_scope", "common_specific"),
                    ttm_method=repurchase.get("ttm_method") or issuance.get("ttm_method") or fields.get("ttm_method"),
                )
                public_float_row = _public_float_row(fields, repurchase, issuance)
                public_float_value = None
                if public_float_row is not None:
                    try:
                        require_usd_unit((public_float_row,))
                        public_float_value = decimal(public_float_row["value"])
                    except (KeyError, ValueError):
                        public_float_row = None
                method = (
                    "price_market_cap"
                    if market_cap not in (None, "") and decimal(market_cap) > 0
                    else "public_float_bound"
                    if public_float_value is not None and public_float_value > 0
                    else "none"
                )
                inputs["method"] = method
                if public_float_row is not None:
                    inputs["public_float"] = public_float_row.get("value")
                    inputs["public_float_record"] = public_float_row
                    inputs["public_float_period_end"] = public_float_row.get("period_end")
                if issuance.get("corroborating_tag"):
                    inputs["corroborating_tag"] = issuance["corroborating_tag"]
                split_flag, split_factor = _split_info(fields)
                if split_flag:
                    inputs["split_flag"] = True
                    inputs["split_factor"] = split_factor
                if price_info:
                    inputs.update({key: value for key, value in price_info.items() if key != "price_receipt"})
                share_row = _share_row_for_period(fields, repurchase.get("period_end"), repurchase, issuance)
                output_price = price_info.get("price_close") if price_info else None
                if output_price is None and market_cap not in (None, "") and share_row is not None:
                    output_price = decimal_string(decimal(market_cap) / decimal(share_row["value"]))
                outputs: dict[str, Any] = {
                    "Y": None,
                    "net_return": decimal_string(net),
                    "market_cap": decimal_string(market_cap) if market_cap not in (None, "") else None,
                    "price": output_price,
                    "shares": share_row.get("value") if share_row else None,
                }
                notes: list[str] = issuance_notes(issuance)
                if price_info and price_info.get("price_note"):
                    notes.append(str(price_info["price_note"]))
                if split_flag and "분할조정 종가 가능성" not in notes:
                    notes.append("분할조정 종가 가능성")
                note_suffix = f" ({' · '.join(notes)})" if notes else ""
                if net <= 0:
                    claims.append(_claim("C1", "FAIL", inputs, f"TTM 보통주 매입 {_format_money(repurchase['value'])}에서 발행 {_format_money(issuance['value'])}를 차감한 순현금환원이 {_format_money(net)}으로 0보다 크지 않습니다.{note_suffix}", outputs))
                elif method == "public_float_bound":
                    y_float = net / public_float_value
                    inputs["Y_float"] = decimal_string(y_float)
                    outputs["Y_float"] = decimal_string(y_float)
                    if y_float < Decimal("0.03"):
                        claims.append(_claim("C1", "FAIL", inputs, f"Y_float={_format_percent(y_float)}로 공개유통 시가총액(상한 추정) 기준으로도 3% 미달입니다.{note_suffix}", outputs))
                    else:
                        claims.append(_claim("C1", "INCONCLUSIVE", inputs, f"Y_float={_format_percent(y_float)}이지만 시가총액 확인 필요입니다.{note_suffix}", outputs))
                elif method == "none":
                    claims.append(_claim("C1", "INCONCLUSIVE", {**inputs, "market_cap": market_cap}, f"순현금환원 {_format_money(net)}은 양수지만 당시 시가총액 입력이 없어 Y(3%)를 계산할 수 없습니다.{note_suffix}", outputs))
                else:
                    y = net / decimal(market_cap)
                    status = "PASS" if y >= Decimal("0.03") else "FAIL"
                    outputs["Y"] = decimal_string(y)
                    reason = (f"Y={_format_percent(y)}로 3% 이상입니다." if status == "PASS" else f"Y={_format_percent(y)}로 3% 미만입니다.") + note_suffix
                    claims.append(_claim("C1", status, {**inputs, "market_cap": decimal_string(market_cap), "Y": decimal_string(y)}, reason, outputs))
            except ValueError as exc:
                claims.append(_claim("C1", "INCONCLUSIVE", {"error": str(exc), "method": "none"}, "단위 또는 기간이 일치하지 않아 순현금환원을 계산할 수 없습니다."))

    share_rows = fields.get("shares") or []
    share_accession = _latest_10k_accession(*share_rows)
    share_pair = _latest_two_from_filing(share_rows, share_accession)
    if share_pair is None:
        claims.append(_claim("C2", "INCONCLUSIVE", {}, "최신 기준일 전 10-K의 동일 비교열에서 두 시점의 기간말 보통주 발행주식수가 확인되지 않아 YoY 감소율을 계산할 수 없습니다."))
    else:
        current, prior = share_pair
        try:
            require_same_unit((current, prior))
            current_value = decimal(current["value"])
            prior_value = decimal(prior["value"])
            if prior_value <= 0:
                raise ValueError("prior share count is not positive")
            change = current_value / prior_value - 1
            status = "PASS" if change <= Decimal("-0.02") else "FAIL"
            reason = f"기간말 보통주 수 YoY 변화율 {_format_percent(change)}로 2% 이상 감소했습니다." if status == "PASS" else f"기간말 보통주 수 YoY 변화율 {_format_percent(change)}로 2% 이상 감소하지 않았습니다."
            c2_inputs = _record_inputs(current, prior, yoy_change=change, filing_accession=share_accession)
            c2_outputs = {
                "current_shares": decimal_string(current_value),
                "prior_shares": decimal_string(prior_value),
                "current": decimal_string(current_value),
                "prior": decimal_string(prior_value),
                "yoy_change": decimal_string(change),
            }
            if current.get("derived") == "issued - treasury" or prior.get("derived") == "issued - treasury":
                c2_inputs["derived"] = "issued - treasury"
            claims.append(_claim("C2", status, c2_inputs, reason, c2_outputs))
        except ValueError as exc:
            claims.append(_claim("C2", "INCONCLUSIVE", {"error": str(exc)}, "주식수 단위 또는 분모가 유효하지 않아 판정할 수 없습니다."))

    c3_rows = _by_period(fields, ("cfo", "capex", "repurchase"))
    if c3_rows is None:
        claims.append(_claim("C3", "INCONCLUSIVE", {}, "TTM CFO·설비투자·보통주 매입이 같은 기간으로 모두 확인되지 않아 판정할 수 없습니다."))
    else:
        cfo, capex, repurchase = c3_rows
        try:
            require_usd_unit(c3_rows)
            require_same_period(c3_rows)
            headroom = decimal(cfo["value"]) - decimal(capex["value"])
            threshold = decimal(repurchase["value"])
            status = "PASS" if headroom >= threshold else "FAIL"
            reason = f"TTM CFO-설비투자={_format_money(headroom)}로 보통주 매입 {_format_money(threshold)} 이상입니다." if status == "PASS" else f"TTM CFO-설비투자={_format_money(headroom)}로 보통주 매입 {_format_money(threshold)}보다 작습니다."
            claims.append(
                _claim(
                    "C3",
                    status,
                    _record_inputs(
                        cfo,
                        capex,
                        repurchase,
                        cfo_minus_capex=headroom,
                        ttm_method=repurchase.get("ttm_method") or cfo.get("ttm_method") or fields.get("ttm_method"),
                    ),
                    reason,
                )
            )
        except ValueError as exc:
            claims.append(_claim("C3", "INCONCLUSIVE", {"error": str(exc)}, "단위 또는 기간이 일치하지 않아 현금충당을 계산할 수 없습니다."))

    debt_rows = fields.get("interest_bearing_debt_comparative") or fields.get("interest_bearing_debt") or []
    cash_rows = fields.get("cash_comparative") or fields.get("cash") or []
    asset_rows = fields.get("assets_comparative") or fields.get("assets") or []
    c4_accession = _latest_10k_accession(*(debt_rows + cash_rows + asset_rows))
    c4_maps = [
        _by_filing_period(rows, c4_accession) if c4_accession else {}
        for rows in (debt_rows, cash_rows, asset_rows)
    ]
    common_ends = set(c4_maps[0]) & set(c4_maps[1]) & set(c4_maps[2])
    if len(common_ends) < 2:
        claims.append(_claim("C4", "INCONCLUSIVE", {}, "최신 기준일 전 10-K의 동일 비교열에서 두 기간의 이자부채·현금·총자산이 모두 확인되지 않아 순부채/자산 변화율을 계산할 수 없습니다."))
    else:
        ends = sorted(common_ends, reverse=True)[:2]
        current_end, prior_end = ends[0], ends[1]
        current = tuple(mapping[current_end] for mapping in c4_maps)
        prior = tuple(mapping[prior_end] for mapping in c4_maps)
        try:
            require_usd_unit(current + prior)
            current_ratio = (decimal(current[0]["value"]) - decimal(current[1]["value"])) / decimal(current[2]["value"])
            prior_ratio = (decimal(prior[0]["value"]) - decimal(prior[1]["value"])) / decimal(prior[2]["value"])
            delta = current_ratio - prior_ratio
            status = "PASS" if delta <= Decimal("0.02") else "FAIL"
            reason = f"순부채/자산 비율 YoY 변화가 {_format_percent(delta, suffix='%p')}로 2%p 이하입니다." if status == "PASS" else f"순부채/자산 비율 YoY 변화가 {_format_percent(delta, suffix='%p')}로 2%p를 초과합니다."
            debt_definition = current[0].get("definition") or prior[0].get("definition") or DEBT_DEFINITION
            outputs = {
                "current": {
                    "period_end": current_end,
                    "ratio": decimal_string(current_ratio),
                    "net_debt_assets": decimal_string(current_ratio),
                    "net_debt_assets_ratio": decimal_string(current_ratio),
                    "debt": current[0].get("value"),
                    "cash": current[1].get("value"),
                    "assets": current[2].get("value"),
                    "net_debt": decimal_string(decimal(current[0]["value"]) - decimal(current[1]["value"])),
                },
                "prior": {
                    "period_end": prior_end,
                    "ratio": decimal_string(prior_ratio),
                    "net_debt_assets": decimal_string(prior_ratio),
                    "net_debt_assets_ratio": decimal_string(prior_ratio),
                    "debt": prior[0].get("value"),
                    "cash": prior[1].get("value"),
                    "assets": prior[2].get("value"),
                    "net_debt": decimal_string(decimal(prior[0]["value"]) - decimal(prior[1]["value"])),
                },
                "delta_pp": decimal_string(delta),
                "current_ratio": decimal_string(current_ratio),
                "prior_ratio": decimal_string(prior_ratio),
                "current_debt": current[0].get("value"),
                "current_cash": current[1].get("value"),
                "current_assets": current[2].get("value"),
                "prior_debt": prior[0].get("value"),
                "prior_cash": prior[1].get("value"),
                "prior_assets": prior[2].get("value"),
            }
            claims.append(_claim("C4", status, {"current": _record_inputs(*current, ratio=current_ratio), "prior": _record_inputs(*prior, ratio=prior_ratio), "delta_pp": decimal_string(delta), "filing_accession": c4_accession, "debt_definition": debt_definition}, reason, outputs))
        except (ValueError, DecimalException):
            claims.append(_claim("C4", "INCONCLUSIVE", {}, "순부채/자산의 단위 또는 자산 분모가 유효하지 않아 판정할 수 없습니다."))

    return {"verdict": overall_verdict(claims), "claims": claims, "flags": flags}
