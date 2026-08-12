from urllib.parse import parse_qs, urlparse

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
