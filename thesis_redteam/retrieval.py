"""Dependency-free retrieval for the 81-item counterexample library."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable


COUNTEREXAMPLES_PATH = Path(__file__).resolve().parents[1] / "data" / "counterexamples.json"
_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+", re.UNICODE)
_SUBJECT_ID_RE = re.compile(r"^(?:cash|trap)_([A-Za-z][A-Za-z0-9]*)(?:_|$)", re.IGNORECASE)


def _tokens(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, dict):
        value = " ".join(str(item) for item in value.values())
    elif isinstance(value, (list, tuple, set)):
        value = " ".join(str(item) for item in value)
    return {match.group(0).lower() for match in _TOKEN_RE.finditer(str(value))}


def load_counterexamples(path: str | Path | None = None) -> list[dict[str, Any]]:
    source = Path(path) if path else COUNTEREXAMPLES_PATH
    rows = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("counterexamples.json must contain a list")
    return [row for row in rows if isinstance(row, dict)]


def _normalise_ticker(value: Any) -> str:
    return str(value or "").strip().upper()


def _normalise_cik(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.startswith("CIK"):
        text = text[3:]
    try:
        return str(int(text))
    except (TypeError, ValueError):
        return text


def _as_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def subject_tickers(row: dict[str, Any]) -> list[str]:
    """Return the explicit subject tickers, with a safe legacy fallback."""

    values = _as_values(row.get("subject_tickers"))
    values.extend(_as_values(row.get("ticker")))
    values.extend(_as_values(row.get("tickers")))
    if not values:
        match = _SUBJECT_ID_RE.match(str(row.get("id", "")))
        values = [match.group(1)] if match else []
    return sorted({_normalise_ticker(value) for value in _as_values(values) if _normalise_ticker(value)})


def _subject_ciks(row: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("subject_ciks", "ciks", "cik"):
        values.extend(_as_values(row.get(key)))
    return sorted({_normalise_cik(value) for value in values if _normalise_cik(value)})


def _same_subject(row: dict[str, Any], *, subject_ticker: str | None, subject_cik: str | int | None) -> bool:
    ticker = _normalise_ticker(subject_ticker)
    cik = _normalise_cik(subject_cik)
    return bool(
        (ticker and ticker in subject_tickers(row))
        or (cik and cik in _subject_ciks(row))
    )


def _score(row: dict[str, Any], query: str, template: str | None) -> float:
    query_tokens = _tokens(query)
    keywords = row.get("keywords") or []
    keyword_tokens = set().union(*(_tokens(item) for item in keywords)) if keywords else set()
    score = float(len(query_tokens & keyword_tokens) * 5)
    category = str(row.get("category", "")).lower()
    category_tokens = _tokens(category.replace("_", " "))
    score += len(query_tokens & category_tokens) * 2
    if template and template in (row.get("applies_to_templates") or []):
        score += 8
    elif template:
        score -= 1
    searchable = " ".join(
        str(row.get(name, "")) for name in ("thesis_pattern_ko", "trap_ko", "check_to_add")
    )
    score += len(query_tokens & _tokens(searchable)) * 0.25

    # These are high-signal mechanisms in the B03 shareholder-return domain.
    query_text = str(query).lower()
    if template == "shareholder_return":
        if any(term in query_text for term in ("자사주", "buyback", "매입", "주주환원", "shareholder")):
            if category in {"dilution_offsets_buyback", "cash_flow_insufficient", "debt_funded_return"}:
                score += 5
        if any(term in query_text for term in ("희석", "발행", "issuance", "common stock", "공모")):
            if category == "dilution_offsets_buyback":
                score += 8
        if any(term in query_text for term in ("부채", "debt", "순부채")) and category == "debt_funded_return":
            score += 8
    if template == "earnings_quality":
        if any(term in query_text for term in ("이익", "순이익", "earnings", "net income", "현금품질", "cash quality")):
            if category in {"metric_substitution", "missing_input_unverifiable", "data_integrity", "duplicate_of_known_factor"}:
                score += 4
        if any(term in query_text for term in ("운전자본", "매출채권", "재고", "매입채무", "working capital")) and "working_capital" in str(row.get("check_to_add", "")):
            score += 8
        if any(term in query_text for term in ("발생액", "accrual")) and "accrual" in str(row.get("check_to_add", "")).lower():
            score += 8
        if any(term in query_text for term in ("일회성", "처분", "one-off", "discontinued")) and category == "metric_substitution":
            score += 3
    return score


def retrieve_counterexamples(
    query: str | dict[str, Any],
    *,
    template: str | None = None,
    k: int = 5,
    path: str | Path | None = None,
    subject_ticker: str | None = None,
    subject_cik: str | int | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return the top ``k`` records using keyword/category/template scoring."""

    rows = load_counterexamples(path)
    query_ticker = subject_ticker
    query_cik = subject_cik
    if isinstance(query, dict):
        query_ticker = query_ticker or query.get("subject_ticker") or query.get("ticker")
        query_cik = query_cik or query.get("subject_cik") or query.get("cik")
    excluded = [
        str(row.get("id"))
        for row in rows
        if row.get("id") and _same_subject(row, subject_ticker=query_ticker, subject_cik=query_cik)
    ]
    if metadata is not None:
        metadata["excluded_same_subject"] = excluded
    rows = [row for row in rows if str(row.get("id")) not in set(excluded)]
    query_text = query if isinstance(query, str) else json.dumps(query, ensure_ascii=False)
    scored = [(_score(row, query_text, template), index, row) for index, row in enumerate(rows)]
    scored.sort(key=lambda item: (-item[0], item[1]))
    result: list[dict[str, Any]] = []
    for score, _, row in scored[: max(0, k)]:
        item = dict(row)
        item["retrieval_score"] = round(score, 4)
        result.append(item)
    return result


def search_counterexamples(
    thesis: str,
    template: str | None = None,
    *,
    top_k: int = 5,
    path: str | Path | None = None,
    subject_ticker: str | None = None,
    subject_cik: str | int | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Readable alias used by callers that think in terms of search."""

    return retrieve_counterexamples(
        thesis,
        template=template,
        k=top_k,
        path=path,
        subject_ticker=subject_ticker,
        subject_cik=subject_cik,
        metadata=metadata,
    )


retrieve = retrieve_counterexamples

