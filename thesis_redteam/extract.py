"""PIT-safe extraction of the shareholder-return XBRL fields."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable
import re

from .cash_flow import ABSENT_ISSUANCE_NOTE, common_issuance_absent, cash_flow_presentation
from .pit import allowed_accessions, asof_cutoff, filter_filings, filings_with_fact_history, parse_timestamp
from .sec_client import OfflineDataUnavailable, SecClient, SecNetworkError


COMMON_CASH_ISSUANCE_TOTAL_TAGS: tuple[str, ...] = (
    "ProceedsFromIssuanceOfCommonStock",
    "ProceedsFromIssuanceOfCommonStockNetOfIssuanceCosts",
)

COMMON_CASH_ISSUANCE_PLAN_TAGS: tuple[str, ...] = (
    "ProceedsFromStockOptionsExercised",
    "ProceedsFromIssuanceOfSharesUnderIncentiveAndShareBasedCompensationPlansIncludingStockOptions",
    "ProceedsFromStockPlans",
    "ProceedsFromEmployeeStockPurchasePlan",
)

ISSUANCE_TAGS: tuple[str, ...] = (
    *COMMON_CASH_ISSUANCE_TOTAL_TAGS,
    *COMMON_CASH_ISSUANCE_PLAN_TAGS,
    "StockIssuedDuringPeriodValueNewIssues",
    "ProceedsFromIssuanceOrSaleOfEquity",
)

DIRECT_COMMON_ISSUANCE_TAGS = COMMON_CASH_ISSUANCE_TOTAL_TAGS
EQUITY_STATEMENT_ISSUANCE_TAG = "StockIssuedDuringPeriodValueNewIssues"
CFO_CONTINUING_OPERATIONS_TAG = "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"
CAPEX_TAGS: tuple[str, ...] = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
    "PaymentsForCapitalImprovements",
    "PaymentsToAcquirePropertyPlantAndEquipmentAndOtherProductiveAssets",
)
CORROBORATING_EQUITY_PROCEEDS_TAGS: tuple[str, ...] = (
    "ProceedsFromOtherEquity",
    "ProceedsFromIssuanceOrSaleOfEquity",
    "ProceedsFromIssuanceOfCommonStockNetOfIssuanceCosts",
    "ProceedsFromIssuanceOfCommonStock",
)

FIELD_TAGS: dict[str, tuple[str, ...]] = {
    "repurchase": ("PaymentsForRepurchaseOfCommonStock",),
    "tax_withholding_acquisitions": ("PaymentsRelatedToTaxWithholdingForShareBasedCompensation",),
    "common_issuance": ISSUANCE_TAGS,
    "shares": ("CommonStockSharesOutstanding",),
    "public_float": ("EntityPublicFloat",),
    "cfo": ("NetCashProvidedByUsedInOperatingActivities", CFO_CONTINUING_OPERATIONS_TAG),
    "net_income": (
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ),
    "accounts_receivable_change": ("IncreaseDecreaseInAccountsReceivable",),
    "inventory_change": ("IncreaseDecreaseInInventories",),
    "accounts_payable_change": ("IncreaseDecreaseInAccountsPayable",),
    "one_off_items": (
        "GainLossOnSaleOfPropertyPlantEquipment",
        "GainLossOnDispositionOfAssets",
        "GainsLossesOnExtinguishmentOfDebt",
        "IncomeLossFromDiscontinuedOperationsNetOfTax",
    ),
    "capex": CAPEX_TAGS,
    "cash": ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
    "assets": ("Assets",),
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"),
    "operating_income": ("OperatingIncomeLoss",),
    "dividends": ("PaymentsOfDividends", "PaymentsOfDividendsCommonStock"),
}

PREMISE_FLOW_FIELDS = ("revenue", "operating_income", "dividends")

COMPARATIVE_FIELDS = {"shares", "cash", "assets"}
MONETARY_FIELDS = {
    "repurchase",
    "tax_withholding_acquisitions",
    "common_issuance",
    "public_float",
    "cfo",
    "net_income",
    "accounts_receivable_change",
    "inventory_change",
    "accounts_payable_change",
    "one_off_items",
    "capex",
    "cash",
    "assets",
    *PREMISE_FLOW_FIELDS,
}
ANNUAL_REPORT_FORMS = {
    "10-K",
    "10-K/A",
    "20-F",
    "20-F/A",
    "40-F",
    "40-F/A",
}

ANNUAL_MIN_DAYS = 350
ANNUAL_MAX_DAYS = 380
YTD_PREFERRED_MIN_DAYS = 150


def _fact_root(companyfacts: dict[str, Any]) -> dict[str, Any]:
    return companyfacts.get("facts", companyfacts).get("us-gaap", {})


def _dei_fact_root(companyfacts: dict[str, Any]) -> dict[str, Any]:
    return companyfacts.get("facts", companyfacts).get("dei", {})


def _allowed_fact(fact: dict[str, Any], accessions: set[str] | None, cutoff: Any) -> bool:
    accession = fact.get("accn") or fact.get("accession")
    if accessions is not None and accession not in accessions:
        return False
    filed = fact.get("filed")
    if cutoff is not None and filed:
        filed_dt = parse_timestamp(filed)
        if filed_dt is not None and filed_dt >= cutoff:
            return False
    return True


def _record(tag: str, unit: str, fact: dict[str, Any], cik: str | int | None) -> dict[str, Any]:
    record = {
        "value": str(fact["val"]),
        "unit": unit,
        "period_start": fact.get("start"),
        "period_end": fact.get("end"),
        "form": fact.get("form"),
        "accession": fact.get("accn") or fact.get("accession"),
        "tag": tag,
        "filed": fact.get("filed"),
        **({"cik": str(cik)} if cik is not None else {}),
    }
    for key in ("fy", "fp", "frame"):
        if fact.get(key) is not None:
            record[key] = fact[key]
    return record


def _records_for_tags(
    facts: dict[str, Any],
    tags: Iterable[str],
    *,
    accessions: set[str] | None,
    cutoff: Any,
    cik: str | int | None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for tag in tags:
        node = facts.get(tag, {})
        for unit, rows in node.get("units", {}).items():
            for fact in rows:
                if "val" in fact and _allowed_fact(fact, accessions, cutoff):
                    records.append(_record(tag, unit, fact, cik))
    return records


def _usd_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only monetary facts explicitly reported in USD."""
    return [row for row in records if row.get("unit") == "USD"]


def _select_preferred_monetary_records(
    facts: dict[str, Any],
    tags: tuple[str, ...],
    *,
    accessions: set[str] | None,
    cutoff: Any,
    cik: str | int | None,
    dedupe: bool = True,
) -> list[dict[str, Any]]:
    """Use the first USD tag in each filing, allowing tags to change over time.

    A discontinued primary tag must not hide newer filings using a fallback.
    Keep each filing's definition consistent across its comparative columns.
    """
    selected: list[dict[str, Any]] = []
    covered: set[Any] = set()
    for tag in tags:
        records = _usd_records(_records_for_tags(
            facts,
            (tag,),
            accessions=accessions,
            cutoff=cutoff,
            cik=cik,
        ))
        records = [row for row in records if row.get("accession") not in covered]
        if tag == CFO_CONTINUING_OPERATIONS_TAG:
            for record in records:
                record["definition"] = "계속영업 기준"
        selected.extend(records)
        covered.update(row.get("accession") for row in records)
    return _dedupe(selected) if dedupe else sorted(selected, key=_rank)


