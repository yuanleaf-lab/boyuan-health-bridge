from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    public_base_url: str
    owner_secret: str
    signing_secret: str
    mi_token_json: str

    @property
    def mcp_url(self) -> str:
        return f"{self.public_base_url}/mcp"

    @classmethod
    def from_env(cls) -> Settings:
        base_url = (
            os.getenv("PUBLIC_BASE_URL")
            or os.getenv("RENDER_EXTERNAL_URL")
            or "http://127.0.0.1:8000"
        ).rstrip("/")
        owner_secret = os.getenv("OWNER_SECRET", "") or "local-development-only"
        signing_secret = (
            os.getenv("OAUTH_SIGNING_SECRET", "")
            or "local-development-signing-secret-change-me"
        )
        return cls(
            public_base_url=base_url,
            owner_secret=owner_secret,
            signing_secret=signing_secret,
            mi_token_json=os.getenv("MI_TOKEN_JSON", "").strip(),
        )
