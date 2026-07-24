"""Qoder CN provider — COSY protocol direct HTTP (no CLI subprocess)."""

import base64
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from email.utils import formatdate

import httpx
from cryptography.hazmat.primitives import padding as sym_pad
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_pad
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..openai import ChatCompletionRequest, stream_chunk, stream_done
from .base import Provider

logger = logging.getLogger("qb2api")


class QoderError(Exception):
    """Qoder upstream error."""
    def __init__(self, message: str, status_code: int = 502):
        self.status_code = status_code
        super().__init__(message)


# ── COSY constants ──
_RSA_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc\n"
    "4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l\n"
    "6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17\n"
    "XcW+ML9FoCI6AOvOzwIDAQAB\n"
    "-----END PUBLIC KEY-----"
)
_SECRET = "d2FyLCB3YXIgbmV2ZXIgY2hhbmdlcw=="
_GATEWAY = "https://gateway.qoder.com.cn"
_CHAT_PATH = "/algo/api/v2/service/pro/sse/agent_chat_generation"
_CHAT_QUERY = "FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
_SIGN_PATH = "/api/v2/service/pro/sse/agent_chat_generation"

_STD = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_CUS = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"
_S2C = str.maketrans(_STD + "=", _CUS + "$")

EFFORT_SUFFIX_RE = re.compile(r"^(.*)-effort-(low|medium|high|max)$")

QODER_CLI_MODEL_KEYS = {
    # Verified with `qoderclicn --print --output-format json --model ...` and
    # the returned `modelUsage` key from qoderclicn 1.1.2.
    "auto": "auto",
    "Auto": "auto",
    "Qwen3.8-Max-Preview": "qmodel_preview",
    "Qwen3.7-Max": "qmodel_latest",
    "Qwen3.7-Plus": "qmodel",
    "Qwen3.6-Flash": "q36fmodel",
    "DeepSeek-V4-Pro": "dmodel",
    "DeepSeek-V4-Flash": "dfmodel",
    "GLM-5.2": "gm51model",
    "Kimi-K2.7-Code": "kmodel",
    "MiniMax-M2.7": "mmodel",
}


def _qoder_model_key(model: str) -> str:
    """Return the COSY internal model key used by qoderclicn."""
    return QODER_CLI_MODEL_KEYS.get(model, model)


def _qoder_encode(data: bytes) -> str:
    b64 = base64.b64encode(data).decode("ascii")
    n = len(b64)
    a = n // 3
    return (b64[n - a :] + b64[a : n - a] + b64[:a]).translate(_S2C)


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _rsa_encrypt(data: bytes) -> bytes:
    pub = serialization.load_pem_public_key(_RSA_PEM.encode())
    return pub.encrypt(data, asym_pad.PKCS1v15())


def _aes_encrypt(plain: bytes, key: bytes) -> bytes:
    padder = sym_pad.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    return enc.update(padded) + enc.finalize()


