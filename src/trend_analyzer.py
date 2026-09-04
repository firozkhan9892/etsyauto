from __future__ import annotations

import logging
import random
import re
import time
from typing import Any

import requests
from pytrends.request import TrendReq

logger = logging.getLogger(__name__)

_ETSY_AUTOCOMPLETE_URL = "https://ac.etsy.com/v3/public/suggestions/typeahead"

_RATE_LIMIT_DELAY = (1.0, 3.0)
_PYTRENDS_TIMEOUT = 30
_REQUEST_TIMEOUT = 15
_MAX_RETRIES = 3
_BACKOFF_BASE = 2.0

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
]


def _random_delay() -> None:
    delay = random.uniform(*_RATE_LIMIT_DELAY)
    time.sleep(delay)


def _clean_keyword(raw: str) -> str:
    cleaned = raw.strip().lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"[^a-z0-9\s\-]", "", cleaned)
    return cleaned.strip()


def _is_valid_keyword(kw: str) -> bool:
    return len(kw) >= 2 and not kw.isdigit()


def _deduplicate(keywords: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for kw in keywords:
        normalised = _clean_keyword(kw)
        if normalised and normalised not in seen and _is_valid_keyword(normalised):
            seen.add(normalised)
            result.append(normalised)
    return result


def _retry_request(
    method: str,
    url: str,
    *,
    retries: int = _MAX_RETRIES,
    timeout: int = _REQUEST_TIMEOUT,
    **kwargs: Any,
) -> requests.Response | None:
    for attempt in range(1, retries + 1):
        try:
            resp = requests.request(method, url, timeout=timeout, **kwargs)
            if resp.status_code == 429:
                wait = _BACKOFF_BASE ** attempt + random.uniform(0, 1)
                logger.warning("Rate limited (429). Backing off %.1fs (attempt %d/%d)", wait, attempt, retries)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.Timeout:
            logger.warning("Request timed out (attempt %d/%d): %s", attempt, retries, url)
        except requests.exceptions.ConnectionError as exc:
            logger.warning("Connection error (attempt %d/%d): %s", attempt, retries, exc)
        except requests.exceptions.HTTPError as exc:
            # 403 from Etsy's public autocomplete endpoint is expected block
            # behaviour (bot detection); it is non-fatal so log softly and stop.
            if getattr(exc.response, "status_code", None) == 403:
                logger.info(
                    "Etsy autocomplete blocked (403) for %s; treating as no suggestions.",
                    url,
                )
                return None
            logger.error("HTTP error: %s", exc)
            return None

        if attempt < retries:
            wait = _BACKOFF_BASE ** attempt
            time.sleep(wait)

    logger.error("All %d retries exhausted for %s", retries, url)
    return None


# ---------------------------------------------------------------------------
# 1. Google Trends – rising queries
# ---------------------------------------------------------------------------

def fetch_rising_trends(
    seed_keywords: list[str],
    geo: str = "US",
    timeframe: str = "now 7-d",
) -> list[dict[str, Any]]:
    """Query Google Trends for *rising* related queries tied to each seed keyword.

    Returns a list sorted by ``breakout`` score (highest first), each entry::

        {
            "keyword": "<rising query>",
            "breakout": 1200,       # 0-2000 scale; 2000 = absolute breakout
            "parent": "<seed keyword>",
        }

    Empty list on total failure so callers can always iterate safely.
    """
    if not seed_keywords:
        return []

    pytrends = TrendReq(hl="en-US", tz=360, timeout=_PYTRENDS_TIMEOUT)

    # pytrends caps payloads at 5 keywords per request
    batches = [seed_keywords[i : i + 5] for i in range(0, len(seed_keywords), 5)]

    all_results: list[dict[str, Any]] = []

    for batch in batches:
        try:
            pytrends.build_payload(batch, timeframe=timeframe, geo=geo)
        except Exception as exc:
            logger.error("pytrends build_payload failed for batch %s: %s", batch, exc)
            _random_delay()
            continue

        _random_delay()

        try:
            related = pytrends.related_queries()
        except Exception as exc:
            logger.error("pytrends related_queries failed for batch %s: %s", batch, exc)
            _random_delay()
            continue

        for seed in batch:
            entry = related.get(seed)
            if entry is None:
                continue

            rising_df = entry.get("rising")
            if rising_df is None or rising_df.empty:
                continue

            for _, row in rising_df.iterrows():
                query = row.get("query", "")
                breakout = int(row.get("value", 0))
                cleaned = _clean_keyword(str(query))
                if cleaned and _is_valid_keyword(cleaned):
                    all_results.append(
                        {
                            "keyword": cleaned,
                            "breakout": breakout,
                            "parent": seed,
                        }
                    )

        _random_delay()

    all_results.sort(key=lambda r: r["breakout"], reverse=True)

    # deduplicate while keeping highest breakout value
    deduped: dict[str, dict[str, Any]] = {}
    for r in all_results:
        existing = deduped.get(r["keyword"])
        if existing is None or r["breakout"] > existing["breakout"]:
            deduped[r["keyword"]] = r

    return list(deduped.values())


# ---------------------------------------------------------------------------
# 2. Etsy autocomplete – buyer-intent validation
# ---------------------------------------------------------------------------

def fetch_etsy_autocomplete(query: str) -> list[str]:
    """Hit Etsy's public typeahead API and return deduplicated keyword strings.

    Useful for validating whether a trending topic has real buyer search
    volume on Etsy and for discovering long-tail variations.

    Returns clean, lowercased, deduplicated keyword strings.
    """
    clean_q = _clean_keyword(query)
    if not clean_q:
        return []

    params = {"q": clean_q, "section": "all", "page": 1}
    headers = {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "application/json",
        "Referer": "https://www.etsy.com/",
    }

    resp = _retry_request("GET", _ETSY_AUTOCOMPLETE_URL, params=params, headers=headers)
    if resp is None:
        return []

    try:
        payload = resp.json()
    except ValueError:
        logger.warning("Non-JSON response from Etsy autocomplete for query=%r", clean_q)
        return []

    raw_suggestions: list[str] = payload.get("results", []) if isinstance(payload, dict) else []
    return _deduplicate(raw_suggestions)
