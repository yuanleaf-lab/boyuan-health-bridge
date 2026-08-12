from __future__ import annotations

import json
from datetime import date
from typing import Any, Literal

from mi_fitness import AuthToken, MiHealthClient, XiaomiAuth

Metric = Literal[
    "heart_rate",
    "sleep",
    "steps",
    "calories",
    "spo2",
    "intensity",
    "valid_stand",
    "weight",
    "blood_pressure",
]


class HealthBridge:
    def __init__(self, token_json: str = ""):
        self._token_json = token_json
        self._client: MiHealthClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self._token_json)

    def set_token_json(self, token_json: str) -> None:
        AuthToken.model_validate_json(token_json)
        self._token_json = token_json
        self._client = None

    def safe_status(self) -> dict[str, Any]:
        user_id = ""
        if self._token_json:
            try:
                token = AuthToken.model_validate_json(self._token_json)
                user_id = token.user_id
            except ValueError:
                pass
        return {
            "ok": True,
            "configured": self.configured,
            "xiaomi_user_id_suffix": user_id[-4:] if user_id else None,
            "mode": "read_only_family_sharing",
        }

    def _get_client(self) -> MiHealthClient:
        if self._client:
            return self._client
        if not self._token_json:
            raise RuntimeError("尚未配置小米登录凭证，请先打开 /setup 完成登录")
        token = AuthToken.model_validate_json(self._token_json)
        auth = XiaomiAuth()
        auth.token = token
        self._client = MiHealthClient(auth)
        return self._client

    async def list_relatives(self) -> list[dict[str, Any]]:
        members = await self._get_client().get_relatives()
        return [member.model_dump(mode="json") for member in members]

    async def resolve_relative(self, relative: str | None) -> Any:
        members = await self._get_client().get_relatives()
        if relative:
            lowered = relative.strip().lower()
            for member in members:
                if str(member.relative_uid) == lowered or member.relative_note.strip().lower() == lowered:
                    return member
            partial = [member for member in members if lowered in member.relative_note.strip().lower()]
            if len(partial) == 1:
                return partial[0]
            raise ValueError(f"没有找到亲友：{relative}")
        if len(members) == 1:
            return members[0]
        if not members:
            raise ValueError("亲友列表为空，请先在小米运动健康里建立亲友共享")
        names = [member.relative_note or str(member.relative_uid) for member in members]
        raise ValueError(f"存在多位亲友，请指定其中一位：{', '.join(names)}")

    async def snapshot(self, relative: str | None) -> dict[str, Any]:
        client = self._get_client()
        member = await self.resolve_relative(relative)
        snapshot = await client.get_latest_data(member.relative_uid)
        summary = await client.get_latest_daily_summary(member.relative_uid)
        shared = await client.get_shared_data_types(member.relative_uid)
        return {
            "relative": member.model_dump(mode="json"),
            "shared_data_types": shared,
            "latest": snapshot.model_dump(mode="json"),
            "daily_summary": summary.model_dump(mode="json"),
            "notice": "数据来自小米运动健康最近一次亲友共享同步，不用于医疗诊断。",
        }

    async def history(
        self,
        relative: str | None,
        metric: Metric,
        query_date: str | None,
        days: int,
    ) -> dict[str, Any]:
        if not 1 <= days <= 30:
            raise ValueError("days 必须在 1 到 30 之间")
        parsed_date = date.fromisoformat(query_date) if query_date else None
        client = self._get_client()
        member = await self.resolve_relative(relative)
        method_names = {
            "heart_rate": "get_heart_rate",
            "sleep": "get_sleep",
            "steps": "get_steps",
            "calories": "get_calories_history",
            "spo2": "get_spo2_history",
            "intensity": "get_intensity_history",
            "valid_stand": "get_valid_stand_history",
            "weight": "get_weight_history",
            "blood_pressure": "get_blood_pressure_history",
        }
        method = getattr(client, method_names[metric])
        rows = await method(member.relative_uid, parsed_date, days=days)
        return {
            "relative": member.model_dump(mode="json"),
            "metric": metric,
            "query_date": parsed_date.isoformat() if parsed_date else None,
            "days": days,
            "data": [row.model_dump(mode="json") for row in rows],
            "notice": "数据来自小米运动健康亲友共享，不用于医疗诊断。",
        }

    def export_token_json(self) -> str:
        if not self._token_json:
            raise RuntimeError("token 尚未生成")
        parsed = json.loads(self._token_json)
        return json.dumps(parsed, ensure_ascii=False, indent=2)
