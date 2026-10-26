"""Point-in-time market prices and market-cap inputs for B03."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


STOOQ_URL = "https://stooq.com/q/d/l/?s={ticker}.us&i=d"
PRICE_NOTE = "가격원: stooq, 분할조정 종가 가능성"


@dataclass(frozen=True)
class PriceQuote:
    ticker: str
    date: str
    close: Decimal
    source: str
    url: str | None = None
    receipt: dict[str, Any] | None = None


class PriceProvider(Protocol):
    def get_price(self, ticker: str, on_or_before: str) -> PriceQuote | None:
        """Return the last available close on or before ``on_or_before``."""


class StooqProvider:
    """Fetch daily Stooq CSV data with a SecClient-style content cache."""

    def __init__(
        self,
        cache_dir: str | Path = "cache/prices",
        receipts_path: str | Path | None = None,
        *,
        opener: Callable[..., Any] | None = None,
        user_agent: str = "Thesis red-team price verification research@example.com",
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.receipts_path = Path(receipts_path) if receipts_path else self.cache_dir / "receipts.jsonl"
        self.receipts_path.parent.mkdir(parents=True, exist_ok=True)
        self._opener = opener or urllib.request.urlopen
        self.user_agent = user_agent
        self.last_receipt: dict[str, Any] | None = None
        self._redaction_secrets: tuple[str, ...] = ()

    @staticmethod
    def url_key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = self.url_key(url)
        return self.cache_dir / f"{key}.body", self.cache_dir / f"{key}.json"

    def _append_receipt(self, receipt: dict[str, Any]) -> None:
        with self.receipts_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n")

    def _store(self, url: str, body: bytes, *, source: str) -> dict[str, Any]:
        fetched_at = datetime.now(timezone.utc).isoformat()
        receipt = {
            "url": url,
            "url_key": self.url_key(url),
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
            "fetched_at": fetched_at,
            "source": source,
        }
        body_path, meta_path = self._paths(url)
        body_path.write_bytes(body)
        meta_path.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        self._append_receipt(receipt)
        return receipt

    def _annotate_receipt(self, url: str, note: str) -> dict[str, Any] | None:
        """Add a parser/availability note without changing the cached body."""
        _, meta_path = self._paths(url)
        try:
            receipt = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        receipt["note"] = note
        meta_path.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        self._append_receipt(receipt)
        self.last_receipt = receipt
        return receipt

    def _error_receipt(self, url: str, error: BaseException, status: Any = None) -> None:
        error_text = str(error)
        for secret in self._redaction_secrets:
            if secret:
                error_text = error_text.replace(secret, "REDACTED")
        receipt = {
            "url": url,
            "url_key": self.url_key(url),
            "status": status,
            "error": error_text,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "network_error",
        }
        self._append_receipt(receipt)
        self.last_receipt = receipt

    def _body_and_receipt(
        self,
        url: str,
        *,
        cache_url: str | None = None,
        source: str = "network",
    ) -> tuple[bytes, dict[str, Any] | None]:
        stored_url = cache_url or url
        body_path, meta_path = self._paths(stored_url)
        if body_path.is_file() and meta_path.is_file():
            try:
                receipt = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                receipt = None
            self.last_receipt = receipt
            return body_path.read_bytes(), receipt

        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            response = self._opener(request, timeout=30)
            try:
                body = response.read()
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()
        except urllib.error.HTTPError as exc:
            self._error_receipt(stored_url, exc, exc.code)
            return b"", None
        except urllib.error.URLError as exc:
            self._error_receipt(stored_url, exc)
            return b"", None
        receipt = self._store(stored_url, body, source=source)
        self.last_receipt = receipt
        return body, receipt

    @staticmethod
    def _is_csv(body: bytes) -> bool:
        try:
            header = body.decode("utf-8-sig").splitlines()[0].strip()
        except (UnicodeDecodeError, IndexError):
            return False
        return header.split(",")[:2] == ["Date", "Open"]

    @staticmethod
    def _parse_rows(body: bytes) -> list[tuple[date, Decimal]]:
        rows: list[tuple[date, Decimal]] = []
        try:
            reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
            for row in reader:
                raw_date = str(row.get("Date") or "").strip()
                raw_close = str(row.get("Close") or "").strip()
                if not raw_date or not raw_close or raw_close.upper() in {"N/D", "NA", "N/A"}:
                    continue
                try:
                    trading_date = date.fromisoformat(raw_date)
                    close = Decimal(raw_close)
                except (ValueError, InvalidOperation):
                    continue
                if close.is_finite() and close > 0:
                    rows.append((trading_date, close))
        except (UnicodeDecodeError, csv.Error):
            return []
        return rows

    def get_price(self, ticker: str, on_or_before: str) -> PriceQuote | None:
        target = date.fromisoformat(str(on_or_before)[:10])
        normalized = str(ticker).strip().lower()
        if not normalized:
            return None
        url = STOOQ_URL.format(ticker=normalized)
        body, receipt = self._body_and_receipt(url)
        if not self._is_csv(body):
            self._annotate_receipt(url, "가격원 응답이 CSV가 아님(브라우저 검증)")
            return None
        candidates = [(trading_date, close) for trading_date, close in self._parse_rows(body) if trading_date <= target]
        if not candidates:
            return None
        trading_date, close = max(candidates, key=lambda item: item[0])
        return PriceQuote(
            ticker=str(ticker).upper(),
            date=trading_date.isoformat(),
            close=close,
            source="stooq",
            url=url,
            receipt=receipt,
        )

    price_on_or_before = get_price


TIINGO_URL = "https://api.tiingo.com/tiingo/daily/{ticker}/prices?startDate={start}&endDate={end}&token={token}"
TIINGO_PRICE_NOTE = "가격원: tiingo, 미조정 종가(close)"


class TiingoProvider(StooqProvider):
    """Fetch a narrowly bounded Tiingo daily-price window.

    The cache identity and every stored receipt use a redacted URL. The raw
    token exists only in the in-memory request URL passed to the opener.
    """

    def __init__(
        self,
        cache_dir: str | Path = "cache/prices",
        receipts_path: str | Path | None = None,
        *,
        opener: Callable[..., Any] | None = None,
        user_agent: str = "Thesis red-team price verification research@example.com",
        api_key: str | None = None,
        key_path: str | Path | None = None,
    ) -> None:
        super().__init__(cache_dir=cache_dir, receipts_path=receipts_path, opener=opener, user_agent=user_agent)
        if api_key is None:
            api_key = os.environ.get("TIINGO_API_KEY")
            if not api_key:
                configured_path = Path(key_path).expanduser() if key_path else Path.home() / ".config" / "thesis-redteam" / "tiingo.key"
                try:
                    api_key = configured_path.read_text(encoding="utf-8")
                except OSError:
                    api_key = None
        self.api_key = api_key.strip() if api_key else None
        self._redaction_secrets = (self.api_key,) if self.api_key else ()

    def _urls(self, ticker: str, target: date) -> tuple[str, str] | None:
        if not self.api_key:
            return None
        start = target - timedelta(days=7)
        end = target + timedelta(days=7)
        raw = TIINGO_URL.format(ticker=str(ticker).strip().upper(), start=start.isoformat(), end=end.isoformat(), token=self.api_key)
        redacted = TIINGO_URL.format(ticker=str(ticker).strip().upper(), start=start.isoformat(), end=end.isoformat(), token="REDACTED")
        return raw, redacted

    @staticmethod
    def _parse_rows(body: bytes) -> list[tuple[date, Decimal]]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        rows: list[tuple[date, Decimal]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            raw_date = str(item.get("date") or "")[:10]
            raw_close = item.get("close")
            if not raw_date or raw_close in (None, ""):
                continue
            try:
                trading_date = date.fromisoformat(raw_date)
                close = Decimal(str(raw_close))
            except (ValueError, InvalidOperation):
                continue
            if close.is_finite() and close > 0:
                rows.append((trading_date, close))
        return rows

    def get_price(self, ticker: str, on_or_before: str) -> PriceQuote | None:
        target = date.fromisoformat(str(on_or_before)[:10])
        normalized = str(ticker).strip()
        if not normalized:
            return None
        urls = self._urls(normalized, target)
        if urls is None:
            return None
        request_url, stored_url = urls
        body, receipt = self._body_and_receipt(request_url, cache_url=stored_url, source="tiingo")
        candidates = [(trading_date, close) for trading_date, close in self._parse_rows(body) if trading_date <= target]
        if not candidates:
            return None
        trading_date, close = max(candidates, key=lambda item: item[0])
        return PriceQuote(
            ticker=normalized.upper(),
            date=trading_date.isoformat(),
            close=close,
            source="tiingo",
            url=stored_url,
            receipt=receipt,
        )

    price_on_or_before = get_price


class FakePriceProvider:
    """Deterministic price provider for tests and offline demonstrations."""

    def __init__(self, prices: Mapping[str, Mapping[str, Any]]) -> None:
        self.prices: dict[str, dict[str, Decimal]] = {}
        for ticker, rows in prices.items():
            self.prices[str(ticker).upper()] = {
                str(trading_date)[:10]: Decimal(str(value)) for trading_date, value in rows.items()
            }

    def get_price(self, ticker: str, on_or_before: str) -> PriceQuote | None:
        target = date.fromisoformat(str(on_or_before)[:10])
        rows = self.prices.get(str(ticker).upper(), {})
        candidates = [(date.fromisoformat(day), close) for day, close in rows.items() if date.fromisoformat(day) <= target]
        if not candidates:
            return None
        trading_date, close = max(candidates, key=lambda item: item[0])
        return PriceQuote(ticker=str(ticker).upper(), date=trading_date.isoformat(), close=close, source="fake")

    price_on_or_before = get_price


def market_cap_for_fields(
    fields: dict[str, Any],
    *,
    ticker: str,
    provider: PriceProvider,
) -> tuple[Decimal | None, dict[str, Any] | None]:
    """Calculate B03's fiscal-period market cap from same-filing evidence."""

    repurchases = fields.get("repurchase") or []
    issuances = fields.get("common_issuance") or []
    if not repurchases or not issuances:
        return None, None
    pairs = [
        (repurchase, issuance)
        for repurchase in repurchases
        for issuance in issuances
        if (repurchase.get("period_start"), repurchase.get("period_end"))
        == (issuance.get("period_start"), issuance.get("period_end"))
    ]
    if not pairs:
        return None, None
    repurchase, issuance = max(pairs, key=lambda pair: pair[0].get("period_end") or "")
    period_end = repurchase.get("period_end")
    if not period_end:
        return None, None
    accessions = {row.get("accession") for row in (repurchase, issuance) if row.get("accession")}
    share_rows = [
        row
        for row in (fields.get("shares") or [])
        if row.get("tag") == "CommonStockSharesOutstanding"
        and row.get("period_end") == period_end
        and (not accessions or row.get("accession") in accessions)
    ]
    if not share_rows:
        return None, None
    share_row = max(share_rows, key=lambda row: (row.get("filed") or "", row.get("accession") or ""))
    try:
        shares = Decimal(str(share_row["value"]))
    except (KeyError, InvalidOperation):
        return None, None
    if not shares.is_finite() or shares <= 0:
        return None, None
    quote = provider.get_price(ticker, str(period_end))
    if quote is None:
        receipt = getattr(provider, "last_receipt", None)
        return None, {"price_receipt": receipt} if receipt else None
    market_cap = quote.close * shares
    info = {
        "price_date": quote.date,
        "price_close": str(quote.close),
        "price_source": quote.source,
        "price_note": {
            "stooq": PRICE_NOTE,
            "tiingo": TIINGO_PRICE_NOTE,
        }.get(quote.source, "가격원: fake"),
        "method": "price_market_cap",
        "shares_period_end": period_end,
        "shares_accession": share_row.get("accession"),
    }
    if quote.receipt:
        info["price_receipt"] = quote.receipt
    return market_cap, info
