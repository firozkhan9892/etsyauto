#!/usr/bin/env python
"""Interactive OAuth 2.0 PKCE flow for Etsy Open API v3.

Run from the project root:

    python scripts/etsy_auth_setup.py

Prerequisites (see https://developers.etsy.com):
  - An Etsy API key (ETSY_API_KEY) and its keystring (ETSY_KEYSTRING).
  - A registered OAuth redirect URI of exactly:
        http://localhost:3003
  (Configure it in the Etsy UI under "Advanced" -> "Auth callback URL".)

This script:
  1. Generates a PKCE code_verifier / code_challenge pair (S256).
  2. Opens the Etsy authorization URL with the needed scopes.
  3. Starts a local HTTP server on localhost:3003 to catch the redirect
     containing the authorization code.
  4. Exchanges the code for access/refresh tokens.
  5. Writes the tokens (plus expiry) into the project's .env file.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
ENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.example"

AUTHORIZE_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"

REDIRECT_HOST = "localhost"
REDIRECT_PORT = 3003
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}"

REQUIRED_SCOPES = ["listings_w", "listings_r", "shops_r"]


class AuthFlowError(RuntimeError):
    """Raised when the PKCE flow cannot be completed."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce(byte_length: int = 64) -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using the S256 method."""
    code_verifier = _b64url(secrets.token_bytes(byte_length))
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = _b64url(digest)
    return code_verifier, code_challenge


def build_authorize_url(
    client_id: str,
    code_challenge: str,
    state: str,
    *,
    scopes: list[str] | None = None,
) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": " ".join(scopes or REQUIRED_SCOPES),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


class RedirectHandler(BaseHTTPRequestHandler):
    """One-shot HTTP handler that captures the OAuth ``code`` and ``state``."""

    capture: dict | None = None
    event: threading.Event | None = None

    def log_message(self, _format: str, *args: object) -> None:
        logger.debug(_format, *args)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        error = params.get("error", [None])[0]

        if error:
            self._respond(
                400,
                f"<h1>Authorization failed</h1><p>Etsy reported: "
                f"<code>{error}</code></p><p>You may close this window.</p>",
            )
            if self.capture is not None:
                self.capture["error"] = error
            self._finish()
            return

        if not code or not state:
            self._respond(
                400,
                "<h1>Missing code or state</h1><p>Close this window and retry.</p>",
            )
            self._finish()
            return

        if self.capture is not None:
            self.capture["code"] = code
            self.capture["state"] = state

        self._respond(
            200,
            "<h1>Authorization successful!</h1>"
            "<p>You may close this window and return to your terminal.</p>",
        )
        self._finish()

    def _finish(self) -> None:
        if self.event is not None:
            self.event.set()

    def _respond(self, status: int, body: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))


def listen_for_code(timeout: int = 300) -> dict:
    """Start a local HTTP server and wait for the OAuth redirect."""
    server = HTTPServer(
        (REDIRECT_HOST, REDIRECT_PORT),
        RedirectHandler,
    )
    capture: dict = {}
    event = threading.Event()
    RedirectHandler.capture = capture
    RedirectHandler.event = event

    print(f"\n  Waiting for redirect at {REDIRECT_URI} "
          f"(up to {timeout}s)...")

    try:
        server.handle_request()  # blocks until ONE request arrives
        if not event.wait(timeout=timeout):
            raise AuthFlowError(
                f"Timed out after {timeout}s waiting for Etsy redirect. "
                "Try running the script again."
            )
    finally:
        server.server_close()

    if "error" in capture:
        raise AuthFlowError(f"Etsy rejected the request: {capture['error']}")
    if "code" not in capture or "state" not in capture:
        raise AuthFlowError("Redirect did not include the expected code/state.")

    return capture


