from pydantic import BaseModel

from app.health import HealthBridge


class Member(BaseModel):
    relative_uid: int
    relative_note: str
    latest_data_time: int = 0


class Row(BaseModel):
    value: int


class FakeClient:
    async def get_relatives(self):
        return [Member(relative_uid=42, relative_note="小叶")]

    async def get_steps(self, relative_uid, query_date, *, days):
        assert relative_uid == 42
        assert days == 3
        return [Row(value=8000)]


async def test_resolve_single_relative_and_history() -> None:
    bridge = HealthBridge("synthetic")
    bridge._client = FakeClient()
    result = await bridge.history(None, "steps", "2026-08-12", 3)
    assert result["relative"]["relative_note"] == "小叶"
    assert result["data"] == [{"value": 8000}]


async def test_status_never_exposes_token() -> None:
    bridge = HealthBridge()
    status = bridge.safe_status()
    assert status["configured"] is False
    assert "token" not in status
