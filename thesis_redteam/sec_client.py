"""Small SEC EDGAR client with deterministic cache and offline support."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
import urllib.error
import urllib.request


DEFAULT_USER_AGENT = "Public historical filing verification research@example.com"
SEC_DATA = "https://data.sec.gov"
SEC_PUBLIC = "https://www.sec.gov"
SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"


class OfflineDataUnavailable(RuntimeError):
    """Raised when offline mode has neither a cached nor fixture response."""


class SecNetworkError(RuntimeError):
    """A network failure with enough context to produce an auditable report."""

    def __init__(self, url: str, status: Any, original: BaseException) -> None:
        self.url = url
        self.status = status
        self.original = original
        super().__init__(f"SEC request failed: {url} {status}")


class SecClient:
    """Fetch SEC responses, never exceeding five requests per second."""

    def __init__(
        self,
        cache_dir: str | Path = "cache",
        receipts_path: str | Path | None = None,
        *,
        offline: bool = False,
        fixture_dir: str | Path | None = None,
        user_agent: str | None = None,
        opener: Callable[..., Any] | None = None,
        min_interval: float = 0.21,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.receipts_path = Path(receipts_path) if receipts_path else self.cache_dir / "receipts.jsonl"
        self.receipts_path.parent.mkdir(parents=True, exist_ok=True)
        self.fixture_dir = Path(fixture_dir) if fixture_dir else None
        self.offline = offline
        self.user_agent = user_agent or os.environ.get("SEC_USER_AGENT", DEFAULT_USER_AGENT)
        self._opener = opener or urllib.request.urlopen
        self._min_interval = max(min_interval, 0.2)
        self._last_request = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def url_key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = self.url_key(url)
        return self.cache_dir / f"{key}.body", self.cache_dir / f"{key}.json"

    def _append_receipt(self, url: str, body: bytes, fetched_at: str, source: str) -> dict[str, Any]:
        receipt = {
            "url_key": self.url_key(url),
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
            "fetched_at": fetched_at,
            "source": source,
        }
        with self.receipts_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n")
        return receipt

    def _append_error_receipt(self, url: str, status: Any, error: BaseException) -> dict[str, Any]:
        receipt = {
            "url": url,
            "url_key": self.url_key(url),
            "status": status,
            "error": str(error),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "network_error",
        }
        with self.receipts_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n")
        return receipt

    def _store(self, url: str, body: bytes, *, fetched_at: str, source: str) -> dict[str, Any]:
        body_path, meta_path = self._paths(url)
        body_path.write_bytes(body)
        meta = {
            "url_key": self.url_key(url),
            "sha256": hashlib.sha256(body).hexdigest(),
            "bytes": len(body),
            "fetched_at": fetched_at,
            "source": source,
        }
        meta_path.write_text(json.dumps(meta, sort_keys=True) + "\n", encoding="utf-8")
        self._append_receipt(url, body, fetched_at, source)
        return meta

    def _fixture_path(self, url: str) -> Path | None:
        if self.fixture_dir is None:
            return None
        mapping_path = self.fixture_dir / "url_map.json"
        if not mapping_path.exists():
            return None
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        relative = mapping.get(self.url_key(url))
        if not relative:
            return None
        candidate = (self.fixture_dir / relative).resolve()
        root = self.fixture_dir.resolve()
        if root not in candidate.parents and candidate != root:
            raise ValueError("fixture path escapes fixture directory")
        return candidate if candidate.is_file() else None

    def get(self, url: str) -> bytes:
        body_path, meta_path = self._paths(url)
        if body_path.is_file() and meta_path.is_file():
            return body_path.read_bytes()

        fixture = self._fixture_path(url)
        if fixture:
            body = fixture.read_bytes()
            self._store(
                url,
                body,
                fetched_at=datetime.now(timezone.utc).isoformat(),
                source="fixture",
            )
            return body

        if self.offline:
            raise OfflineDataUnavailable(f"offline mode has no cache/fixture for {self.url_key(url)}")

        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
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
                self._append_error_receipt(url, exc.code, exc)
                raise SecNetworkError(url, exc.code, exc) from exc
            except urllib.error.URLError as exc:
                status = str(getattr(exc, "reason", exc))
                self._append_error_receipt(url, status, exc)
                raise SecNetworkError(url, status, exc) from exc
            self._last_request = time.monotonic()
        self._store(
            url,
            body,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            source="network",
        )
        return body

    def get_json(self, url: str) -> dict[str, Any]:
        return json.loads(self.get(url).decode("utf-8"))

    def submissions(self, cik: str | int) -> dict[str, Any]:
        return self.get_json(f"{SEC_DATA}/submissions/CIK{int(cik):010d}.json")

    def companyfacts(self, cik: str | int) -> dict[str, Any]:
        return self.get_json(f"{SEC_DATA}/api/xbrl/companyfacts/CIK{int(cik):010d}.json")

    def ticker_map(self) -> dict[str, Any]:
        return self.get_json(f"{SEC_PUBLIC}/files/company_tickers.json")

    def filing_index(self, cik: str | int, accession: str) -> dict[str, Any]:
        acc_no_dash = accession.replace("-", "")
        return self.get_json(f"{SEC_ARCHIVES}/{int(cik)}/{acc_no_dash}/index.json")

    def filing_index_html(self, cik: str | int, accession: str) -> bytes:
        """Fetch the HTML filing index used as the SGML acceptance-time source."""
        acc_no_dash = accession.replace("-", "")
        return self.get(f"{SEC_ARCHIVES}/{int(cik)}/{acc_no_dash}/{accession}-index.htm")

    def primary_document(self, cik: str | int, accession: str, primary_document: str) -> bytes:
        acc_no_dash = accession.replace("-", "")
        return self.get(f"{SEC_ARCHIVES}/{int(cik)}/{acc_no_dash}/{primary_document}")

