from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class SignedTokens:
    """Small HMAC token codec used for stateless OAuth and setup sessions."""

    def __init__(self, secret: str):
        self._secret = secret.encode("utf-8")

    def issue(self, purpose: str, claims: dict[str, Any], ttl_seconds: int) -> str:
        payload = {
            "v": 1,
            "purpose": purpose,
            "iat": int(time.time()),
            "exp": int(time.time()) + ttl_seconds,
            **claims,
        }
        encoded = _b64encode(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        )
        signature = _b64encode(hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def verify(self, token: str, purpose: str) -> dict[str, Any] | None:
        try:
            encoded, supplied_signature = token.split(".", 1)
            expected_signature = _b64encode(
                hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(supplied_signature, expected_signature):
                return None
            payload = json.loads(_b64decode(encoded))
            if payload.get("purpose") != purpose or int(payload.get("exp", 0)) < int(time.time()):
                return None
            return payload
        except (ValueError, TypeError, json.JSONDecodeError):
            return None


class BridgeRefreshToken(RefreshToken):
    resource: str | None = None


class BridgeOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, BridgeRefreshToken, AccessToken]
):
    """Single-owner OAuth provider with CIMD clients and stateless signed tokens."""

    def __init__(self, base_url: str, resource_url: str, signer: SignedTokens):
        self.base_url = base_url.rstrip("/")
        self.resource_url = resource_url
        self.signer = signer
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._used_codes: set[str] = set()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        cached = self._clients.get(client_id)
        if cached:
            return cached

        parsed = urlparse(client_id)
        host = (parsed.hostname or "").lower()
        allowed = host in {"chatgpt.com", "openai.com"} or host.endswith(
            (".chatgpt.com", ".openai.com")
        )
        if parsed.scheme != "https" or not allowed or not parsed.path or parsed.path == "/":
            return None

        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False) as http:
                response = await http.get(client_id, headers={"Accept": "application/json"})
                response.raise_for_status()
                metadata = response.json()
            redirect_uris = metadata.get("redirect_uris")
            if not isinstance(redirect_uris, list) or not redirect_uris:
                return None
            if metadata.get("client_id") not in (None, client_id):
                return None
            if metadata.get("token_endpoint_auth_method", "none") != "none":
                return None
            client = OAuthClientInformationFull(
                client_id=client_id,
                client_name=metadata.get("client_name", "ChatGPT"),
                redirect_uris=redirect_uris,
                token_endpoint_auth_method="none",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope="health.read",
            )
        except (httpx.HTTPError, ValueError, TypeError):
            return None

        self._clients[client_id] = client
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.resource_url.rstrip("/"):
            raise AuthorizeError(
                error="invalid_target",
                error_description="OAuth resource does not match this MCP server",
            )
        ticket = self.signer.issue(
            "approval",
            {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_explicit": params.redirect_uri_provided_explicitly,
                "scope": params.scopes or ["health.read"],
                "code_challenge": params.code_challenge,
                "resource": self.resource_url,
                "state": params.state,
            },
            ttl_seconds=600,
        )
        return f"{self.base_url}/approve?ticket={quote(ticket)}"

    def approve(self, ticket: str) -> str | None:
        claims = self.signer.verify(ticket, "approval")
        if not claims:
            return None
        code = self.signer.issue(
            "authorization_code",
            {
                "client_id": claims["client_id"],
                "redirect_uri": claims["redirect_uri"],
                "redirect_uri_explicit": claims["redirect_uri_explicit"],
                "scope": claims["scope"],
                "code_challenge": claims["code_challenge"],
                "resource": claims["resource"],
            },
            ttl_seconds=300,
        )
        return construct_redirect_uri(
            claims["redirect_uri"], code=code, state=claims.get("state"), iss=self.base_url
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        if hashlib.sha256(authorization_code.encode()).hexdigest() in self._used_codes:
            return None
        claims = self.signer.verify(authorization_code, "authorization_code")
        if not claims or claims.get("client_id") != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            client_id=claims["client_id"],
            scopes=claims["scope"],
            expires_at=claims["exp"],
            code_challenge=claims["code_challenge"],
            redirect_uri=claims["redirect_uri"],
            redirect_uri_provided_explicitly=claims["redirect_uri_explicit"],
            resource=claims.get("resource"),
            subject="owner",
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._used_codes.add(hashlib.sha256(authorization_code.code.encode()).hexdigest())
        return self._token_pair(
            client.client_id, authorization_code.scopes, authorization_code.resource
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        claims = self.signer.verify(token, "access_token")
        if not claims or claims.get("resource") != self.resource_url:
            return None
        return AccessToken(
            token=token,
            client_id=claims["client_id"],
            scopes=claims["scope"],
            expires_at=claims["exp"],
            resource=claims["resource"],
            subject="owner",
            claims={"iss": self.base_url},
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> BridgeRefreshToken | None:
        claims = self.signer.verify(refresh_token, "refresh_token")
        if not claims or claims.get("client_id") != client.client_id:
            return None
        return BridgeRefreshToken(
            token=refresh_token,
            client_id=claims["client_id"],
            scopes=claims["scope"],
            expires_at=claims["exp"],
            resource=claims.get("resource"),
            subject="owner",
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: BridgeRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        return self._token_pair(client.client_id, scopes, refresh_token.resource)

    async def revoke_token(self, token: AccessToken | BridgeRefreshToken) -> None:
        return None

    def _token_pair(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        normalized_resource = resource or self.resource_url
        access = self.signer.issue(
            "access_token",
            {"client_id": client_id, "scope": scopes, "resource": normalized_resource},
            ttl_seconds=3600,
        )
        refresh = self.signer.issue(
            "refresh_token",
            {"client_id": client_id, "scope": scopes, "resource": normalized_resource},
            ttl_seconds=30 * 24 * 3600,
        )
        return OAuthToken(
            access_token=access,
            refresh_token=refresh,
            expires_in=3600,
            scope=" ".join(scopes),
        )
