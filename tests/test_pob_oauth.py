# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 lifan-builds. poe2brain; Section 7 attribution terms in ATTRIBUTION.md.
from functools import partial
from pathlib import Path
from time import time

import httpx
import pytest

from poe2brain.http import Poe2HttpClient
from poe2brain.pob.oauth import GggOAuthClient, GggOAuthError, OAuthTokenSet


@pytest.mark.parametrize("race", ["refresh", "replace", "logout"])
@pytest.mark.parametrize("shared_http", [False, True])
def test_401_reloads_saved_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race: str, shared_http: bool
) -> None:
    path = tmp_path / "oauth_tokens.json"
    other = GggOAuthClient(token_path=path)
    original = OAuthTokenSet(access_token="first", refresh_token="one", expires_at=time() + 3600)
    rotated = OAuthTokenSet(access_token="second", refresh_token="two", expires_at=time() + 3600)
    other.save_tokens(original)
    requests = []
    exchanges = []

    def api(request: httpx.Request) -> httpx.Response:
        requests.append(request.headers["Authorization"])
        if len(requests) == 1:
            if race == "replace":
                other.save_tokens(rotated)
            elif race == "logout":
                other.logout()
            return httpx.Response(401)
        return httpx.Response(200, json={"characters": []})

    def exchange(form: dict[str, str]) -> OAuthTokenSet:
        assert other.load_tokens() == original
        exchanges.append(form["refresh_token"])
        return rotated

    transport = httpx.MockTransport(api)
    http = Poe2HttpClient(sync_transport=transport) if shared_http else None
    if http is None:
        monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=transport))
    client = GggOAuthClient(token_path=path, http_client=http)
    monkeypatch.setattr(client, "_exchange_token", exchange)

    if race == "logout":
        with pytest.raises(GggOAuthError, match="Not authenticated"):
            client.list_characters()
        assert not path.exists()
        assert requests == ["Bearer first"]
    else:
        assert client.list_characters() == []
        assert requests == ["Bearer first", "Bearer second"]
        assert other.load_tokens() == rotated
    assert exchanges == (["one"] if race == "refresh" else [])


def test_expired_token_refresh_holds_file_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fcntl = pytest.importorskip("fcntl")
    client = GggOAuthClient(token_path=tmp_path / "oauth_tokens.json")
    client.save_tokens(OAuthTokenSet(access_token="first", refresh_token="one"))

    def exchange(_form: dict[str, str]) -> OAuthTokenSet:
        with (tmp_path / "oauth_tokens.lock").open("rb") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return OAuthTokenSet(access_token="second", refresh_token="two", expires_at=time() + 3600)

    monkeypatch.setattr(client, "_exchange_token", exchange)
    refreshed = client.ensure_valid_token()
    assert refreshed.access_token == "second"
    assert client.load_tokens() == refreshed
