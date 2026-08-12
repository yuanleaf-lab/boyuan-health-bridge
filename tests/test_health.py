import pytest
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


class RawShapeClient:
    async def get_relatives(self):
        return []

    async def _request(self, method, path):
        assert method == "GET"
        return {
            "code": 0,
            "result": {
                "data": {
                    "relativeList": [
                        {
                            "userId": "42",
                            "nickname": "小叶",
                            "latestDataTime": 123,
                            "latestAbnormalRecordTime": "",
                            "sourceTag": {},
                        }
                    ]
                }
            },
        }

    async def get_family_members(self):
        raise AssertionError("raw relative fallback should have resolved the member")


class UnknownShapeClient:
    async def get_relatives(self):
        return []

    async def _request(self, method, path):
        return {"code": 0, "result": {"mystery": [{"opaque": "secret-value"}]}}

    async def get_family_members(self):
        return []


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


async def test_falls_back_to_changed_xiaomi_response_shape() -> None:
    bridge = HealthBridge("synthetic")
    bridge._client = RawShapeClient()

    members = await bridge.list_relatives()

    assert members == [
        {
            "relative_uid": 42,
            "relative_note": "小叶",
            "relative_icon": "",
            "latest_data_time": 123,
            "latest_abnormal_record_time": 0,
            "source_tag": 0,
        }
    ]


async def test_unknown_shape_returns_safe_diagnostic_only() -> None:
    bridge = HealthBridge("synthetic")
    bridge._client = UnknownShapeClient()

    with pytest.raises(RuntimeError) as exc_info:
        await bridge.list_relatives()

    message = str(exc_info.value)
    assert "mystery" in message
    assert "opaque" in message
    assert "secret-value" not in message
