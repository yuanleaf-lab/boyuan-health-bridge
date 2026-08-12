import pytest
from starlette.testclient import TestClient

from app.server import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_public_status_and_oauth_metadata(client: TestClient) -> None:
    status = client.get("/health")
    assert status.status_code == 200
    assert status.json()["mode"] == "read_only_family_sharing"

    metadata = client.get("/.well-known/oauth-authorization-server")
    assert metadata.status_code == 200
    assert metadata.json()["client_id_metadata_document_supported"] is True
    assert metadata.json()["token_endpoint_auth_methods_supported"] == ["none"]


def test_mcp_requires_bearer_token(client: TestClient) -> None:
    response = client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]
