"""Etsy Open API v3 client.

Authenticates with ``x-api-key`` (the Etsy keystring) plus a Bearer OAuth 2.0
access token, both loaded from the environment via :mod:`src.config`.

Follows the Etsy Open API v3 base path ``https://openapi.etsy.com/v3/application``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from .config import get_settings

logger = logging.getLogger(__name__)

_STATUS_HINTS = {
    400: "Bad request — the payload was invalid for Etsy.",
    401: "Authentication failed — your ETSY_ACCESS_TOKEN is missing, expired, or invalid.",
    403: "Forbidden — the token lacks the required scopes (listings_w, listings_r, shops_r).",
    404: "Not found — the listing/shop/resource does not exist or is not accessible.",
    429: "Rate limited — try again after a short delay.",
}


class EtsyAPIError(Exception):
    """Raised when the Etsy Open API returns a non-success response."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        endpoint: str | None = None,
        response_body: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.endpoint = endpoint
        self.response_body = response_body
        hint = _STATUS_HINTS.get(status_code, "")
        detail = f"{message}. {hint}".strip()
        if response_body:
            detail += f" | Body: {response_body[:500]}"
        super().__init__(detail)


class MissingCredentialsError(RuntimeError):
    """Raised when write credentials are absent; not a crash, just a clear message."""


class EtsyClient:
    """Authenticated client for the Etsy Open API v3."""

    def __init__(self) -> None:
        self._s = get_settings()
        self._session = requests.Session()
        self._session.max_redirects = 5

    # -- Credential handling ------------------------------------------------

    def require_credentials(self) -> None:
        """Validate that the token and shop id needed for write calls exist.

        Raises :class:`MissingCredentialsError` with a helpful message instead
        of letting an opaque HTTP 401 bubble up later.
        """
        missing = []
        if not self._s.etsy_keystring:
            missing.append("ETSY_KEYSTRING")
        if not self._s.etsy_access_token:
            missing.append("ETSY_ACCESS_TOKEN")
        if not self._s.etsy_shop_id:
            missing.append("ETSY_SHOP_ID")
        if missing:
            raise MissingCredentialsError(
                "Missing Etsy credentials: " + ", ".join(missing)
                + ". Add them to your .env file (or run scripts/etsy_auth_setup.py "
                "to obtain ETSY_ACCESS_TOKEN)."
            )

    def verify_credentials(self) -> bool:
        """Live-check that the token and shop id actually work on the API.

        Issues a lightweight ``GET /shops/{shop_id}`` (needs the ``shops_r``
        scope). Returns ``True`` on success; any failure -- a 401/403 token
        problem, a 404 for an invalid/placeholder shop id, or a network error
        -- is treated as "not usable right now" and returns ``False`` so
        callers can stay in local/dry-run mode instead of crashing.
        """
        try:
            self.require_credentials()
            self._request(
                "GET", f"/shops/{self._s.etsy_shop_id}", timeout=20, log_errors=False
            )
            return True
        except (EtsyAPIError, MissingCredentialsError) as exc:
            logger.warning(
                "Etsy credential check failed (%s); staying in local/dry-run mode.",
                exc,
            )
            return False

    # -- Request plumbing ----------------------------------------------------

    def _headers(self, *, json: bool) -> dict[str, str]:
        headers = {
            "x-api-key": self._s.etsy_keystring,
            "Authorization": f"Bearer {self._s.etsy_access_token}",
        }
        if json:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
        timeout: int = 60,
        log_errors: bool = True,
    ) -> dict:
        url = f"{self._s.etsy_base_url}/{path.lstrip('/')}"
        is_json = json_body is not None

        try:
            if is_json:
                resp = self._session.request(
                    method, url, json=json_body, headers=self._headers(json=True), timeout=timeout
                )
            else:
                resp = self._session.request(
                    method, url, data=data, files=files,
                    headers=self._headers(json=False), timeout=timeout,
                )
        except requests.exceptions.Timeout as exc:
            raise EtsyAPIError(0, "Request timed out", endpoint=url) from exc
        except requests.exceptions.ConnectionError as exc:
            raise EtsyAPIError(0, f"Connection error: {exc}", endpoint=url) from exc
        except requests.exceptions.RequestException as exc:
            raise EtsyAPIError(0, f"Request failed: {exc}", endpoint=url) from exc

        body = resp.text

        if not resp.ok:
            try:
                err = resp.json()
                message = err.get("error", err.get("detail", "Etsy API error"))
                if isinstance(message, list):
                    message = "; ".join(str(m.get("message", m)) for m in message)
                message = str(message)
            except ValueError:
                message = body or resp.reason
            log = logger.error if log_errors else logger.debug
            log("Etsy API %s -> %s (%s): %s", method, url, resp.status_code, message)
            raise EtsyAPIError(
                resp.status_code, message, endpoint=url, response_body=body
            )

        try:
            return resp.json()
        except ValueError:
            raise EtsyAPIError(
                resp.status_code, "Response was not valid JSON", endpoint=url, response_body=body
            )

    @staticmethod
    def _require_file(file_path: str | Path) -> Path:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        return path

    # -- Public API -----------------------------------------------------------

    def create_draft_listing(
        self,
        title: str,
        description: str,
        tags: list[str],
        price: float,
        taxonomy_id: int = 2047,
    ) -> dict:
        """Create a draft digital-download listing (never publishes directly)."""
        payload = {
            "title": title,
            "description": description,
            "tags": list(tags)[:13],
            "price": round(float(price), 2),
            "quantity": 1,
            "taxonomy_id": int(taxonomy_id),
            "who_made": "i_did",
            "when_made": "2020_2024",
            "type": "download",
            "is_supply": False,
            "state": "draft",
            "is_digital_download": True,
        }
        path = f"/shops/{self._s.etsy_shop_id}/listings"
        return self._request("POST", path, json_body=payload)

    def upload_listing_image(self, listing_id: int, image_path: str) -> dict:
        """Upload a PNG/JPG mockup to a draft listing (multipart)."""
        path = self._require_file(image_path)
        mime = "image/png" if path.suffix.lower() in (".png",) else "image/jpeg"
        upload_path = (
            f"/shops/{self._s.etsy_shop_id}/listings/{int(listing_id)}/images"
        )
        with open(path, "rb") as f:
            return self._request(
                "POST",
                upload_path,
                files={"image": (path.name, f, mime)},
                data={"rank": "1"},
                timeout=120,
            )

    def upload_digital_file(self, listing_id: int, file_path: str) -> dict:
        """Upload the final digital asset (e.g. PDF) to a draft listing."""
        path = self._require_file(file_path)
        upload_path = (
            f"/shops/{self._s.etsy_shop_id}/listings/{int(listing_id)}/files"
        )
        with open(path, "rb") as f:
            return self._request(
                "POST",
                upload_path,
                files={"file": (path.name, f, "application/octet-stream")},
                data={"name": path.name},
                timeout=300,
            )
