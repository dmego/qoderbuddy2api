"""Runtime Qoder model-key mapping contracts."""

from qb2api.config import Settings
from qb2api.models import ModelDefinition
from qb2api.providers.qoder_payload import (
    clear_runtime_model_keys,
    qoder_model_key,
    set_runtime_model_keys,
)
from qb2api.worker.proxy_state import ProxyState


def test_runtime_key_overrides_static():
    set_runtime_model_keys({"Qwen3.8-Max": "qmodel_38max"})
    try:
        assert qoder_model_key("Qwen3.8-Max") == "qmodel_38max"
        assert qoder_model_key("Qwen3.7-Max") == "qmodel_latest"  # 静态表兜底
        assert qoder_model_key("Unknown-New") == "Unknown-New"     # 无映射时原样返回
    finally:
        clear_runtime_model_keys()


def test_clear_runtime_keys_restores_static_table():
    set_runtime_model_keys({"Qwen3.7-Max": "qmodel_override"})
    try:
        assert qoder_model_key("Qwen3.7-Max") == "qmodel_override"
    finally:
        clear_runtime_model_keys()
    assert qoder_model_key("Qwen3.7-Max") == "qmodel_latest"


def test_set_runtime_keys_replaces_previous_mapping():
    set_runtime_model_keys({"Qwen3.8-Max": "qmodel_38max"})
    try:
        set_runtime_model_keys({"Qwen3.8-Max": "qmodel_new"})
        assert qoder_model_key("Qwen3.8-Max") == "qmodel_new"
    finally:
        clear_runtime_model_keys()


def test_proxy_state_syncs_cosy_keys_into_runtime_mapping():
    state = ProxyState(Settings())
    state.model_definitions = {
        "qoder": [
            ModelDefinition(
                "Qwen3.8-Max",
                "Qwen3.8-Max",
                "qoder",
                metadata={"cosy_key": "qmodel_38max"},
            ),
            ModelDefinition("Legacy-Model", "Legacy Model", "qoder"),
        ]
    }
    try:
        state._sync_runtime_model_keys()
        assert qoder_model_key("Qwen3.8-Max") == "qmodel_38max"
        assert qoder_model_key("Legacy-Model") == "Legacy-Model"  # 无 cosy_key 不映射
    finally:
        clear_runtime_model_keys()