def _derived_share_records(
    facts: dict[str, Any],
    *,
    accessions: set[str] | None,
    cutoff: Any,
    cik: str | int | None,
) -> list[dict[str, Any]]:
    issued = _records_for_tags(
        facts,
        ("CommonStockSharesIssued",),
        accessions=accessions,
        cutoff=cutoff,
        cik=cik,
    )
    treasury = _records_for_tags(
        facts,
        ("TreasuryStockShares",),
        accessions=accessions,
        cutoff=cutoff,
        cik=cik,
    )
    treasury_by_key = {
        (row.get("accession"), row.get("period_end"), row.get("unit")): row
        for row in treasury
        if row.get("accession") and row.get("period_end")
    }
    result: list[dict[str, Any]] = []
    for issued_row in issued:
        if issued_row.get("period_start") is not None:
            continue
        key = (issued_row.get("accession"), issued_row.get("period_end"), issued_row.get("unit"))
        treasury_row = treasury_by_key.get(key)
        if treasury_row is None or treasury_row.get("period_start") is not None:
            continue
        try:
            value = Decimal(issued_row["value"]) - Decimal(treasury_row["value"])
        except (KeyError, InvalidOperation):
            continue
        if value <= 0:
            continue
        derived = dict(issued_row)
        derived.update(
            {
                "value": str(value),
                "tag": "CommonStockSharesOutstanding",
                "derived": "issued - treasury",
                "derived_from": ["CommonStockSharesIssued", "TreasuryStockShares"],
            }
        )
        result.append(derived)
    return result


def _rank(record: dict[str, Any]) -> tuple[str, int, str, str]:
    form_rank = {"10-K": 4, "10-K/A": 5, "10-Q": 3, "10-Q/A": 3}.get(record.get("form"), 1)
    return (
        record.get("period_end") or "",
        form_rank,
        record.get("filed") or "",
        record.get("accession") or "",
    )


def _filing_order(record: dict[str, Any]) -> tuple[int, str, str]:
    """Order facts by the filing that first disclosed their period."""
    filed = record.get("filed") or "9999-99-99"
    return (0 if record.get("filed") else 1, filed, record.get("accession") or "")


def _is_amendment(record: dict[str, Any]) -> bool:
    return str(record.get("form") or "").upper().endswith("/A")


