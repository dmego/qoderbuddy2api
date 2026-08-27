"""WorkBuddy/CodeBuddy upstream model discovery via chat-completions probing.

WorkBuddy has no public model-catalog endpoint (unlike Qoder's
``/api/v1/cloud/models``): ``/v2/chat/completions`` is the only fact source.
A valid model returns HTTP 200 with an SSE stream; an unknown id returns
HTTP 400 ``code=11102`` ("model service info not found"). Reasoning support
is probed by requesting with ``reasoning_effort=low``: when supported, the
first delta carries a non-empty ``reasoning_content``.

Sync updates ``config/models.json`` (the codebuddy section is the durable
fact source for this provider, loaded by the runtime snapshot) and then asks
the control plane to refresh provider pools so the worker picks up changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .models import Credential
from .registry import AccountRegistry
from .repository import AccountRepository
from .resolver import CredentialResolver

logger = logging.getLogger("qb2api.accounts.codebuddy_model_sync")

CHAT_ENDPOINT = "/v2/chat/completions"
MODEL_NOT_FOUND_CODE = "11102"
_PROBE_MESSAGE = "hi"
_PROBE_MAX_TOKENS = 8
_PROBE_CONCURRENCY = 5

# 探测候选：现有已知 ID 与按命名规律扩展的下版本候选。不做无限外推。
_EXTRA_CANDIDATES = (
    "glm-5.4",
    "glm-5.4-flash",
    "glm-5v2",
    "glm-5v",
    "deepseek-v3.1",
    "deepseek-v4.1",
    "deepseek-v4.1-flash",
    "kimi-k2.8",
    "kimi-k2.9",
    "kimi-k3",
    "minimax-m2.5",
    "minimax-m4",
    "hy4",
    "hy4-flash",
)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    model_id: str
    exists: bool
    reasoning: bool = False
    status_code: int = 0
    error_code: str | None = None


@dataclass(slots=True)
class SyncReport:
    added: int = 0
    updated: int = 0
    removed: int = 0
    probed: int = 0
    models: list[dict[str, Any]] = field(default_factory=list)


def _candidate_ids(models_config_path: str) -> list[str]:
    """已配置 + 内置候选，去重且保持稳定顺序。"""
    try:
        cfg = json.loads(Path(models_config_path).read_text(encoding="utf-8"))
        known = [m.get("id") for m in (cfg.get("codebuddy") or {}).get("models") or [] if isinstance(m, dict)]
    except Exception:
        known = []
    seen: set[str] = set()
    ordered: list[str] = []
    for model_id in [*known, *_EXTRA_CANDIDATES]:
        if model_id and model_id not in seen:
            seen.add(model_id)
            ordered.append(model_id)
    return ordered


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }


def _parse_reasoning_sse(text: str) -> bool:
    """扫描 SSE 文本，首个 delta 的 reasoning_content 非空即支持思考。"""
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            chunk = json.loads(line[5:].strip())
        except Exception:
            continue
        delta = ((chunk.get("choices") or [{}])[0]).get("delta") or {}
        if delta.get("reasoning_content"):
            return True
    return False


async def _probe_model(
    client: httpx.AsyncClient,
    access_token: str,
    model_id: str,
) -> ProbeResult:
    """探测存在性与思考能力：先带 effort=low，未检出再补一次裸探测。"""
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": _PROBE_MESSAGE}],
        "stream": True,
        "max_tokens": _PROBE_MAX_TOKENS,
        "reasoning_effort": "low",
    }
    try:
        response = await client.post(CHAT_ENDPOINT, headers=_headers(access_token), json=body)
    except httpx.HTTPError:
        return ProbeResult(model_id=model_id, exists=False)
    if response.status_code != 200:
        error_code = None
        try:
            payload = response.json()
            code = payload.get("code")
            if code is not None:
                error_code = str(code)
        except Exception:
            pass
        return ProbeResult(
            model_id=model_id,
            exists=False,
            status_code=response.status_code,
            error_code=error_code,
        )
    reasoning = _parse_reasoning_sse(response.text)
    # 部分模型默认产思考但忽略/拒绝 reasoning_effort，补一次裸探测。
    if not reasoning:
        try:
            plain_body = {k: v for k, v in body.items() if k != "reasoning_effort"}
            second = await client.post(CHAT_ENDPOINT, headers=_headers(access_token), json=plain_body)
            if second.status_code == 200:
                reasoning = _parse_reasoning_sse(second.text)
        except httpx.HTTPError:
            pass
    return ProbeResult(
        model_id=model_id,
        exists=True,
        reasoning=reasoning,
        status_code=200,
    )


def _upsert_config(models_config: str, results: list[ProbeResult]) -> tuple[int, int, int]:
    """把探测结果写回 models.json 的 codebuddy 段；返回 (added, updated, removed)。"""
    path = Path(models_config)
    cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    codebuddy = cfg.setdefault("codebuddy", {})
    models = codebuddy.setdefault("models", [])

    by_id = {m.get("id"): m for m in models if isinstance(m, dict) and m.get("id")}
    added = updated = removed = 0
    for result in results:
        if not result.exists:
            # 明确 400/11102（model not found）才移除；网络错误保留。
            if result.error_code == MODEL_NOT_FOUND_CODE and result.model_id in by_id:
                del by_id[result.model_id]
                removed += 1
            continue
        caps: dict[str, bool] = {"chat": True, "streaming": True}
        if result.reasoning:
            caps["reasoning"] = True
            caps["reasoning_effort"] = True
        existing = by_id.get(result.model_id)
        if existing is None:
            by_id[result.model_id] = {
                "id": result.model_id,
                "name": result.model_id,
                "capabilities": caps,
            }
            added += 1
            continue
        prev = existing.get("capabilities") or {}
        # 探测 False 不降级：max_tokens 截断等会使思考能力漏报，已有标注保持。
        if (prev.get("reasoning") or prev.get("reasoning_effort")) and not result.reasoning:
            continue
        if prev.get("reasoning") != caps.get("reasoning") or prev.get("reasoning_effort") != caps.get(
            "reasoning_effort"
        ):
            existing["capabilities"] = caps
            updated += 1
    # 稳定顺序：保留原有顺序 + 新增排尾，避免配置抖动
    ordered = [by_id[m] for m in by_id]
    models[:] = ordered
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return added, updated, removed


async def _pick_codebuddy_token(registry: AccountRegistry, resolver: CredentialResolver) -> str | None:

    slots = [slot for slot in registry.snapshot("chat") if slot.provider == "codebuddy"]
    ordered = [slot for slot in slots if slot.verification_status == "verified"]
    ordered.extend(slot for slot in slots if slot.verification_status != "verified")
    for slot in ordered:
        for purpose in ("checkin", "chat"):
            try:
                credential: Credential = await resolver.credential(slot.provider, slot.account_id, purpose)
            except LookupError:
                continue
            token = credential.payload.get("access_token") or credential.payload.get("token")
            if isinstance(token, str) and token.strip():
                return token
    return None


async def sync_codebuddy_models(
    repository: AccountRepository,
    registry: AccountRegistry,
    resolver: CredentialResolver,
    *,
    models_config_path: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> SyncReport:
    """探测 workbuddy 候选模型并写回 models.json 的 codebuddy 段。"""
    from qb2api.config import Settings

    settings = Settings.from_env()
    token = await _pick_codebuddy_token(registry, resolver)
    if token is None:
        raise RuntimeError("No available codebuddy account credential")

    config_path = models_config_path or settings.model_config_path
    candidates = _candidate_ids(config_path)
    report = SyncReport()
    results: list[ProbeResult] = []
    own_client = client is None
    probe_client = client or httpx.AsyncClient(
        base_url=settings.codebuddy_endpoint,
        timeout=30.0,
        trust_env=False,
    )
    try:
        semaphore = asyncio.Semaphore(_PROBE_CONCURRENCY)

        async def probe_one(model_id: str) -> ProbeResult:
            async with semaphore:
                return await _probe_model(probe_client, token, model_id)

        results = await asyncio.gather(*(probe_one(m) for m in candidates))
        report.probed = len(results)
    finally:
        if own_client:
            await probe_client.aclose()

    report.added, report.updated, report.removed = _upsert_config(config_path, results)
    report.models = [{"model_id": r.model_id, "exists": r.exists, "reasoning": r.reasoning} for r in results]
    return report
