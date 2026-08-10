"""DataForSEO client for real search volume and keyword difficulty.

Two endpoints are used:

  keywords_data/google_ads/search_volume/live   -> monthly search volume, competition
  dataforseo_labs/google/bulk_keyword_difficulty/live -> difficulty 0-100

Both accept keyword batches, so the pipeline fetches metrics for every discovered
query in two requests rather than two per query. On a trial account that is the
difference between burning credits in one run and burning them in twenty.

Nothing here fabricates a value. If the provider has no data for a keyword, the
metric comes back as None and the scoring formula deals with the gap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

# DataForSEO rejects oversized keyword arrays; both endpoints cap at 1000, but
# keeping batches small keeps error blast radius small too.
MAX_KEYWORDS_PER_REQUEST = 100

# Their tasks carry their own status codes independent of HTTP status.
_TASK_OK = 20000


class DataForSEOError(RuntimeError):
    """DataForSEO was unreachable or returned an error payload."""


@dataclass(frozen=True)
class KeywordMetrics:
    keyword: str
    search_volume: int | None = None
    competition_index: int | None = None
    keyword_difficulty: int | None = None
    cpc: float | None = None

    @property
    def difficulty(self) -> int | None:
        """Preferred difficulty signal, falling back to the Ads competition index.

        Keyword difficulty is the better measure but is a Labs endpoint and is not
        always populated for long-tail conversational queries. Competition index is
        also 0-100, so the fallback stays on the same scale.
        """
        if self.keyword_difficulty is not None:
            return self.keyword_difficulty
        return self.competition_index


class DataForSEOClient:
    def __init__(
        self,
        login: str | None,
        password: str | None,
        *,
        base_url: str = "https://api.dataforseo.com",
        location_code: int = 2840,
        language_code: str = "en",
        timeout: int = 60,
        session: requests.Session | None = None,
    ) -> None:
        if not login or not password:
            raise DataForSEOError("DATAFORSEO_LOGIN and DATAFORSEO_PASSWORD must be set")
        self._auth = (login, password)
        self._base_url = base_url.rstrip("/")
        self._location_code = location_code
        self._language_code = language_code
        self._timeout = timeout
        self._session = session or requests.Session()

    # --- transport -----------------------------------------------------------

    def _post(self, path: str, payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
        url = f"{self._base_url}{path}"
        try:
            response = self._session.post(
                url, json=payload, auth=self._auth, timeout=self._timeout
            )
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise DataForSEOError(f"DataForSEO request to {path} failed: {exc}") from exc
        except ValueError as exc:
            raise DataForSEOError(f"DataForSEO returned non-JSON from {path}") from exc

        if body.get("status_code") != _TASK_OK:
            raise DataForSEOError(
                f"DataForSEO error {body.get('status_code')}: {body.get('status_message')}"
            )

        tasks = body.get("tasks") or []
        results: list[dict[str, Any]] = []
        for task in tasks:
            if task.get("status_code") != _TASK_OK:
                # One failed task in a batch should not discard the others.
                logger.warning(
                    "dataforseo task failed",
                    extra={
                        "path": path,
                        "task_status": task.get("status_code"),
                        "task_message": task.get("status_message"),
                    },
                )
                continue
            for result in task.get("result") or []:
                if isinstance(result, dict):
                    results.append(result)
        return results

    @staticmethod
    def _batched(keywords: list[str]) -> list[list[str]]:
        return [
            keywords[i : i + MAX_KEYWORDS_PER_REQUEST]
            for i in range(0, len(keywords), MAX_KEYWORDS_PER_REQUEST)
        ]

    # --- endpoints -----------------------------------------------------------

    def search_volume(self, keywords: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for batch in self._batched(keywords):
            payload = [
                {
                    "keywords": batch,
                    "location_code": self._location_code,
                    "language_code": self._language_code,
                    # Long conversational queries have no exact-match Ads data; partial
                    # match lets DataForSEO return the closest grouped estimate.
                    "search_partners": False,
                }
            ]
            for row in self._post("/v3/keywords_data/google_ads/search_volume/live", payload):
                keyword = (row.get("keyword") or "").strip().lower()
                if keyword:
                    out[keyword] = row
        return out

    def keyword_difficulty(self, keywords: list[str]) -> dict[str, int | None]:
        out: dict[str, int | None] = {}
        for batch in self._batched(keywords):
            payload = [
                {
                    "keywords": batch,
                    "location_code": self._location_code,
                    "language_code": self._language_code,
                }
            ]
            rows = self._post(
                "/v3/dataforseo_labs/google/bulk_keyword_difficulty/live", payload
            )
            for row in rows:
                for item in row.get("items") or []:
                    keyword = (item.get("keyword") or "").strip().lower()
                    if keyword:
                        out[keyword] = item.get("keyword_difficulty")
        return out

    def fetch_metrics(self, keywords: list[str]) -> dict[str, KeywordMetrics]:
        """Fetch volume and difficulty for a batch of keywords.

        Difficulty failing is not fatal: volume alone still produces a usable score,
        and losing the whole batch because a Labs endpoint is unavailable would be a
        worse trade.
        """
        cleaned = [k.strip() for k in keywords if k and k.strip()]
        if not cleaned:
            return {}

        volume_rows = self.search_volume(cleaned)

        try:
            difficulty_rows = self.keyword_difficulty(cleaned)
        except DataForSEOError as exc:
            logger.warning("keyword difficulty unavailable", extra={"error": str(exc)})
            difficulty_rows = {}

        metrics: dict[str, KeywordMetrics] = {}
        for keyword in cleaned:
            key = keyword.strip().lower()
            row = volume_rows.get(key, {})
            metrics[key] = KeywordMetrics(
                keyword=keyword,
                search_volume=row.get("search_volume"),
                competition_index=row.get("competition_index"),
                keyword_difficulty=difficulty_rows.get(key),
                cpc=row.get("cpc"),
            )
        return metrics
