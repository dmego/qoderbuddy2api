"""Repository methods for the Worker model catalog."""

from __future__ import annotations

import json
from typing import Any

from .schema import now_iso


class CatalogRepositoryMixin:
    async def upsert_model(
        self,
        *,
        provider: str,
        model_id: str,
        display_name: str = "",
        capabilities: list[str] | None = None,
        source: str = "provider",
        enabled: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        async with self._operation(write=True) as db:
            await db.execute(
                """
                INSERT INTO model_catalog
                    (provider, model_id, display_name, capabilities_json, source,
                     enabled, last_seen_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, model_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    capabilities_json=excluded.capabilities_json,
                    source=excluded.source,
                    enabled=excluded.enabled,
                    last_seen_at=excluded.last_seen_at,
                    metadata_json=excluded.metadata_json
                """,
                (provider, model_id, display_name, json.dumps(capabilities or [], ensure_ascii=False),
                 source, int(enabled), now_iso(), json.dumps(metadata or {}, ensure_ascii=False)),
            )

    async def list_models(self, provider: str | None = None, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if provider:
            clauses.append("provider=?")
            params.append(provider)
        if enabled_only:
            clauses.append("enabled=1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self._operation() as db:
            cursor = await db.execute(
                f"SELECT * FROM model_catalog {where} ORDER BY provider, model_id", params
            )
            rows = await cursor.fetchall()
        return [self._model_row(row) for row in rows]

    async def set_model_enabled(self, provider: str, model_id: str, enabled: bool) -> bool:
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                "UPDATE model_catalog SET enabled=?, last_seen_at=? WHERE provider=? AND model_id=?",
                (int(enabled), now_iso(), provider, model_id),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _model_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["enabled"] = bool(result.get("enabled"))
        for field, default in (("capabilities_json", []), ("metadata_json", {})):
            raw = result.pop(field, None)
            try:
                result[field.removesuffix("_json")] = json.loads(raw) if raw else default
            except json.JSONDecodeError:
                result[field.removesuffix("_json")] = default
        return result
