"""Qoder upstream model discovery and catalog sync service tests."""

from unittest.mock import AsyncMock, Mock

import pytest

from qb2api.accounts.qoder_model_sync import (
    MODELS_ENDPOINT,
    SyncReport,
    UpstreamModel,
    convert_upstream_models,
    fetch_qoder_models,
    sync_qoder_models,
)
from qb2api.providers.qoder_auth import QoderError

SAMPLE = {
    "id": "qmodel_38max",
    "display_name": "Qwen3.8-Max",
    "is_enabled": True,
    "is_new": True,
    "is_vl": False,
    "support_disable_reasoning": True,
    "price_factor": 1.0,
    "max_input_tokens": 131072,
    "default_context_window": 131072,
    "available_context_windows": [131072, 262144],
    "efforts": ["low", "high"],
    "default_effort": "high",
}


def test_convert_upstream_model():
    row = convert_upstream_models([UpstreamModel.from_dict(SAMPLE)])[0]
    assert row["model_id"] == "Qwen3.8-Max"
    assert row["enabled"] is True
    assert row["capabilities"] == ["chat", "streaming", "reasoning", "reasoning_effort", "context_window"]
    assert row["metadata"]["cosy_key"] == "qmodel_38max"
    assert row["metadata"]["is_new"] is True
    assert row["metadata"]["default_context_window"] == 131072
    assert row["metadata"]["available_context_windows"] == [131072, 262144]
    assert row["metadata"]["efforts"] == ["low", "high"]


def test_convert_disabled_model():
    item = UpstreamModel.from_dict({**SAMPLE, "is_enabled": False})
    row = convert_upstream_models([item])[0]
    assert row["enabled"] is False


def test_from_dict_missing_optional():
    item = UpstreamModel.from_dict(
        {"id": "qmodel_min", "display_name": "Qwen3-Mini", "is_enabled": True}
    )
    assert item.is_new is False
    assert item.is_vl is False
    assert item.support_disable_reasoning is False
    assert item.price_factor == 1.0
    assert item.max_input_tokens == 0
    assert item.default_context_window == 0
    assert item.available_context_windows == []
    assert item.efforts == []
    assert item.default_effort == ""


async def test_fetch_qoder_models_success():
    response = Mock(status_code=200, json=lambda: {"data": [SAMPLE]})
    client = Mock(get=AsyncMock(return_value=response))
    result = await fetch_qoder_models("pat-123", client=client)
    assert len(result) == 1
    assert result[0].id == "qmodel_38max"
    client.get.assert_awaited_once()
    args, kwargs = client.get.call_args
    assert args[0] == MODELS_ENDPOINT
    assert kwargs["headers"]["Authorization"] == "Bearer pat-123"


async def test_fetch_qoder_models_http_error():
    response = Mock(status_code=401)
    client = Mock(get=AsyncMock(return_value=response))
    with pytest.raises(QoderError) as exc:
        await fetch_qoder_models("pat", client=client)
    assert exc.value.status_code == 401


async def test_sync_qoder_models_upsert_and_disable():
    # Baseline: one stale upstream record (old display name, to be disabled)
    # plus one existing record that the sync will update (content differs).
    old_catalog = [
        {
            "provider": "qoder",
            "model_id": "Qwen3.8-Old",
            "display_name": "Qwen3.8-Old",
            "capabilities": ["chat"],
            "source": "upstream",
            "enabled": True,
            "metadata": {"cosy_key": "qmodel_old", "source": "upstream"},
        },
        {
            "provider": "qoder",
            "model_id": "Qwen3.8-Max",
            "display_name": "Qwen3.8-Max",
            "capabilities": ["chat"],
            "source": "upstream",
            "enabled": True,
            "metadata": {"cosy_key": "qmodel_38max", "is_new": False, "source": "upstream"},
        },
    ]
    new_model = {
        "id": "qmodel_plus",
        "display_name": "Qwen3.8-Plus",
        "is_enabled": True,
        "is_new": True,
        "is_vl": False,
        "support_disable_reasoning": False,
        "price_factor": 1.0,
        "max_input_tokens": 65536,
        "default_context_window": 65536,
        "available_context_windows": [65536],
        "efforts": ["low"],
        "default_effort": "low",
    }
    # Upstream returns the SAMPLE model (modified vs its baseline record -> updated)
    # plus one NEW model (not in baseline -> added).
    response = Mock(status_code=200, json=lambda: {"data": [SAMPLE, new_model]})
    client = Mock(get=AsyncMock(return_value=response))

    upserts: list[dict] = []

    async def record_upsert(**kwargs):
        upserts.append(kwargs)

    repo = Mock()
    repo.transaction.return_value = AsyncMock()
    repo.list_models = AsyncMock(return_value=old_catalog)
    repo.upsert_model = AsyncMock(side_effect=record_upsert)
    repo.set_model_enabled = AsyncMock(return_value=True)

    registry = Mock()
    registry.snapshot.return_value = [
        Mock(provider="qoder", account_id="q-verified", verification_status="verified"),
        Mock(provider="qoder", account_id="q-pending", verification_status="pending"),
    ]

    resolver = Mock()
    resolver.credential = AsyncMock(
        side_effect=lambda provider, account_id, purpose: Mock(payload={"pat": "pat-1"})
    )

    report = await sync_qoder_models(repo, registry, resolver, client=client)

    assert isinstance(report, SyncReport)
    assert report.added == 1
    assert report.updated == 1
    assert report.disabled == 1
    assert [row["model_id"] for row in report.models] == ["Qwen3.8-Max", "Qwen3.8-Plus"]
    assert [call.kwargs["model_id"] for call in repo.upsert_model.await_args_list] == [
        "Qwen3.8-Max",
        "Qwen3.8-Plus",
    ]
    repo.set_model_enabled.assert_awaited_once_with("qoder", "Qwen3.8-Old", False)


async def test_sync_qoder_models_no_credential_raises():
    registry = Mock()
    registry.snapshot.return_value = [
        Mock(provider="qoder", account_id="q-1", verification_status="verified")
    ]
    resolver = Mock()
    resolver.credential = AsyncMock(side_effect=LookupError("credential gone"))

    with pytest.raises(QoderError) as exc:
        await sync_qoder_models(Mock(), registry, resolver)
    assert exc.value.status_code == 409