def _dedupe(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first known fact, superseded only by a pre-as-of amendment.

    Companyfacts repeats prior-year comparative values in later filings. The
    later filing is not a point-in-time restatement, so it must not replace the
    earliest original disclosure. A 10-K/A or 10-Q/A is the explicit exception
    and wins when it was filed before the caller's cutoff.
    """
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for record in records:
        key = (
            record.get("unit"),
            record.get("period_start"),
            record.get("period_end"),
        )
        grouped.setdefault(key, []).append(record)

    selected: dict[tuple[Any, ...], dict[str, Any]] = {}
    for key, rows in grouped.items():
        amendments = [row for row in rows if _is_amendment(row)]
        if amendments:
            selected[key] = max(amendments, key=_filing_order)
        else:
            selected[key] = min(rows, key=_filing_order)
    return sorted(selected.values(), key=_rank)


def _select_issuance_records(
    facts: dict[str, Any],
    *,
    accessions: set[str] | None,
    cutoff: Any,
    cik: str | int | None,
    periods: set[tuple[Any, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Use cash-flow common issuance, or a corroborated equity-statement value."""
    records, _ = _select_issuance_with_metadata(
        facts,
        accessions=accessions,
        cutoff=cutoff,
        cik=cik,
        periods=periods,
    )
    return records


def _select_one_per_tag_context(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in records:
        key = (
            record.get("tag"),
            record.get("unit"),
            record.get("period_start"),
            record.get("period_end"),
            record.get("accession"),
        )
        current = grouped.get(key)
        if current is None or (record.get("filed") or "") > (current.get("filed") or ""):
            grouped[key] = record
    return list(grouped.values())


def _aggregate_common_cash_records(
    facts: dict[str, Any],
    *,
    accessions: set[str] | None,
    cutoff: Any,
    cik: str | int | None,
    dedupe: bool = True,
) -> list[dict[str, Any]]:
    """Aggregate common-stock cash proceeds without summing total and plan tags."""
    records = _select_one_per_tag_context(
        [
            row
            for row in _records_for_tags(
                facts,
                (*COMMON_CASH_ISSUANCE_TOTAL_TAGS, *COMMON_CASH_ISSUANCE_PLAN_TAGS),
                accessions=accessions,
                cutoff=cutoff,
                cik=cik,
            )
            if row.get("unit") == "USD"
        ]
    )
    by_context: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for record in records:
        key = (
            record.get("unit"),
            record.get("period_start"),
            record.get("period_end"),
            record.get("accession"),
        )
        by_context.setdefault(key, []).append(record)

    result: list[dict[str, Any]] = []
    for rows in by_context.values():
        totals = [row for row in rows if row.get("tag") in COMMON_CASH_ISSUANCE_TOTAL_TAGS]
        plans = [row for row in rows if row.get("tag") in COMMON_CASH_ISSUANCE_PLAN_TAGS]
        try:
            plan_total = sum((Decimal(row["value"]) for row in plans), Decimal(0))
        except (KeyError, InvalidOperation):
            plans, plan_total = [], Decimal(0)
        use_plans = False
        if totals:
            chosen = next(
                (row for row in totals if row.get("tag") == "ProceedsFromIssuanceOfCommonStock"),
                totals[0],
            )
            aggregate = dict(chosen)
            aggregate["tags_used"] = [chosen["tag"]]
            use_plans = bool(plans) and (Decimal(chosen["value"]) == 0 or Decimal(chosen["value"]) < plan_total)
        if not totals or use_plans:
            if not plans:
                continue
            aggregate = dict(plans[0])
            aggregate["value"] = str(plan_total)
            aggregate["tag"] = "+".join(row["tag"] for row in plans)
            aggregate["tags_used"] = [row["tag"] for row in plans]
            if use_plans:
                aggregate["issuance_note"] = "total tag 0/smaller than plan tags; used plan tags"
        aggregate["issuance_scope"] = "common_specific"
        result.append(aggregate)
    return _dedupe(result) if dedupe else sorted(result, key=_rank)


def _refine_repurchase_precision(
    records: list[dict[str, Any]],
    facts: dict[str, Any],
    *,
    accessions: set[str] | None,
    cutoff: Any,
    cik: str | int | None,
) -> list[dict[str, Any]]:
    """Equity cash can refine an existing rounded common-stock fact, never replace a missing one."""
    equity = _select_one_per_tag_context(_usd_records(_records_for_tags(
        facts, ("PaymentsForRepurchaseOfEquity",), accessions=accessions, cutoff=cutoff, cik=cik,
    )))
    by_context = {(row.get("accession"), row.get("period_start"), row.get("period_end")): row
                  for row in equity}
    result = []
    for common in records:
        precise = by_context.get((common.get("accession"), common.get("period_start"), common.get("period_end")))
        if (precise is not None and common.get("accession")
                and common.get("period_start") and common.get("period_end")):
            try:
                rounded, value = Decimal(common["value"]), Decimal(precise["value"])
                if (rounded > 0 and value > 0 and rounded % 100000000 == 0
                        and value % 100000000 != 0
                        and abs(rounded - value) / rounded <= Decimal("0.02")):
                    result.append({**precise, "precision_note":
                                   "rounded common-stock tag within 2% of equity cash; used more precise equity tag"})
                    continue
            except InvalidOperation:
                pass
        result.append(common)
    return result


def _within_one_percent(left: Any, right: Any) -> bool:
    try:
        left_decimal = Decimal(str(left))
        right_decimal = Decimal(str(right))
    except (InvalidOperation, ValueError):
        return False
    if left_decimal == right_decimal:
        return True
    scale = abs(right_decimal)
    return scale != 0 and abs(left_decimal - right_decimal) / scale <= Decimal("0.01")


def _same_period(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (left.get("period_start"), left.get("period_end")) == (right.get("period_start"), right.get("period_end"))


def _preferred_proceeds_tags(facts: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        tag
        for tag in facts
        if tag.startswith("ProceedsFromIssuanceOfPreferredStock")
        or tag.startswith("ProceedsFromIssuanceOfConvertiblePreferredStock")
    )


def _select_issuance_with_metadata(
    facts: dict[str, Any],
    *,
    accessions: set[str] | None,
    cutoff: Any,
    cik: str | int | None,
    periods: set[tuple[Any, Any]] | None = None,
    dedupe: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    records = _aggregate_common_cash_records(
        facts,
        accessions=accessions,
        cutoff=cutoff,
        cik=cik,
        dedupe=dedupe,
    )
    if periods:
        records = [
            record
            for record in records
            if (record.get("period_start"), record.get("period_end")) in periods
        ]
    if records:
        return records, None

    equity_records = _usd_records(_records_for_tags(
        facts,
        (EQUITY_STATEMENT_ISSUANCE_TAG,),
        accessions=accessions,
        cutoff=cutoff,
        cik=cik,
    ))
    if periods:
        equity_records = [
            record
            for record in equity_records
            if (record.get("period_start"), record.get("period_end")) in periods
        ]
    if not equity_records:
        return [], None
    selected_equity = _dedupe(equity_records) if dedupe else sorted(equity_records, key=_rank)
    corroborating = _usd_records(_records_for_tags(
        facts,
        CORROBORATING_EQUITY_PROCEEDS_TAGS,
        accessions=accessions,
        cutoff=cutoff,
        cik=cik,
    ))
    preferred_tags = _preferred_proceeds_tags(facts)
    preferred = _usd_records(_records_for_tags(
        facts,
        preferred_tags,
        accessions=accessions,
        cutoff=cutoff,
        cik=cik,
    )) if preferred_tags else []

    for equity in selected_equity:
        preferred_matches = [
            row for row in preferred
            if _same_period(equity, row) and _within_one_percent(equity.get("value"), row.get("value"))
        ]
        if preferred_matches:
            return [], {
                "tag": EQUITY_STATEMENT_ISSUANCE_TAG,
                "value": equity.get("value"),
                "preferred_tag": preferred_matches[0].get("tag"),
                "reason": "발행 현금 교차확인 실패(우선주/비현금 가능)",
            }
        cash_matches = [
            row for row in corroborating
            if _same_period(equity, row) and _within_one_percent(equity.get("value"), row.get("value"))
        ]
        if not cash_matches:
            return [], {
                "tag": EQUITY_STATEMENT_ISSUANCE_TAG,
                "value": equity.get("value"),
                "reason": "발행 현금 교차확인 실패(우선주/비현금 가능)",
            }
        equity["issuance_scope"] = "equity_cross_checked"
        equity["corroborating_tag"] = cash_matches[0].get("tag")
        equity["corroborating_value"] = cash_matches[0].get("value")
        equity["tags_used"] = [equity.get("tag")]
    return selected_equity, None


FLOW_FIELDS = ("repurchase", "common_issuance", "cfo", "capex")
EARNINGS_TTM_FIELDS = (
    "net_income",
    "accounts_receivable_change",
    "inventory_change",
    "accounts_payable_change",
)
EARNINGS_COMPARATIVE_FIELDS = (*EARNINGS_TTM_FIELDS, "cfo", "one_off_items")
EARNINGS_FLOW_FIELDS = EARNINGS_COMPARATIVE_FIELDS


def _day(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _next_day(value: Any) -> str | None:
    parsed = _day(value)
    return (parsed + timedelta(days=1)).isoformat() if parsed else None


def _duration_days(record: dict[str, Any]) -> int:
    start, end = _day(record.get("period_start")), _day(record.get("period_end"))
    if start is None or end is None or end < start:
        return -1
    return (end - start).days


def _is_annual_duration(record: dict[str, Any]) -> bool:
    duration = _duration_days(record)
    return ANNUAL_MIN_DAYS <= duration <= ANNUAL_MAX_DAYS


def _is_preferred_ytd_duration(record: dict[str, Any]) -> bool:
    duration = _duration_days(record)
    return YTD_PREFERRED_MIN_DAYS <= duration < ANNUAL_MIN_DAYS


def _is_short_ytd_duration(record: dict[str, Any]) -> bool:
    duration = _duration_days(record)
    return 0 <= duration < YTD_PREFERRED_MIN_DAYS


def _transition_period_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recognize stubs adjoining a full fiscal year, not quarterly footnotes.

    A transition starts immediately after a full FY and has no full FY with
    the same start. This also distinguishes a stub from an overlapping annual
    recast/pro-forma comparative ending on the same date (e.g. LHX 2019).
    """
    annual = [row for row in rows if row.get("form") in {"10-K", "10-K/A"}]
    full_years = [row for row in annual if _is_annual_duration(row)]
    next_starts = {_next_day(row.get("period_end")) for row in full_years}
    full_starts = {row.get("period_start") for row in full_years}
    return [
        {**row, "ttm_method": "transition_period"}
        for row in annual
        if _duration_days(row) > 0 and not _is_annual_duration(row)
        and row.get("period_start") in next_starts
        and row.get("period_start") not in full_starts
    ]


def _duration_comparative_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep annual/YTD facts and explicitly marked transition evidence.

    Companyfacts can contain quarter facts in a 10-K, sometimes with the same
    ``fp=FY`` marker as the annual fact. For 10-Q rows, keep the longest fact
    for each fiscal-year start and period end so a quarter is not chosen when
    a six- or nine-month YTD fact is also present.
    """
    annual = [
        row for row in rows
        if row.get("form") in {"10-K", "10-K/A"} and _is_annual_duration(row)
    ]
    quarterly = [row for row in rows if row.get("form") in {"10-Q", "10-Q/A"}]
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in quarterly:
        key = (row.get("accession"), row.get("period_start"), row.get("period_end"))
        grouped.setdefault(key, []).append(row)
    ytd: list[dict[str, Any]] = []
    for candidate_rows in grouped.values():
        preferred = [row for row in candidate_rows if _is_preferred_ytd_duration(row)]
        selected = preferred or [row for row in candidate_rows if _is_short_ytd_duration(row)]
        if selected:
            ytd.append(max(selected, key=lambda row: (_duration_days(row), row.get("filed") or "")))
    # Stubs are evidence of non-comparability, never accepted as annual facts.
    return sorted([*annual, *ytd, *_transition_period_records(rows)], key=_rank)


def _filing_sort_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        row.get("conservative_acceptance") or row.get("acceptanceDateTime") or row.get("filingDate") or row.get("filed") or "",
        row.get("filing_date") or row.get("filingDate") or row.get("filed") or "",
        row.get("accession") or row.get("accessionNumber") or "",
    )


def _latest_financial_filing(
    filings: list[dict[str, Any]] | None,
    asof: Any,
    records: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    if filings is not None and asof is not None:
        candidates = [
            row for row in filter_filings(filings, asof)
            if row.get("form") in {"10-K", "10-K/A", "10-Q", "10-Q/A"}
        ]
        if candidates:
            return max(candidates, key=_filing_sort_key)
    inferred: list[dict[str, Any]] = []
    for row in records:
        if row.get("form") not in {"10-K", "10-K/A", "10-Q", "10-Q/A"}:
            continue
        inferred.append(
            {
                "accession": row.get("accession"),
                "form": row.get("form"),
                "filing_date": row.get("filed"),
                "period_end": row.get("period_end"),
            }
        )
    return max(inferred, key=lambda row: (_filing_sort_key(row), row.get("period_end") or "")) if inferred else None


def _issuance_periods_for_ttm(
    rows: list[dict[str, Any]],
    *,
    filings: list[dict[str, Any]] | None,
    asof: Any,
) -> set[tuple[Any, Any]] | None:
    """Limit issuance candidates to the filing cohorts used by TTM."""
    if not rows:
        return None
    latest = _latest_financial_filing(filings, asof, rows)
    if latest is None:
        return None

    annual_filing = latest if latest.get("form") in {"10-K", "10-K/A"} else None
    if annual_filing is None:
        annual_candidates = [
            row for row in (filings or [])
            if row.get("form") in {"10-K", "10-K/A"}
        ]
        if annual_candidates:
            pit_annual = filter_filings(annual_candidates, asof) if asof is not None else annual_candidates
            annual_filing = max(pit_annual, key=_filing_sort_key) if pit_annual else None
    if annual_filing is None:
        annual_candidates = [
            row for row in rows
            if row.get("form") in {"10-K", "10-K/A"} and row.get("accession")
        ]
        if annual_candidates:
            annual_filing = max(
                annual_candidates,
                key=lambda row: (_filing_sort_key(row), row.get("period_end") or ""),
            )

    accessions = {
        row.get("accession")
        for row in (latest, annual_filing)
        if row is not None and row.get("accession")
    }
    if not accessions:
        return None
    periods = {
        (row.get("period_start"), row.get("period_end"))
        for row in rows
        if row.get("accession") in accessions
    }
    return periods or None


def _annual_record(rows: list[dict[str, Any]], filing: dict[str, Any] | None) -> dict[str, Any] | None:
    if filing is None:
        return None
    accession = filing.get("accession") or filing.get("accessionNumber")
    # Inferred history's reportDate can be a later cover-page share-count date.
    period_end = None if filing.get("history_source") else filing.get("reportDate") or filing.get("period_end")
    candidates = [
        row for row in rows
        if row.get("form") in {"10-K", "10-K/A"}
        and (not accession or row.get("accession") == accession)
        and row.get("period_start") and row.get("period_end")
        and _is_annual_duration(row)
        and (not period_end or row.get("period_end") == period_end)
    ]
    if not candidates and filing.get('form') == '10-K/A' and period_end:
        originals = [row for row in rows if row.get('form') == '10-K'
                     and row.get('period_end') == period_end and _is_annual_duration(row)]
        if originals:
            # A governance-only amendment does not withdraw the original cash
            # statement. Retain the original accession rather than relabel it.
            original = max(originals, key=_filing_order)
            return {**original, 'amendment_accession':accession,
                    'amendment_note':'same-FY original; amendment has no value for this component'}
    if not candidates:
        return None
    latest_end = max(row.get("period_end") for row in candidates)
    same_end = [row for row in candidates if row.get("period_end") == latest_end]
    return max(same_end, key=lambda row: (_duration_days(row), row.get("filed") or ""))


def _quarter_ytd_records(
    rows: list[dict[str, Any]],
    filing: dict[str, Any] | None,
    *,
    annual: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if filing is None:
        return None
    accession = filing.get("accession") or filing.get("accessionNumber")
    candidates = [
        row for row in rows
        if row.get("form") in {"10-Q", "10-Q/A"}
        and (not accession or row.get("accession") == accession)
        and row.get("period_start") and row.get("period_end")
    ]
    if not candidates:
        return None
    current_end = max(row.get("period_end") for row in candidates)
    current_start = _next_day(annual.get("period_end")) if annual else None
    prior_start = annual.get("period_start") if annual else None
    if current_start is None or prior_start is None:
        starts = sorted({row.get("period_start") for row in candidates if row.get("period_start")})
        if not starts:
            return None
        current_start = starts[-1]
        prior_start = starts[-2] if len(starts) >= 2 else starts[0]

    current_candidates = [
        row for row in candidates
        if row.get("period_end") == current_end and row.get("period_start") == current_start
    ]
    preferred_current = [row for row in current_candidates if _is_preferred_ytd_duration(row)]
    current_candidates = preferred_current or [row for row in current_candidates if _is_short_ytd_duration(row)]
    if not current_candidates:
        return None
    current = max(current_candidates, key=lambda row: (_duration_days(row), row.get("filed") or ""))
    prior_candidates = [
        row for row in candidates
        if row.get("period_end") < current_end and row.get("period_start") == prior_start
    ]
    preferred_prior = [row for row in prior_candidates if _is_preferred_ytd_duration(row)]
    prior_candidates = preferred_prior or [row for row in prior_candidates if _is_short_ytd_duration(row)]
    if not prior_candidates:
        return None
    prior_end = max(row.get("period_end") for row in prior_candidates)
    prior_same_end = [row for row in prior_candidates if row.get("period_end") == prior_end]
    prior = max(prior_same_end, key=lambda row: (_duration_days(row), row.get("filed") or ""))
    return current, prior


def _ttm_record(
    annual: dict[str, Any],
    current: dict[str, Any] | None,
    prior: dict[str, Any] | None,
    *,
    method: str,
) -> dict[str, Any] | None:
    if method == "fy_only":
        result = dict(annual)
        result["ttm_method"] = method
        result["ttm_components"] = {"fy": annual}
        return result
    if current is None or prior is None:
        return None
    try:
        value = Decimal(annual["value"]) + Decimal(current["value"]) - Decimal(prior["value"])
    except (KeyError, InvalidOperation):
        return None
    result = dict(current)
    result["value"] = str(value)
    result["period_start"] = _next_day(prior.get("period_end")) or annual.get("period_start")
    result["period_end"] = current.get("period_end")
    result["ttm_method"] = method
    result["ttm_components"] = {"fy": annual, "current_ytd": current, "prior_ytd": prior}
    tags = []
    for row in (annual, current, prior):
        for tag in row.get("tags_used", [row.get("tag")]):
            if tag and tag not in tags:
                tags.append(tag)
    if tags:
        result["tags_used"] = tags
    modes = {r.get('presentation') for r in (annual, current, prior)} - {None}
    problems = [r['presentation_change'] for r in (annual, current, prior) if r.get('presentation_change')]
    if len(modes) > 1 or problems:
        result['presentation_change'] = '; '.join(dict.fromkeys(problems)) or 'mixed gross/net cash-flow presentation'
    return result


def _latest_exact_period_record(
    rows: list[dict[str, Any]],
    period_start: Any,
    period_end: Any,
    *,
    forms: set[str] | None = None,
) -> dict[str, Any] | None:
    candidates = [
        row
        for row in rows
        if row.get("period_start") == period_start
        and row.get("period_end") == period_end
        and (forms is None or row.get("form") in forms)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: (_filing_sort_key(row), _rank(row)))


def _annual_period_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    annual = [
        row
        for row in rows
        if row.get("form") in {"10-K", "10-K/A"} and _is_annual_duration(row)
    ]
    by_period: dict[tuple[Any, Any], dict[str, Any]] = {}
    for row in annual:
        key = (row.get("period_start"), row.get("period_end"))
        current = by_period.get(key)
        if current is None or (_filing_sort_key(row), _rank(row)) > (_filing_sort_key(current), _rank(current)):
            by_period[key] = row
    return sorted(by_period.values(), key=lambda row: (row.get("period_end") or "", _filing_sort_key(row)))


def _ytd_record_for_start(
    rows: list[dict[str, Any]],
    period_start: str | None,
    duration_days: int,
) -> dict[str, Any] | None:
    if period_start is None:
        return None
    candidates = [
        row
        for row in rows
        if row.get("form") in {"10-Q", "10-Q/A"}
        and row.get("period_start") == period_start
        and _duration_days(row) == duration_days
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: (row.get("period_end") or "", _filing_sort_key(row), _rank(row)))


def _prior_comparable_ytd_record(
    rows: list[dict[str, Any]],
    *,
    period_start: str | None,
    duration_days: int,
    before_start: str | None = None,
) -> dict[str, Any] | None:
    if period_start is not None:
        return _ytd_record_for_start(rows, period_start, duration_days)
    candidates = [
        row
        for row in rows
        if row.get("form") in {"10-Q", "10-Q/A"}
        and _duration_days(row) == duration_days
        and row.get("period_start")
        and (before_start is None or row["period_start"] < before_start)
    ]
    if not candidates:
        return None
    latest_start = max(row["period_start"] for row in candidates)
    return max(
        [row for row in candidates if row["period_start"] == latest_start],
        key=lambda row: (_filing_sort_key(row), _rank(row)),
    )


def _fy_only_record(annual: dict[str, Any], *, method: str = "fy_only") -> dict[str, Any]:
    result = dict(annual)
    result["ttm_method"] = method
    result["ttm_components"] = {"fy": annual}
    return result


def _earnings_history_accessions(
    facts: dict[str, Any],
    filings: list[dict[str, Any]] | None,
    accessions: set[str] | None,
    cutoff: Any,
) -> set[str] | None:
    """Supplement truncated submissions.recent with dated companyfacts history.

    A known accession must still pass its conservative acceptance cutoff.
    Only absent accessions may fall back to companyfacts' own filing date.
    This history is used solely for the earnings TTM comparative assembly.
    """
    if accessions is None:
        return None
    known = {
        row.get("accession") or row.get("accessionNumber") or row.get("accn")
        for row in filings or []
    }
    historical = {
        row["accession"]
        for field in EARNINGS_COMPARATIVE_FIELDS
        for row in _records_for_tags(facts, FIELD_TAGS[field], accessions=None, cutoff=cutoff, cik=None)
        if row.get("accession") and row["accession"] not in known and row.get("filed")
    }
    return accessions | historical


def _earnings_ttm_pair(
    rows: list[dict[str, Any]],
    annual_filing: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], str] | None:
    annuals = _annual_period_records(rows)
    current_annual = _annual_record(rows, annual_filing)
    transitions = [row for row in rows if row.get("ttm_method") == "transition_period"]
    accession = (annual_filing or {}).get("accession") or (annual_filing or {}).get("accessionNumber")
    current_end = current_annual.get("period_end") if current_annual else (annual_filing or {}).get("period_end")
    if annual_filing and not annual_filing.get("history_source"):
        current_end = annual_filing.get("reportDate") or current_end
    current_transitions = [row for row in transitions
                           if (not accession or row.get("accession") == accession)
                           and (not current_end or row.get("period_end") == current_end)]
    if current_transitions:
        current = max(current_transitions, key=lambda row: (row.get("period_end") or "", _filing_sort_key(row)))
        prior = [row for row in annuals if _next_day(row.get("period_end")) == current.get("period_start")]
        if prior:
            return current, _fy_only_record(prior[-1]), "transition_period"
    if current_annual is None:
        return None
    prior_transitions = [row for row in transitions if _next_day(row.get("period_end")) == current_annual.get("period_start")]
    if prior_transitions:
        prior = max(prior_transitions, key=lambda row: (_filing_sort_key(row), _duration_days(row)))
        return _fy_only_record(current_annual), prior, "transition_period"
    prior_annuals = [row for row in annuals if row.get("period_end") < current_annual.get("period_start")]
    if not prior_annuals:
        return None
    prior_annual = prior_annuals[-1]

    current_ytd_candidates = [
        row
        for row in rows
        if row.get("form") in {"10-Q", "10-Q/A"}
        and row.get("period_start") == _next_day(current_annual.get("period_end"))
        and _is_preferred_ytd_duration(row)
    ]
    current_ytd = max(
        current_ytd_candidates,
        key=lambda row: (row.get("period_end") or "", _filing_sort_key(row), _rank(row)),
    ) if current_ytd_candidates else None
    if current_ytd is None:
        return _fy_only_record(current_annual), _fy_only_record(prior_annual), "fy_only"

    duration = _duration_days(current_ytd)
    prior_ytd = _ytd_record_for_start(rows, current_annual.get("period_start"), duration)
    prior_prior_start = prior_annual.get("period_start")
    prior_prior_ytd = _prior_comparable_ytd_record(
        rows,
        period_start=prior_prior_start,
        duration_days=duration,
        before_start=prior_ytd.get("period_start") if prior_ytd else None,
    )
    if prior_ytd is None or prior_prior_ytd is None:
        return _fy_only_record(current_annual), _fy_only_record(prior_annual), "fy_only"

    current = _ttm_record(current_annual, current_ytd, prior_ytd, method="fy_plus_ytd")
    prior = _ttm_record(prior_annual, prior_ytd, prior_prior_ytd, method="fy_plus_ytd")
    if current is None or prior is None:
        return _fy_only_record(current_annual), _fy_only_record(prior_annual), "fy_only"
    return current, prior, "fy_plus_ytd"


def _aggregate_one_off_comparative_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_tag_period: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = (row.get("tag"), row.get("period_start"), row.get("period_end"))
        current = by_tag_period.get(key)
        if current is None or (_filing_sort_key(row), _rank(row)) > (_filing_sort_key(current), _rank(current)):
            by_tag_period[key] = row
    by_period: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in by_tag_period.values():
        by_period.setdefault((row.get("period_start"), row.get("period_end")), []).append(row)
    result: list[dict[str, Any]] = []
    for period_rows in by_period.values():
        try:
            total = sum((Decimal(row["value"]) for row in period_rows), Decimal(0))
        except (KeyError, InvalidOperation):
            continue
        latest = max(period_rows, key=lambda row: (_filing_sort_key(row), _rank(row)))
        aggregate = dict(latest)
        aggregate["value"] = str(total)
        aggregate["tag"] = "+".join(str(row.get("tag")) for row in period_rows if row.get("tag"))
        aggregate["tags_used"] = [row.get("tag") for row in period_rows if row.get("tag")]
        # Preserve signed source items: E5 must exclude losses per tag,
        # rather than clipping a net total that may offset unrelated gains.
        aggregate["components"] = period_rows
        result.append(aggregate)
    return result


def _apply_earnings_quality_ttm(
    extracted: dict[str, Any],
    *,
    filings: list[dict[str, Any]] | None,
    asof: Any,
) -> None:
    pairs: dict[str, dict[str, Any]] = {}
    methods: dict[str, str] = {}
    annual_filings = [row for row in filings or [] if row.get("form") in {"10-K", "10-K/A"}]
    annual_rows = [row for field in EARNINGS_FLOW_FIELDS for row in extracted.get(f"{field}_comparative") or []
                   if row.get("form") in {"10-K", "10-K/A"}]
    annual_filing = _latest_financial_filing(annual_filings, asof, annual_rows)
    for field in EARNINGS_FLOW_FIELDS:
        rows = extracted.get(f"{field}_comparative") or []
        if field == "one_off_items":
            rows = _aggregate_one_off_comparative_rows(rows)
        pair = _earnings_ttm_pair([row for row in rows if isinstance(row, dict)], annual_filing)
        if pair is None:
            continue
        current, prior, method = pair
        pairs[field] = {"current": current, "prior": prior, "ttm_method": method}
        methods[field] = method
        # CFO in the shareholder fields must retain the C3 group's period.
        # Earnings-quality consumes its independently assembled pair above.
        if field == "net_income":
            extracted[field] = [current]
    extracted["earnings_quality_comparatives"] = pairs or None
    method_values = set(methods.values())
    extracted["earnings_quality_ttm_methods"] = methods or None
    extracted["earnings_quality_ttm_method"] = (
        "transition_period" if "transition_period" in method_values
        else "fy_plus_ytd" if "fy_plus_ytd" in method_values else "fy_only" if method_values else None
    )


def _apply_ttm(
    extracted: dict[str, Any],
    *,
    filings: list[dict[str, Any]] | None,
    asof: Any,
) -> None:
    all_records = [row for field in FLOW_FIELDS for row in (extracted.get(field) or [])]
    latest = _latest_financial_filing(filings, asof, all_records)
    if latest is None:
        return
    annual_filing = latest if latest.get("form") in {"10-K", "10-K/A"} else None
    if annual_filing is None:
        annual_candidates = [
            row for row in (filings or [])
            if row.get("form") in {"10-K", "10-K/A"}
        ]
        if annual_candidates:
            pit_annual = filter_filings(annual_candidates, asof) if asof is not None else annual_candidates
            annual_filing = max(pit_annual, key=_filing_sort_key) if pit_annual else None
    if annual_filing is None:
        # Companyfacts-only callers do not have submission metadata.  Recover
        # the latest annual filing identity from the already PIT-filtered
        # duration facts so a latest 10-Q can still be converted to TTM.
        annual_candidates = [
            row for row in all_records
            if row.get("form") in {"10-K", "10-K/A"} and row.get("accession")
        ]
        if annual_candidates:
            annual_filing = max(
                annual_candidates,
                key=lambda row: (_filing_sort_key(row), row.get("period_end") or ""),
            )

    annual_rows = {
        field: _annual_record(extracted.get(field) or [], annual_filing)
        for field in FLOW_FIELDS
    }
    q_rows = {
        field: _quarter_ytd_records(
            extracted.get(field) or [],
            latest,
            annual=annual_rows[field],
        )
        for field in FLOW_FIELDS
    }
    q_complete = latest.get("form") in {"10-Q", "10-Q/A"} and all(
        annual_rows[field] is not None and q_rows[field] is not None
        for field in ("repurchase", "cfo", "capex")
    )
    method = "fy_plus_ytd" if q_complete else "fy_only"
    extracted["ttm_method"] = method
    for field in FLOW_FIELDS:
        annual = annual_rows[field]
        if annual is None:
            extracted[field] = None
            continue
        current, prior = q_rows[field] if q_rows[field] is not None else (None, None)
        field_method = method
        if field == "common_issuance" and q_rows[field] is None:
            field_method = "fy_only"
        row = _ttm_record(annual, current, prior, method=field_method)
        extracted[field] = [row] if row is not None else None


def _apply_ttm_subset(
    extracted: dict[str, Any],
    fields: tuple[str, ...],
    *,
    filings: list[dict[str, Any]] | None,
    asof: Any,
) -> None:
    """Apply the existing FY+YTD TTM rule to an independent field group.

    Earnings-quality fields must not make shareholder-return TTM selection
    depend on optional one-off or working-capital tags.  Each field therefore
    falls back independently to the same filing's FY value.
    """
    all_records = [row for field in fields for row in (extracted.get(field) or [])]
    latest = _latest_financial_filing(filings, asof, all_records)
    if latest is None:
        return
    annual_filing = latest if latest.get("form") in {"10-K", "10-K/A"} else None
    if annual_filing is None:
        annual_candidates = [row for row in (filings or []) if row.get("form") in {"10-K", "10-K/A"}]
        if annual_candidates:
            pit_annual = filter_filings(annual_candidates, asof) if asof is not None else annual_candidates
            annual_filing = max(pit_annual, key=_filing_sort_key) if pit_annual else None
    if annual_filing is None:
        annual_candidates = [
            row for row in all_records
            if row.get("form") in {"10-K", "10-K/A"} and row.get("accession")
        ]
        if annual_candidates:
            annual_filing = max(annual_candidates, key=lambda row: (_filing_sort_key(row), row.get("period_end") or ""))
    for field in fields:
        rows = extracted.get(field) or []
        annual = _annual_record(rows, annual_filing)
        q_rows = _quarter_ytd_records(rows, latest, annual=annual)
        method = "fy_plus_ytd" if latest.get("form") in {"10-Q", "10-Q/A"} and annual is not None and q_rows is not None else "fy_only"
        if annual is None:
            extracted[field] = None
            continue
        current, prior = q_rows if q_rows is not None else (None, None)
        row = _ttm_record(annual, current, prior, method=method)
        extracted[field] = [row] if row is not None else None


def _debt_definition(tags: list[str]) -> str:
    if tags == ["LongTermDebtCurrent", "LongTermDebtNoncurrent", "CommercialPaper"]:
        return "CommercialPaper + LongTermDebtCurrent + LongTermDebtNoncurrent; cash and assets as reported"
    if tags and tags[0] == "DebtInstrumentCarryingAmount":
        optional = " + " + " + ".join(tags[1:]) if len(tags) > 1 else ""
        return f"DebtInstrumentCarryingAmount (원금 기준){optional}; cash and assets as reported"
    return " + ".join(tags) + "; cash and assets as reported"


def _latest_annual_form(
    filings: list[dict[str, Any]] | None,
    asof: Any,
) -> str | None:
    if not filings or asof is None:
        return None
    candidates = [row for row in filter_filings(filings, asof) if row.get("form") in ANNUAL_REPORT_FORMS]
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


DEBT_TAGS = (
    "LongTermDebt",
    "LongTermDebtCurrent",
    "LongTermDebtNoncurrent",
    "LongTermDebtAndCapitalLeaseObligations",
    "LongTermDebtAndCapitalLeaseObligationsCurrent",
    "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
    "CommercialPaper",
    "ShortTermBorrowings",
    "DebtCurrent",
    "DebtInstrumentCarryingAmount",
)


def _aggregate_debt(
    facts: dict[str, Any],
    *,
    accessions: set[str] | None,
    cutoff: Any,
    cik: str | int | None,
) -> list[dict[str, Any]]:
    records = _records_for_tags(facts, DEBT_TAGS, accessions=accessions, cutoff=cutoff, cik=cik)
    by_accession: dict[Any, dict[Any, dict[str, dict[str, Any]]]] = {}
    for record in records:
        if record.get("unit") != "USD" or not record.get("period_end"):
            continue
        period_rows = by_accession.setdefault(record.get("accession"), {}).setdefault(record["period_end"], {})
        current = period_rows.get(record.get("tag"))
        if current is None or (record.get("filed") or "") > (current.get("filed") or ""):
            period_rows[record.get("tag")] = record

    def complete(period_rows: dict[Any, dict[str, dict[str, Any]]], periods: list[Any], tags: tuple[str, ...]) -> bool:
        return all(all(tag in period_rows[period] for tag in tags) for period in periods)

    def choose_tags(period_rows: dict[Any, dict[str, dict[str, Any]]], periods: list[Any]) -> list[str] | None:
        optional = tuple(
            tag for tag in ("CommercialPaper", "ShortTermBorrowings")
            if complete(period_rows, periods, (tag,))
        )
        if complete(period_rows, periods, ("LongTermDebt",)):
            if complete(period_rows, periods, ("DebtCurrent",)):
                return ["LongTermDebt", "DebtCurrent"]
            return ["LongTermDebt", *optional]
        if complete(period_rows, periods, ("LongTermDebtCurrent", "LongTermDebtNoncurrent")):
            if complete(period_rows, periods, ("DebtCurrent",)):
                return ["LongTermDebtNoncurrent", "DebtCurrent"]
            return ["LongTermDebtCurrent", "LongTermDebtNoncurrent", *optional]
        if complete(period_rows, periods, ("LongTermDebtAndCapitalLeaseObligations", "LongTermDebtAndCapitalLeaseObligationsCurrent")):
            if complete(period_rows, periods, ("DebtCurrent",)):
                return ["LongTermDebtAndCapitalLeaseObligations", "DebtCurrent"]
            return [
                "LongTermDebtAndCapitalLeaseObligations",
                "LongTermDebtAndCapitalLeaseObligationsCurrent",
                *optional,
            ]
        if complete(period_rows, periods, ("DebtInstrumentCarryingAmount",)):
            return ["DebtInstrumentCarryingAmount", *optional]
        # This total already includes current maturities. Use it only when
        # the existing definitions cannot cover both comparative columns.
        total = "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"
        if complete(period_rows, periods, (total,)):
            return [total, *optional]
        return None

    result: list[dict[str, Any]] = []
    for period_rows in by_accession.values():
        periods = sorted(period_rows, reverse=True)[:2]
        tags = choose_tags(period_rows, periods)
        if tags is None:
            continue
        for rows in period_rows.values():
            if not all(tag in rows for tag in tags):
                continue
            try:
                total = sum((Decimal(rows[tag]["value"]) for tag in tags), Decimal(0))
            except (KeyError, InvalidOperation):
                continue
            first = rows[tags[0]]
            result.append(
                {
                    "value": str(total),
                    "unit": "USD",
                    "period_start": first.get("period_start"),
                    "period_end": first.get("period_end"),
                    "form": first.get("form"),
                    "accession": first.get("accession"),
                    "tag": "+".join(tags),
                    "definition": _debt_definition(tags),
                    "filed": first.get("filed"),
                    "components": {tag: rows[tag] for tag in tags},
                    **({"cik": str(cik)} if cik is not None else {}),
                }
            )
    return result


def _preferred_balance_records(
    records: list[dict[str, Any]],
    tags: tuple[str, ...],
) -> list[dict[str, Any]]:
    by_context: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for record in records:
        key = (
            record.get("unit"),
            record.get("period_start"),
            record.get("period_end"),
            record.get("accession"),
        )
        by_context.setdefault(key, []).append(record)
    selected: list[dict[str, Any]] = []
    for rows in by_context.values():
        for tag in tags:
            matching = [row for row in rows if row.get("tag") == tag]
            if matching:
                selected.append(max(matching, key=lambda row: (row.get("filed") or "", row.get("accession") or "")))
                break
    return selected


def _annotate_cash_presentation(extracted: dict, filings: list[dict] | None, facts: dict, *,
                                asof: Any, client: SecClient,
                                before_fetch: Callable[[], None] | None) -> None:
    """Inspect only the source filings feeding current FY/TTM cash predicates.

    Missing components keep their provenance in presentation_changes; no amount
    is imputed for them, and an old FY value cannot masquerade as current TTM.
    """
    visible = filter_filings(filings, asof) if filings is not None and asof is not None else filings or []
    financial = [r for r in visible if r.get('form') in {'10-K', '10-K/A', '10-Q', '10-Q/A'}]
    annual = [r for r in financial if r.get('form') in {'10-K', '10-K/A'}]
    selected = {r.get('accession') or r.get('accessionNumber'): r for r in
                ([max(financial, key=_filing_sort_key)] if financial else []) +
                ([max(annual, key=_filing_sort_key)] if annual else [])}
    # Include the actual original source when a governance-only amendment has
    # no numerical statements of its own.
    for filing in list(selected.values()):
        if filing.get('form') != '10-K/A':
            continue
        original = next((_annual_record(extracted.get(field) or [], filing)
                         for field in ('repurchase', 'cfo', 'capex')
                         if _annual_record(extracted.get(field) or [], filing)), None)
        if original and original.get('amendment_accession'):
            source_filing = next((f for f in visible if
                (f.get('accession') or f.get('accessionNumber')) == original['accession']), None)
            if source_filing:
                selected[original['accession']] = source_filing
    equivalents = _usd_records(_records_for_tags(facts, ('PaymentsForRepurchaseOfEquity',),
        accessions=set(selected), cutoff=asof_cutoff(asof) if asof is not None else None, cik=None))
    for accession, filing in selected.items():
        sources = [r for field in ('cfo', 'capex', 'repurchase', 'common_issuance')
                   for r in extracted.get(field) or [] if r.get('accession') == accession]
        # Keep FY or YTD, not shorter quarter facts sharing the same end date.
        by_end = {}
        for source in sources:
            end = source.get('period_end')
            if end not in by_end or _duration_days(source) > _duration_days(by_end[end]):
                by_end[end] = source
        sources = sorted(by_end.values(), key=lambda r: r.get('period_end') or '', reverse=True)[:2]
        document = filing.get('primaryDocument')
        cik = next((r.get('cik') for r in sources if r.get('cik')), None)
        if not (accession and document and cik):
            continue
        try:
            if before_fetch:
                before_fetch()
            body = client.primary_document(cik, accession, document)
        except (OfflineDataUnavailable, SecNetworkError, OSError):
            continue
        for source in sources:
            evidence = cash_flow_presentation(body, source)
            info = evidence.get('repurchase') or {}
            label = info.get('presentation_line', '')
            program = (info.get('presentation') == 'gross'
                       and 'PaymentsForRepurchaseOfEquity' in info.get('presentation_tags', [])
                       and re.search(r'program|common stock', label, re.I)
                       and not re.search(r'tax|withhold|preferred|noncontrolling|subsidiar', label, re.I))
            if program and not any(r.get('accession') == accession and _same_period(r, source)
                                   for r in extracted.get('repurchase') or []):
                candidates = [r for r in equivalents if r.get('accession') == accession and _same_period(r, source)]
                for candidate in candidates:
                    equivalent = {**candidate, **info, 'cik':source.get('cik'), 'repurchase_scope':'program',
                                  'equivalent_note':'equity cash tag confirmed as program repurchases in same-period cash-flow row'}
                    extracted['repurchase'] = [*(extracted.get('repurchase') or []), equivalent]
            for field in ('repurchase', 'common_issuance'):
                matches = [r for r in extracted.get(field) or []
                           if r.get('accession') == accession and _same_period(r, source)]
                info = evidence.get(field)
                if not matches and evidence.get('aggregated'):
                    info = info if info and info.get('presentation_change') else evidence['aggregated']
                if info is None:
                    continue
                for match in matches:
                    match.update(info)
                if not matches and info.get('presentation_change'):
                    missing = {**source, **info, 'field':field, 'primary_document':document}
                    missing.pop('value', None)  # not a numeric component
                    extracted.setdefault('presentation_changes', {}).setdefault(field, []).append(missing)


def _infer_absent_issuance(
    extracted: dict[str, Any], facts: dict[str, Any], filings: list[dict[str, Any]] | None,
    *, asof: Any, client: SecClient, before_fetch: Callable[[], None] | None,
) -> None:
    # Reuse TTM selection so only the FY/YTD source contexts actually needed
    # by the current repurchase are inspected, rather than historical filings.
    projection = {field: extracted.get(field) for field in FLOW_FIELDS}
    _apply_ttm(projection, filings=filings, asof=asof)
    documents = {row.get("accession") or row.get("accessionNumber"): row.get("primaryDocument")
                 for row in filings or []}
    issuance_facts = _records_for_tags(facts, ISSUANCE_TAGS, accessions=None, cutoff=None, cik=None)
    loaded: dict[tuple[str, str], bytes | None] = {}
    for repurchase in projection.get("repurchase") or []:
        for source in (repurchase.get("ttm_components") or {"fy": repurchase}).values():
            accession, cik = source.get("accession"), source.get("cik")
            document = documents.get(accession)
            if not (accession and cik and document):
                continue
            if any(row.get("accession") == accession and _same_period(row, source) for row in issuance_facts):
                continue
            key = (cik, accession)
            if key not in loaded:
                try:
                    if before_fetch:
                        before_fetch()
                    # The advisory xbrl_html check uses this same client/cache.
                    loaded[key] = client.primary_document(cik, accession, document)
                except (OfflineDataUnavailable, SecNetworkError, OSError):
                    loaded[key] = None
            body = loaded[key]
            if body is not None and common_issuance_absent(body, source):
                inferred = {**source, "value": "0", "tag": None, "tags_used": [],
                            "primary_document": document,
                            "issuance_scope": "absent_in_cash_flow_statement",
                            "issuance_note": ABSENT_ISSUANCE_NOTE}
                inferred.pop("precision_note", None)
                extracted["common_issuance"] = [*(extracted.get("common_issuance") or []), inferred]


def extract_fields(
    companyfacts: dict[str, Any],
    filings: Iterable[dict[str, Any]] | None = None,
    *,
    asof: str | Any | None = None,
    cik: str | int | None = None,
    sec_client: SecClient | None = None,
    before_fetch: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Extract fields; each non-missing field is a list of fully attributed values."""
    facts = _fact_root(companyfacts)
    filing_rows = list(filings) if filings is not None else None
    accessions: set[str] | None = None
    cutoff = None
    if asof is not None:
        cutoff = asof_cutoff(asof)
    if filing_rows is not None:
        if asof is None:
            raise ValueError("filings require an as-of cutoff")
        filing_rows = filings_with_fact_history(filing_rows, companyfacts, asof)
        accessions = allowed_accessions(filing_rows, asof)
    earnings_accessions = _earnings_history_accessions(facts, filing_rows, accessions, cutoff)

    extracted: dict[str, Any] = {
        "latest_annual_form": _latest_annual_form(filing_rows, asof),
    }
    for field, tags in FIELD_TAGS.items():
        if field == "common_issuance":
            repurchase_rows = extracted.get("repurchase") or []
            values, unconfirmed = _select_issuance_with_metadata(
                facts,
                accessions=accessions,
                cutoff=cutoff,
                cik=cik,
                dedupe=False,
                periods=_issuance_periods_for_ttm(
                    repurchase_rows,
                    filings=filing_rows,
                    asof=asof,
                ),
            )
            if unconfirmed:
                extracted["issuance_unconfirmed"] = unconfirmed
        elif field == "shares":
            records = _records_for_tags(facts, tags, accessions=accessions, cutoff=cutoff, cik=cik)
            if not records:
                records = _derived_share_records(
                    facts,
                    accessions=accessions,
                    cutoff=cutoff,
                    cik=cik,
                )
            values = records
        elif field == "public_float":
            records = _records_for_tags(
                _dei_fact_root(companyfacts),
                tags,
                accessions=accessions,
                cutoff=cutoff,
                cik=cik,
            )
            records = _usd_records(records)
            values = [
                row for row in records if row.get("form") in {"10-K", "10-K/A"}
            ]
        elif field in {"cfo", "capex"}:
            values = _select_preferred_monetary_records(
                facts,
                tags,
                accessions=accessions,
                cutoff=cutoff,
                cik=cik,
                dedupe=False,
            )
            if field == "capex":
                extracted["capex_comparative"] = _duration_comparative_records(values) or None
        elif field in PREMISE_FLOW_FIELDS:
            values = _select_preferred_monetary_records(
                facts, tags, accessions=accessions, cutoff=cutoff, cik=cik, dedupe=False,
            )
            extracted[f"{field}_comparative"] = _duration_comparative_records(values) or None
        elif field == "net_income":
            values = _select_preferred_monetary_records(
                facts,
                tags,
                accessions=accessions,
                cutoff=cutoff,
                cik=cik,
            )
        elif field in {"accounts_receivable_change", "inventory_change", "accounts_payable_change"}:
            values = _select_preferred_monetary_records(
                facts,
                tags,
                accessions=accessions,
                cutoff=cutoff,
                cik=cik,
            )
        elif field == "one_off_items":
            values = _usd_records(_records_for_tags(
                facts,
                tags,
                accessions=accessions,
                cutoff=cutoff,
                cik=cik,
            ))
        else:
            records = _records_for_tags(facts, tags, accessions=accessions, cutoff=cutoff, cik=cik)
            if field in MONETARY_FIELDS:
                records = _usd_records(records)
            if field == "repurchase":
                records = _refine_repurchase_precision(
                    records, facts, accessions=accessions, cutoff=cutoff, cik=cik,
                )
            if field in {"cash", "assets"}:
                # Keep the historical single-period field semantics while
                # retaining all filing cohorts for C4's YoY comparison.
                if field == "cash":
                    records = _preferred_balance_records(records, tags)
                extracted[f"{field}_comparative"] = records or None
                values = _dedupe(records)
            else:
                # Shares are only consumed by the YoY claim, so comparative
                # columns remain available until it selects a filing cohort.
                # TTM selects a filing first, then its FY/YTD comparative
                # columns. Period-only dedupe would discard those columns
                # when the prior YTD was originally disclosed a year earlier.
                values = records if field in COMPARATIVE_FIELDS or field in {"repurchase", "tax_withholding_acquisitions"} else _dedupe(records)
        extracted[field] = values or None
    for field in EARNINGS_COMPARATIVE_FIELDS:
        raw = extracted.get(field)
        extracted[f"{field}_comparative"] = list(raw) if isinstance(raw, list) else None
    # Earnings TTM needs historical FY/YTD periods often absent from recent
    # submissions. Preserve their own filing dates and exact-period provenance.
    for field in EARNINGS_COMPARATIVE_FIELDS:
        tags = FIELD_TAGS[field]
        if field == "one_off_items":
            raw = _usd_records(_records_for_tags(
                facts,
                tags,
                accessions=earnings_accessions,
                cutoff=cutoff,
                cik=cik,
            ))
        else:
            raw = _select_preferred_monetary_records(
                facts,
                tags,
                accessions=earnings_accessions,
                cutoff=cutoff,
                cik=cik,
                dedupe=False,
            )
        extracted[f"{field}_comparative"] = _duration_comparative_records(raw) or None
    if sec_client is not None:
        from .share_counts import enrich_shares
        extracted['shares'] = enrich_shares(extracted.get('shares') or [],
            filter_filings(filing_rows, asof) if filing_rows is not None and asof is not None else filing_rows,
            sec_client, cik, before_fetch=before_fetch) or None
        _annotate_cash_presentation(extracted, filing_rows, facts, asof=asof,
                                   client=sec_client, before_fetch=before_fetch)
        _infer_absent_issuance(extracted, facts, filing_rows, asof=asof,
                               client=sec_client, before_fetch=before_fetch)
    # WATCH keeps independent PIT-filtered history. Legacy fields below still
    # use exactly their existing latest-TTM selection and fallback rules.
    from .commitment_periods import fiscal_cohorts
    ledger_fields = {}
    for field in ('repurchase', 'dividends', 'revenue', 'operating_income', 'capex', 'cfo', 'assets'):
        ledger_fields[field] = _select_preferred_monetary_records(
            facts, FIELD_TAGS[field], accessions=accessions, cutoff=cutoff, cik=cik, dedupe=False,
        )
    ledger_fields['repurchase'] = list(extracted.get('repurchase') or [])
    ledger_fields['interest_bearing_debt'] = _aggregate_debt(
        facts, accessions=accessions, cutoff=cutoff, cik=cik,
    )
    extracted['commitment_ledger'] = {
        'fields': ledger_fields, 'periods': fiscal_cohorts(
            ledger_fields, filter_filings(filing_rows, asof) if filing_rows is not None else None),
    }
    # Excluded employee-tax acquisitions are independently assembled and never
    # influence the program-repurchase TTM period selection.
    _apply_ttm_subset(extracted, ('tax_withholding_acquisitions',), filings=filing_rows, asof=asof)
    _apply_ttm(extracted, filings=filing_rows, asof=asof)
    _apply_ttm_subset(extracted, EARNINGS_TTM_FIELDS, filings=filing_rows, asof=asof)
    _apply_earnings_quality_ttm(extracted, filings=filing_rows, asof=asof)
    debt = _aggregate_debt(facts, accessions=accessions, cutoff=cutoff, cik=cik)
    extracted["interest_bearing_debt_comparative"] = debt or None
    extracted["interest_bearing_debt"] = _dedupe(debt) or None
    # Independent optional fields must never change the existing C1-C4/E1-E5
    # period selection. Retain same-filing FY columns for literal YoY checks.
    _apply_ttm_subset(extracted, PREMISE_FLOW_FIELDS, filings=filing_rows, asof=asof)
    annual_filings = [row for row in filing_rows or [] if row.get("form") in {"10-K", "10-K/A"}]
    annual_records = [row for field in (*PREMISE_FLOW_FIELDS, "capex")
                      for row in extracted.get(f"{field}_comparative") or []
                      if row.get("form") in {"10-K", "10-K/A"}]
    latest_annual = _latest_financial_filing(annual_filings or None, asof, annual_records)
    extracted["premise_latest_annual_accession"] = (latest_annual or {}).get("accession")
    extracted["premise_latest_annual_period_end"] = (
        latest_annual.get("reportDate") or latest_annual.get("period_end")
        if latest_annual and not latest_annual.get("history_source") else None
    )
    latest_share_filing = _latest_financial_filing(filing_rows, asof, extracted.get('shares') or [])
    if latest_share_filing and str(latest_share_filing.get('form', '')).endswith('/A'):
        amendment_accession = latest_share_filing.get('accession') or latest_share_filing.get('accessionNumber')
        original_rows = [r for r in extracted.get('shares') or []
                         if r.get('amendment_accession') == amendment_accession]
        if original_rows:
            source = max(original_rows, key=lambda r:r.get('period_end') or '')
            latest_share_filing = {**latest_share_filing, 'accession':source['accession']}
    if latest_share_filing:
        extracted['share_latest_accession'] = latest_share_filing.get('accession') or latest_share_filing.get('accessionNumber')
        extracted['share_latest_period_end'] = None if latest_share_filing.get('history_source') else (
            latest_share_filing.get('reportDate') or latest_share_filing.get('period_end'))
        current_end = extracted['share_latest_period_end']
        annual_ends = {f.get('reportDate') for f in filing_rows or []
                       if f.get('form') in {'10-K', '10-K/A'} and not f.get('history_source')}
        annual_ends.update(r.get('period_end') for field in ('cfo', 'repurchase', 'revenue')
                           for r in ledger_fields.get(field) or [] if _is_annual_duration(r))
        eligible = [end for end in annual_ends if end and current_end and end < current_end]
        if eligible:
            # Equity notes contain interim rollforwards too. The comparison is
            # the prior fiscal-end balance, never the nearest quarter in a note.
            extracted['share_prior_period_end'] = max(eligible)
    return extracted

