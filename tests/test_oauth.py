from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from app.oauth import BridgeOAuthProvider, SignedTokens


def test_signed_tokens_reject_tampering() -> None:
    signer = SignedTokens("test-secret")
    token = signer.issue("example", {"value": 7}, ttl_seconds=60)
    assert signer.verify(token, "example")["value"] == 7
    assert signer.verify(f"x{token}", "example") is None
    assert signer.verify(token, "different") is None


@pytest.mark.asyncio
async def test_cimd_accepts_chatgpt_public_client_method(monkeypatch: pytest.MonkeyPatch) -> None:
    client_id = "https://chatgpt.com/oauth/example/client.json"

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == client_id
        return httpx.Response(
            200,
            json={
                "client_id": client_id,
                "client_name": "ChatGPT",
                "redirect_uris": ["https://chatgpt.com/connector/oauth/example"],
                "token_endpoint_auth_method": "private_key_jwt",
                "token_endpoint_auth_methods_supported": ["none", "private_key_jwt"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )

    real_async_client = httpx.AsyncClient

    def mock_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_async_client)
    provider = BridgeOAuthProvider(
        "https://bridge.example", "https://bridge.example/mcp", SignedTokens("test-secret")
    )

    client = await provider.get_client(client_id)

    assert client is not None
    assert client.token_endpoint_auth_method == "none"
    assert [str(uri) for uri in client.redirect_uris] == [
        "https://chatgpt.com/connector/oauth/example"
    ]


@pytest.mark.asyncio
async def test_cimd_rejects_client_without_public_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_id = "https://chatgpt.com/oauth/example/client.json"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "client_id": client_id,
                "redirect_uris": ["https://chatgpt.com/connector/oauth/example"],
                "token_endpoint_auth_method": "private_key_jwt",
                "token_endpoint_auth_methods_supported": ["private_key_jwt"],
            },
        )

    real_async_client = httpx.AsyncClient

    def mock_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_async_client)
    provider = BridgeOAuthProvider(
        "https://bridge.example", "https://bridge.example/mcp", SignedTokens("test-secret")
    )

    assert await provider.get_client(client_id) is None


@pytest.mark.asyncio
async def test_stateless_oauth_round_trip() -> None:
    signer = SignedTokens("test-secret")
    provider = BridgeOAuthProvider(
        "https://bridge.example", "https://bridge.example/mcp", signer
    )
    client = OAuthClientInformationFull(
        client_id="https://chatgpt.com/client.json",
        redirect_uris=["https://chatgpt.com/callback"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        scope="health.read",
    )
    await provider.register_client(client)

    approval_url = await provider.authorize(
        client,
        AuthorizationParams(
            state="state-1",
            scopes=["health.read"],
            code_challenge="challenge",
            redirect_uri="https://chatgpt.com/callback",
            redirect_uri_provided_explicitly=True,
            resource="https://bridge.example/mcp",
        ),
    )
    ticket = parse_qs(urlparse(approval_url).query)["ticket"][0]
    callback = provider.approve(ticket)
    code = parse_qs(urlparse(callback).query)["code"][0]

    loaded_code = await provider.load_authorization_code(client, code)
    assert loaded_code is not None
    token_pair = await provider.exchange_authorization_code(client, loaded_code)
    access = await provider.load_access_token(token_pair.access_token)
    assert access is not None
    assert access.scopes == ["health.read"]
    assert await provider.load_authorization_code(client, code) is None

    refresh = await provider.load_refresh_token(client, token_pair.refresh_token)
    assert refresh is not None
    refreshed = await provider.exchange_refresh_token(client, refresh, ["health.read"])
    assert await provider.load_access_token(refreshed.access_token) is not None
