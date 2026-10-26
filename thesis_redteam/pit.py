"""Point-in-time filing acceptance helpers."""

from __future__ import annotations

from datetime import date, datetime, timezone
import re
from typing import Any, Iterable


UTC = timezone.utc
SGML_ACCEPTANCE_RE = re.compile(r"<ACCEPTANCE-DATETIME>\s*(\d{14})", re.IGNORECASE)


def parse_timestamp(value: Any) -> datetime | None:
    """Parse SEC JSON, SGML, ISO, or date-only timestamps as UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    text = str(value).strip()
    if len(text) == 14 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    if len(text) == 10:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
    normalized = text.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def asof_cutoff(asof: str | date | datetime) -> datetime:
    """Return the exclusive PIT cutoff.

    A date-only as-of means the beginning of that UTC date, so acceptance on
    the named date is deliberately excluded by the strict-before rule.
    """
    parsed = parse_timestamp(asof)
    if parsed is None:
        raise ValueError("as-of date is required")
    return parsed.astimezone(UTC)


def parse_sgml_acceptance(text: str) -> str | None:
    """Read the SGML header's ``ACCEPTANCE-DATETIME`` when present."""
    match = SGML_ACCEPTANCE_RE.search(text)
    if not match:
        return None
    return parse_timestamp(match.group(1)).isoformat()


def _first(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        if mapping.get(name) not in (None, ""):
            return mapping[name]
    return None


def normalize_filing(filing: dict[str, Any]) -> dict[str, Any]:
    """Normalize a submission row while preserving both acceptance clocks."""
    json_time = _first(filing, "acceptanceDateTime", "acceptance_datetime_json", "acceptance_json")
    sgml_time = _first(filing, "acceptance_datetime", "acceptanceDateTimeSgml", "sgml_acceptance", "acceptance_sgml")
    if sgml_time is None:
        sgml_time = parse_sgml_acceptance(str(_first(filing, "sgml_text", "submission_text") or ""))
    filing_date = _first(filing, "filingDate", "filed", "filed_date")
    json_dt = parse_timestamp(json_time)
    sgml_dt = parse_timestamp(sgml_time)
    candidates = [item for item in (json_dt, sgml_dt) if item is not None]
    conservative = max(candidates) if candidates else parse_timestamp(filing_date)
    accession = _first(filing, "accessionNumber", "accession", "accn")
    normalized = dict(filing)
    normalized.update(
        {
            "accession": accession,
            "acceptance_json": json_dt.isoformat() if json_dt else None,
            "acceptance_sgml": sgml_dt.isoformat() if sgml_dt else None,
            "conservative_acceptance": conservative.isoformat() if conservative else None,
            "filing_date": filing_date,
        }
    )
    return normalized


def filter_filings(filings: Iterable[dict[str, Any]], asof: str | date | datetime) -> list[dict[str, Any]]:
    """Keep only filings accepted strictly before ``asof``."""
    cutoff = asof_cutoff(asof)
    result: list[dict[str, Any]] = []
    for filing in filings:
        normalized = normalize_filing(filing)
        accepted = parse_timestamp(normalized.get("conservative_acceptance"))
        if accepted is not None and accepted < cutoff:
            result.append(normalized)
    return result


def allowed_accessions(filings: Iterable[dict[str, Any]], asof: str | date | datetime) -> set[str]:
    return {
        item["accession"]
        for item in filter_filings(filings, asof)
        if item.get("accession")
    }


def filings_with_fact_history(
    filings: Iterable[dict[str, Any]],
    companyfacts: dict[str, Any],
    asof: str | date | datetime,
) -> list[dict[str, Any]]:
    """Recover filings missing from truncated submissions.recent, without I/O.

    Only absent accessions may use companyfacts' dated filing evidence. Known
    submissions retain their authoritative form and both acceptance clocks,
    including rows excluded by the cutoff. Never infer a date from the period.
    """
    rows = list(filings)
    known = {row.get("accession") or row.get("accessionNumber") or row.get("accn") for row in rows}
    cutoff = asof_cutoff(asof)
    history: dict[str, dict[str, Any]] = {}
    for namespace in companyfacts.get("facts", companyfacts).values():
        for node in namespace.values():
            for facts in node.get("units", {}).values():
                for fact in facts:
                    accession = fact.get("accn") or fact.get("accession")
                    filed = parse_timestamp(fact.get("filed"))
                    form = fact.get("form")
                    if not accession or accession in known or filed is None:
                        continue
                    if form not in {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A"}:
                        continue
                    candidate = history.setdefault(accession, {
                        "accession": accession, "form": form,
                        "filingDate": fact["filed"], "reportDate": fact.get("end"),
                        "history_source": "companyfacts.filed",
                    })
                    # If source rows disagree, use the later disclosure date.
                    candidate["filingDate"] = max(candidate["filingDate"], fact["filed"])
                    candidate["reportDate"] = max(candidate.get("reportDate") or "", fact.get("end") or "")
    return rows + [row for row in history.values() if parse_timestamp(row["filingDate"]) < cutoff]