# ── QoderSession ──
class QoderSession:
    def __init__(self, pat: str):
        self.pat = pat
        self._client = httpx.AsyncClient(timeout=30)
        self.machine_id = str(uuid.uuid4())
        self.machine_token = base64.urlsafe_b64encode(
            (str(uuid.uuid4()) + str(uuid.uuid4())).encode()
        )[:50].decode()
        self.machine_type = uuid.uuid4().hex[:18]
        self.user_id: str = ""
        self.cosy_key: str = ""
        self.payload_b64: str = ""
        self._ready = False

    async def authenticate(self):
        now = formatdate(timeval=time.time(), usegmt=True)
        sig = _md5(f"cosy&{_SECRET}&{now}")
        inner = json.dumps({
            "personalToken": self.pat,
            "securityOauthToken": "",
            "refreshToken": "",
            "needRefresh": False,
            "authInfo": {},
        }, ensure_ascii=False)
        outer = {"payload": inner, "encodeVersion": "1"}
        body = _qoder_encode(json.dumps(outer).encode())

        resp = await self._client.post(
            f"{_GATEWAY}/algo/api/v3/user/jobToken?Encode=1",
            content=body,
            headers={
                "cosy-machinetoken": self.machine_token,
                "cosy-machinetype": self.machine_type,
                "login-version": "v2", "appcode": "cosy",
                "accept": "application/json", "accept-encoding": "identity",
                "cosy-version": "0.1.43", "cosy-clienttype": "5",
                "date": now, "signature": sig,
                "content-type": "application/json",
                "cosy-machineid": self.machine_id,
                "user-agent": "Go-http-client/2.0",
            },
        )
        resp.raise_for_status()
        job = resp.json()
        self.user_id = job["id"]
        logger.info(f"Qoder: authenticated as {job.get('name', '?')} ({self.user_id[:20]}...)")

        # nnapi identity format: yx_uid is empty
        identity = {
            "name": str(job.get("name", "")),
            "aid": str(job["id"]),
            "uid": str(job["id"]),
            "yx_uid": "",
            "organization_id": "", "organization_name": "",
            "user_type": str(job.get("userType", "personal_standard")),
            "security_oauth_token": str(job.get("securityOauthToken", "")),
            "refresh_token": str(job.get("refreshToken", "")),
        }
        tk = uuid.uuid4().hex[:16].encode("ascii")
        self.cosy_key = base64.b64encode(_rsa_encrypt(tk)).decode("ascii")
        info = base64.b64encode(
            _aes_encrypt(json.dumps(identity, ensure_ascii=False).encode(), tk)
        ).decode("ascii")
        payload = json.dumps(
            {"cosyVersion": "0.1.43", "ideVersion": "", "info": info,
             "requestId": str(uuid.uuid4()), "version": "v1"},
            sort_keys=True,
        )
        self.payload_b64 = base64.b64encode(payload.encode()).decode("ascii")
        self._ready = True

    def chat_headers(self, encoded_body: str, model: str) -> dict:
        date = str(int(time.time()))
        signature = _md5(
            "\n".join([self.payload_b64, self.cosy_key, date, encoded_body, _SIGN_PATH])
        )
        return {
            "cosy-data-policy": "AGREE", "content-type": "application/json",
            "cosy-machinetype": self.machine_type, "cosy-clienttype": "5",
            "cosy-date": date, "cosy-user": self.user_id,
            "cosy-key": self.cosy_key, "cache-control": "no-cache",
            "accept": "text/event-stream", "cosy-clientip": "169.254.198.161",
            "authorization": f"Bearer COSY.{self.payload_b64}.{signature}",
            "accept-encoding": "identity", "cosy-version": "0.1.43",
            "cosy-machineid": self.machine_id, "cosy-machinetoken": self.machine_token,
            "login-version": "v2", "user-agent": "Go-http-client/2.0",
            "x-model-key": model, "x-model-source": "system",
        }

    async def close(self):
        await self._client.aclose()


