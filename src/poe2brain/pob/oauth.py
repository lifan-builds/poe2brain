# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
"""Official GGG OAuth 2.1 Public Client (PKCE S256 + localhost loopback) for PoE2.

Strictly ToS-compliant:
  - Uses GGG's official OAuth 2.1 Public Client PKCE flow (same architecture as
    MIT PathOfBuilding-PoE2's `Classes/PoEAPI.lua`, never touching `POESESSID`).
  - Uses poe2brain's own registered `client_id` (overrideable via
    `POE2BRAIN_OAUTH_CLIENT_ID`), never spoofing another application's client_id.
  - All API calls to `https://api.pathofexile.com/character/poe2...` are paced
    through `GGGRateLimiter` with a descriptive `User-Agent`.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field

from poe2brain.config import (
    DEFAULT_GGG_OAUTH_CLIENT_ID,
    GGG_API_BASE_URL,
    GGG_OAUTH_AUTHORIZE_URL,
    GGG_OAUTH_CLIENT_ID_ENV,
    GGG_OAUTH_TOKEN_URL,
    USER_AGENT,
    get_cache_dir,
)
from poe2brain.http import Poe2HttpClient
from poe2brain.models.common import Poe2Model

DEFAULT_OAUTH_SCOPES: tuple[str, ...] = (
    "account:profile",
    "account:leagues",
    "account:characters",
)


class GggOAuthError(RuntimeError):
    """Raised when GGG OAuth 2.1 authorization, refresh, or API fetch fails."""


class OAuthTokenSet(Poe2Model):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_at: float = 0.0
    scope: str = ""
    username: str | None = None
    client_id: str = DEFAULT_GGG_OAUTH_CLIENT_ID


class GggAuthStatus(Poe2Model):
    authenticated: bool
    client_id: str
    token_path: str
    username: str | None = None
    scopes: list[str] = Field(default_factory=list)
    expires_at: float | None = None
    expires_in_seconds: int | None = None
    has_refresh_token: bool = False
    note: str | None = None


class GggCharacterSummary(Poe2Model):
    name: str
    realm: str = "poe2"
    class_name: str = ""
    league: str = ""
    level: int = 1
    experience: int | None = None
    current: bool = False


def get_oauth_client_id() -> str:
    return (
        os.environ.get(GGG_OAUTH_CLIENT_ID_ENV, "").strip()
        or DEFAULT_GGG_OAUTH_CLIENT_ID
    )


def get_oauth_token_path(cache_dir: Path | None = None) -> Path:
    root = cache_dir if cache_dir is not None else get_cache_dir()
    return root / "oauth_tokens.json"


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: Sequence[str],
    state: str,
    code_challenge: str,
) -> str:
    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        },
        quote_via=urllib.parse.quote,
    )
    return f"{GGG_OAUTH_AUTHORIZE_URL}?{params}"


class GggOAuthClient:
    """Manage official GGG OAuth 2.1 PKCE tokens and PoE2 character API fetches."""

    def __init__(
        self,
        *,
        client_id: str | None = None,
        token_path: Path | None = None,
        http_client: Poe2HttpClient | None = None,
    ) -> None:
        self.client_id = client_id or get_oauth_client_id()
        self.token_path = token_path or get_oauth_token_path()
        self._http = http_client

    def load_tokens(self) -> OAuthTokenSet | None:
        if not self.token_path.is_file():
            return None
        try:
            raw = json.loads(self.token_path.read_text(encoding="utf-8"))
            return OAuthTokenSet.model_validate(raw)
        except Exception:
            return None

    def save_tokens(self, tokens: OAuthTokenSet) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_suffix(".tmp")
        tmp.write_text(tokens.model_dump_json(indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
        tmp.replace(self.token_path)

    def logout(self) -> dict[str, Any]:
        existed = self.token_path.is_file()
        if existed:
            self.token_path.unlink()
        return {
            "logged_out": True,
            "had_saved_token": existed,
            "token_path": str(self.token_path),
        }

    def status(self) -> GggAuthStatus:
        tok = self.load_tokens()
        if tok is None:
            return GggAuthStatus(
                authenticated=False,
                client_id=self.client_id,
                token_path=str(self.token_path),
                note=(
                    "Not logged in. Run 'poe2 auth login' to authenticate via official GGG "
                    "OAuth 2.1 PKCE, or use 'poe2 pob import-local' for zero-auth local PoB2 sync."
                ),
            )
        now = time.time()
        remaining = int(tok.expires_at - now)
        expired = remaining <= 0
        return GggAuthStatus(
            authenticated=not expired or bool(tok.refresh_token),
            client_id=tok.client_id or self.client_id,
            token_path=str(self.token_path),
            username=tok.username,
            scopes=tok.scope.split() if tok.scope else [],
            expires_at=tok.expires_at,
            expires_in_seconds=max(0, remaining),
            has_refresh_token=bool(tok.refresh_token),
            note=(
                "Access token expired; will auto-refresh via refresh_token on next call."
                if expired and tok.refresh_token
                else None
            ),
        )

    def login(
        self,
        *,
        scopes: Sequence[str] = DEFAULT_OAUTH_SCOPES,
        timeout: float = 120.0,
        open_browser: bool = True,
    ) -> GggAuthStatus:
        """Run interactive browser OAuth 2.1 PKCE flow with ephemeral localhost callback."""
        code_verifier, code_challenge = _pkce_pair()
        expected_state = secrets.token_hex(16)
        result_box: dict[str, Any] = {}
        done_event = threading.Event()

        class _CallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(parsed.query)
                if "error" in qs:
                    err_desc = qs.get("error_description", qs["error"])[0]
                    result_box["error"] = err_desc
                    body = (
                        "<h2>poe2brain GGG OAuth failed</h2>"
                        f"<p>{html.escape(err_desc)}</p>"
                        "<p>You can close this tab.</p>"
                    )
                    self._send_html(400, body)
                    done_event.set()
                    return

                code = qs.get("code", [""])[0]
                state = qs.get("state", [""])[0]
                if not code:
                    self._send_html(400, "<p>Missing authorization code.</p>")
                    return
                result_box["code"] = code
                result_box["state"] = state
                self._send_html(
                    200,
                    "<h2>poe2brain authorized with Path of Exile!</h2>"
                    "<p>You can close this browser tab and return to your terminal / AI agent.</p>",
                )
                done_event.set()

            def _send_html(self, status: int, body: str) -> None:
                raw = f"<!doctype html><html><body>{body}</body></html>".encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        port = server.server_address[1]
        redirect_uri = f"http://localhost:{port}"
        auth_url = _build_authorize_url(
            client_id=self.client_id,
            redirect_uri=redirect_uri,
            scopes=scopes,
            state=expected_state,
            code_challenge=code_challenge,
        )

        srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
        srv_thread.start()
        try:
            sys.stderr.write(f"[poe2brain] Opening GGG OAuth 2.1 login: {auth_url}\n")
            sys.stderr.flush()
            if open_browser:
                webbrowser.open(auth_url)

            if not done_event.wait(timeout=timeout):
                raise GggOAuthError(
                    f"Timed out after {timeout:.0f}s waiting for GGG OAuth "
                    f"callback on {redirect_uri}."
                )
        finally:
            server.shutdown()
            server.server_close()

        if "error" in result_box:
            raise GggOAuthError(f"GGG OAuth authorization denied: {result_box['error']}")
        if result_box.get("state") != expected_state:
            raise GggOAuthError("GGG OAuth state mismatch; aborting token exchange.")

        tokens = self._exchange_token(
            {
                "client_id": self.client_id,
                "grant_type": "authorization_code",
                "code": result_box["code"],
                "redirect_uri": redirect_uri,
                "scope": " ".join(scopes),
                "code_verifier": code_verifier,
            }
        )
        self.save_tokens(tokens)
        return self.status()

    def _exchange_token(self, form_data: dict[str, str]) -> OAuthTokenSet:
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0) as client:
            resp = client.post(GGG_OAUTH_TOKEN_URL, data=form_data)
            if resp.status_code >= 400:
                detail = resp.text.strip()[:400]
                raise GggOAuthError(
                    f"GGG OAuth token endpoint returned HTTP {resp.status_code}: {detail}"
                )
            data = resp.json()

        access = str(data.get("access_token") or "")
        if not access:
            raise GggOAuthError("GGG OAuth token response missing access_token.")
        expires_in = float(data.get("expires_in") or 3600)
        return OAuthTokenSet(
            access_token=access,
            refresh_token=data.get("refresh_token"),
            token_type=str(data.get("token_type") or "Bearer"),
            expires_at=time.time() + expires_in,
            scope=str(data.get("scope") or form_data.get("scope") or ""),
            username=data.get("username"),
            client_id=self.client_id,
        )

    def ensure_valid_token(self) -> OAuthTokenSet:
        tok = self.load_tokens()
        if tok is None:
            raise GggOAuthError(
                "Not authenticated with GGG OAuth 2.1. Run 'poe2 auth login' first "
                "(or use 'poe2 pob import-local' to load saved local PoB2 builds without login)."
            )
        if tok.expires_at > time.time() + 60:
            return tok
        if not tok.refresh_token:
            raise GggOAuthError(
                "GGG OAuth access token expired and no refresh_token is available. "
                "Run 'poe2 auth login' again."
            )
        refreshed = self._exchange_token(
            {
                "client_id": tok.client_id or self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": tok.refresh_token,
            }
        )
        if not refreshed.refresh_token:
            refreshed.refresh_token = tok.refresh_token
        if not refreshed.username:
            refreshed.username = tok.username
        self.save_tokens(refreshed)
        return refreshed

    def _api_get_json(self, path: str) -> Any:
        tok = self.ensure_valid_token()
        url = f"{GGG_API_BASE_URL}{path}"
        headers = {
            "Authorization": f"Bearer {tok.access_token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if self._http is not None:
            return self._http.get_json(
                url,
                headers=headers,
                ttl_seconds=0.0,
                rate_limit_group="ggg_oauth_api",
            )
        with httpx.Client(headers=headers, timeout=30.0) as client:
            resp = client.get(url)
            if resp.status_code == 401 and tok.refresh_token:
                tok.expires_at = 0.0
                self.save_tokens(tok)
                tok = self.ensure_valid_token()
                headers["Authorization"] = f"Bearer {tok.access_token}"
                resp = client.get(url, headers=headers)
            if resp.status_code >= 400:
                raise GggOAuthError(
                    f"GGG API GET {path} failed with HTTP {resp.status_code}: "
                    f"{resp.text.strip()[:400]}"
                )
            return resp.json()

    def list_characters(self, *, realm: str = "poe2") -> list[GggCharacterSummary]:
        suffix = "" if realm == "pc" else f"/{urllib.parse.quote(realm, safe='')}"
        payload = self._api_get_json(f"/character{suffix}")
        rows = payload.get("characters", []) if isinstance(payload, dict) else []
        out: list[GggCharacterSummary] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("name"):
                continue
            out.append(
                GggCharacterSummary(
                    name=str(row["name"]),
                    realm=str(row.get("realm") or realm),
                    class_name=str(row.get("class") or ""),
                    league=str(row.get("league") or ""),
                    level=int(row.get("level") or 1),
                    experience=(
                        int(row["experience"]) if row.get("experience") is not None else None
                    ),
                    current=bool(row.get("current", False)),
                )
            )
        return out

    def fetch_character_json(self, character_name: str, *, realm: str = "poe2") -> str:
        suffix = "" if realm == "pc" else f"/{urllib.parse.quote(realm, safe='')}"
        encoded_name = urllib.parse.quote(character_name, safe="")
        payload = self._api_get_json(f"/character{suffix}/{encoded_name}")
        return json.dumps(payload, ensure_ascii=False)