def exchange_code(
    client_id: str,
    code_verifier: str,
    code: str,
    state: str,
    expected_state: str,
    *,
    timeout: int = 30,
) -> dict:
    """Exchange the authorization code for tokens via Etsy's token endpoint."""
    if state != expected_state:
        raise AuthFlowError("state mismatch: possible CSRF. Aborting.")

    payload = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "code": code,
        "code_verifier": code_verifier,
    }

    try:
        resp = requests.post(TOKEN_URL, data=payload, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise AuthFlowError(f"Token request failed: {exc}") from exc

    if not resp.ok:
        raise AuthFlowError(
            f"Token endpoint returned HTTP {resp.status_code}: {resp.text[:500]}"
        )

    tokens = resp.json()

    missing = {"access_token", "refresh_token", "expires_in"} - tokens.keys()
    if missing:
        raise AuthFlowError(
            f"Token response missing fields: {', '.join(sorted(missing))}"
        )

    return tokens


def upsert_env(updates: dict[str, str]) -> Path:
    """Insert or update KEY=VALUE pairs into the project's .env file."""
    if not ENV_PATH.exists():
        print(f"\n  Creating new {ENV_PATH} ...")
        ENV_PATH.write_text("", encoding="utf-8")
    else:
        print(f"\n  Updating existing {ENV_PATH} ...")

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    existing_keys = {line.split("=", 1)[0].strip() for line in lines if "=" in line}

    new_lines: list[str] = []
    for line in lines:
        if "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates.pop(key)}")
                continue
        new_lines.append(line)

    for key, value in updates.items():
        new_lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return ENV_PATH


def _load_env_var(name: str, fallback: str | None = None) -> str:
    value = os.environ.get(name, "").strip()
    if not value and fallback:
        value = os.environ.get(fallback, "").strip()
    if not value:
        raise AuthFlowError(
            f"Missing required environment variable {name}. "
            f"Copy {ENV_EXAMPLE_PATH.name} to .env and fill it in first."
        )
    return value


def print_banner() -> None:
    print("=" * 60)
    print(" ETSY OAUTH 2.0 PKCE SETUP")
    print("=" * 60)
    print(f" Redirect URI : {REDIRECT_URI}")
    print(f" Scopes       : {', '.join(REQUIRED_SCOPES)}")
    print(f" Env file     : {ENV_PATH}")
    print("=" * 60)


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    print_banner()

    try:
        # Etsy OAuth2 client_id is the API keystring. Fall back to
        # ETSY_KEYSTRING when ETSY_API_KEY is not set.
        client_id = _load_env_var("ETSY_API_KEY", fallback="ETSY_KEYSTRING")

        code_verifier, code_challenge = generate_pkce()
        state = _b64url(secrets.token_bytes(32))

        authorize_url = build_authorize_url(client_id, code_challenge, state)

        print("\n[1/4] Generating PKCE challenge... done.")

        print("\n[2/4] Opening your browser to authorize Etsy access.")
        print("      If it doesn't open, paste this URL into your browser:\n")
        print(f"      {authorize_url}\n")

        try:
            webbrowser.open(authorize_url)
        except Exception:
            pass  # browser failures are non-fatal; user can paste URL

        redirect = listen_for_code()
        print("\n  Redirect received with authorization code.")

        print("\n[3/4] Exchanging code for access + refresh tokens...")
        tokens = exchange_code(
            client_id,
            code_verifier,
            redirect["code"],
            redirect["state"],
            state,
        )

        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]
        expires_in = int(tokens["expires_in"])
        issued_at = int(time.time())
        expires_at = issued_at + expires_in

        print("\n[4/4] Writing tokens into .env...")
        env_path = upsert_env(
            {
                "ETSY_ACCESS_TOKEN": access_token,
                "ETSY_REFRESH_TOKEN": refresh_token,
                "ETSY_TOKEN_EXPIRES_AT": str(expires_at),
            }
        )

    except AuthFlowError as exc:
        logger.error("Authentication failed: %s", exc)
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1

    print("\n" + "=" * 60)
    print(" SUCCESS")
    print("=" * 60)
    print(f" Access token  : saved")
    print(f" Refresh token : saved")
    print(f" Expires at    : {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expires_at))}")
    print(f" Env file      : {env_path}")
    print("=" * 60)
    print("\nYour access token is now available to the pipeline.\n"
          "Remember to also set ETSY_SHOP_ID in .env before running main.py.")
    print("NOTE: scopes like listings_w expire; use the refresh token to\n"
          "renew, or re-run this script when the access token expires.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