# ── QoderProvider ──
class QoderProvider(Provider):
    name = "qoder"

    def __init__(self, pat: str, timeout: int = 300, **kwargs):
        self.pat = pat
        self.timeout = timeout
        self._session: QoderSession | None = None

    async def _ensure_session(self) -> QoderSession:
        if self._session is None or not self._session._ready:
            self._session = QoderSession(self.pat)
            await self._session.authenticate()
        return self._session

    async def complete(self, request: ChatCompletionRequest) -> dict:
        from ..sse import StreamAggregator
        aggregator = StreamAggregator(model=request.model)
        async for chunk in self.stream(request):
            if chunk.startswith(b"data: [DONE]"):
                break
            try:
                obj = json.loads(chunk[6:].decode().strip())
                if obj:
                    aggregator.process(obj)
            except Exception:
                pass
        request.observe_usage(aggregator.usage)
        return aggregator.response()

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        session = await self._ensure_session()
        model = request.model
        upstream_model = _qoder_model_key(model)

        # Build payload and encode
        payload = self._build_payload(request, model)
        encoded_body = _qoder_encode(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        headers = session.chat_headers(encoded_body, upstream_model)
        url = f"{_GATEWAY}{_CHAT_PATH}?{_CHAT_QUERY}"

        logger.info(f"Qoder: POST {url[:60]}... body={len(encoded_body)}B model={model} upstream={upstream_model}")

        client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=15))
        try:
            async with client.stream("POST", url, content=encoded_body.encode("utf-8"), headers=headers) as resp:
                if resp.status_code != 200:
                    err = (await resp.aread()).decode("utf-8", errors="replace")
                    raise QoderError(f"HTTP {resp.status_code}: {err[:300]}", status_code=resp.status_code)

                chunk_count = 0
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    try:
                        outer = json.loads(data_str, strict=False)
                        inner = json.loads(outer["body"], strict=False) if isinstance(outer.get("body"), str) else outer
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue

                    choices = inner.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    finish = choices[0].get("finish_reason")

                    out = {}
                    if delta.get("role"):
                        out["role"] = delta["role"]
                    if delta.get("content"):
                        out["content"] = delta["content"]
                    if delta.get("reasoning_content"):
                        out["reasoning_content"] = delta["reasoning_content"]
                    if delta.get("tool_calls"):
                        from ..sse import inject_tool_call_index, normalize_tool_call_id
                        delta["tool_calls"] = inject_tool_call_index(delta["tool_calls"])
                        delta["tool_calls"] = [normalize_tool_call_id(tc) for tc in delta["tool_calls"]]
                        out["tool_calls"] = delta["tool_calls"]

                    if out or finish:
                        chunk_count += 1
                        yield stream_chunk(model, out, finish_reason=finish)

            logger.info(f"Qoder: stream done, {chunk_count} chunks")

        finally:
            await client.aclose()

        yield stream_done()

    def _build_payload(self, request: ChatCompletionRequest, model: str) -> dict:
        """Build Qoder COSY request body. Named models use qoderclicn internal keys."""
        upstream_model = _qoder_model_key(model)
        user_text = ""
        qoder_msgs = []
        for msg in request.messages:
            content = msg.content or ""
            if isinstance(content, list):
                # Multimodal content blocks → extract text
                parts = []
                for c in content:
                    if isinstance(c, dict):
                        if c.get("type") == "text":
                            parts.append(str(c.get("text", "")))
                        elif c.get("type") == "image_url":
                            parts.append("[image]")
                    else:
                        parts.append(str(c))
                content = " ".join(parts)
            content = str(content)
            if msg.role == "system":
                qoder_msgs.append({
                    "role": "system", "content": content,
                    "response_meta": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                })
                continue
            if msg.role == "user":
                user_text = content
                qoder_msgs.append({
                    "role": "user", "content": "", "contents": [{"type": "text", "text": content}],
                    "response_meta": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                })
                continue
            if msg.role == "assistant":
                entry: dict = {
                    "role": "assistant", "content": content,
                    "response_meta": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                }
                if msg.tool_calls:
                    entry["tool_calls"] = msg.tool_calls
                qoder_msgs.append(entry)
                continue
            if msg.role == "tool":
                qoder_msgs.append({
                    "role": "tool", "content": content,
                    "tool_call_id": msg.tool_call_id,
                    "response_meta": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                })
                continue
            qoder_msgs.append({
                "role": msg.role, "content": content,
                "response_meta": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            })

        uid = str(uuid.uuid4())
        payload = {
            "request_id": uid, "chat_record_id": uid,
            "request_set_id": str(uuid.uuid4()), "session_id": str(uuid.uuid4()),
            "stream": True,
            "model_config": {"key": upstream_model, "source": "system"},
            "chat_context": {
                "text": {"text": user_text},
                "extra": {"originalContent": {"text": user_text}},
            },
            "messages": qoder_msgs,
            "source": 1, "version": "3",
        }

        # Tool call support
        if request.tools:
            payload["tools"] = [t.model_dump() for t in request.tools]
            if request.tool_choice:
                payload["tool_choice"] = request.tool_choice

        return payload

    async def close(self):
        if self._session:
            await self._session.close()
