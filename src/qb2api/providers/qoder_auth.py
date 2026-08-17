"""Per-account Qoder COSY authentication state and cryptographic headers."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
import uuid
from email.utils import formatdate

import httpx
from cryptography.hazmat.primitives import padding as sym_pad
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_pad
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .qoder_payload import qoder_encode

logger = logging.getLogger("qb2api")

GATEWAY = "https://gateway.qoder.com.cn"
CHAT_PATH = "/algo/api/v2/service/pro/sse/agent_chat_generation"
CHAT_QUERY = "FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
_SIGN_PATH = "/api/v2/service/pro/sse/agent_chat_generation"
_SECRET = "d2FyLCB3YXIgbmV2ZXIgY2hhbmdlcw=="
_RSA_PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc\n"
    "4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l\n"
    "6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17\n"
    "XcW+ML9FoCI6AOvOzwIDAQAB\n"
    "-----END PUBLIC KEY-----"
)


class QoderError(Exception):
    """Qoder upstream error with an HTTP-compatible status code."""

    def __init__(self, message: str, status_code: int = 502):
        self.status_code = status_code
        super().__init__(message)


class QoderSession:
    """COSY state owned by exactly one Qoder provider/account instance."""

    def __init__(self, pat: str):
        self.pat = pat
        self._client = httpx.AsyncClient(timeout=30, trust_env=False)
        self.machine_id = str(uuid.uuid4())
        self.machine_token = base64.urlsafe_b64encode(
            (str(uuid.uuid4()) + str(uuid.uuid4())).encode()
        )[:50].decode()
        self.machine_type = uuid.uuid4().hex[:18]
        self.user_id = ""
        self.cosy_key = ""
        self.payload_b64 = ""
        # 认证响应中带回的 OAuth 令牌，签到可据此派生
        self.security_oauth_token = ""
        self.refresh_token = ""
        self.authenticated_at: float | None = None
        self.last_success_at: float | None = None
        self.invalid_reason: str | None = None
        self._ready = False

    async def authenticate(self) -> None:
        date = formatdate(timeval=time.time(), usegmt=True)
        response = await self._client.post(
            f"{GATEWAY}/algo/api/v3/user/jobToken?Encode=1",
            content=_authentication_body(self.pat),
            headers=_authentication_headers(self, date),
        )
        if not 200 <= response.status_code < 300:
            raise QoderError(
                f"Qoder authentication failed (HTTP {response.status_code})",
                status_code=response.status_code,
            )
        try:
            job = response.json()
            self.user_id = str(job["id"])
            identity = _identity(job)
        except (KeyError, TypeError, ValueError) as error:
            raise QoderError("Qoder authentication response was invalid") from error
        # jobToken 响应中带回 securityOauthToken + refreshToken，
        # 签到端点 (openapi.qoder.com.cn) 可据此派生 access_token
        self.security_oauth_token = str(job.get("securityOauthToken", ""))
        self.refresh_token = str(job.get("refreshToken", ""))
        key = uuid.uuid4().hex[:16].encode("ascii")
        self.cosy_key = base64.b64encode(_rsa_encrypt(key)).decode("ascii")
        encrypted = _aes_encrypt(
            json.dumps(identity, ensure_ascii=False).encode(),
            key,
        )
        info = base64.b64encode(encrypted).decode("ascii")
        payload = json.dumps(
            {
                "cosyVersion": "0.1.43",
                "ideVersion": "",
                "info": info,
                "requestId": str(uuid.uuid4()),
                "version": "v1",
            },
            sort_keys=True,
        )
        self.payload_b64 = base64.b64encode(payload.encode()).decode("ascii")
        self.authenticated_at = time.time()
        self.invalid_reason = None
        self._ready = True
        logger.info("Qoder: account session authenticated")

    def chat_headers(self, encoded_body: str, model: str) -> dict[str, str]:
        if not self._ready:
            raise QoderError("Qoder session is not authenticated", status_code=401)
        date = str(int(time.time()))
        signature = _md5(
            "\n".join(
                [self.payload_b64, self.cosy_key, date, encoded_body, _SIGN_PATH]
            )
        )
        return {
            "cosy-data-policy": "AGREE",
            "content-type": "application/json",
            "cosy-machinetype": self.machine_type,
            "cosy-clienttype": "5",
            "cosy-date": date,
            "cosy-user": self.user_id,
            "cosy-key": self.cosy_key,
            "cache-control": "no-cache",
            "accept": "text/event-stream",
            "cosy-clientip": "169.254.198.161",
            "authorization": f"Bearer COSY.{self.payload_b64}.{signature}",
            "accept-encoding": "identity",
            "cosy-version": "0.1.43",
            "cosy-machineid": self.machine_id,
            "cosy-machinetoken": self.machine_token,
            "login-version": "v2",
            "user-agent": "Go-http-client/2.0",
            "x-model-key": model,
            "x-model-source": "system",
        }

    def mark_success(self) -> None:
        self.last_success_at = time.time()

    def invalidate(self, reason: str) -> None:
        self._ready = False
        self.invalid_reason = reason

    async def close(self) -> None:
        await self._client.aclose()


def _authentication_body(pat: str) -> str:
    inner = json.dumps(
        {
            "personalToken": pat,
            "securityOauthToken": "",
            "refreshToken": "",
            "needRefresh": False,
            "authInfo": {},
        },
        ensure_ascii=False,
    )
    return qoder_encode(json.dumps({"payload": inner, "encodeVersion": "1"}).encode())


def _authentication_headers(session: QoderSession, date: str) -> dict[str, str]:
    return {
        "cosy-machinetoken": session.machine_token,
        "cosy-machinetype": session.machine_type,
        "login-version": "v2",
        "appcode": "cosy",
        "accept": "application/json",
        "accept-encoding": "identity",
        "cosy-version": "0.1.43",
        "cosy-clienttype": "5",
        "date": date,
        "signature": _md5(f"cosy&{_SECRET}&{date}"),
        "content-type": "application/json",
        "cosy-machineid": session.machine_id,
        "user-agent": "Go-http-client/2.0",
    }


def _identity(job: dict[str, object]) -> dict[str, str]:
    user_id = str(job["id"])
    return {
        "name": str(job.get("name", "")),
        "aid": user_id,
        "uid": user_id,
        "yx_uid": "",
        "organization_id": "",
        "organization_name": "",
        "user_type": str(job.get("userType", "personal_standard")),
        "security_oauth_token": str(job.get("securityOauthToken", "")),
        "refresh_token": str(job.get("refreshToken", "")),
    }


def _md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _rsa_encrypt(data: bytes) -> bytes:
    key = serialization.load_pem_public_key(_RSA_PEM.encode())
    return key.encrypt(data, asym_pad.PKCS1v15())


def _aes_encrypt(plain: bytes, key: bytes) -> bytes:
    padder = sym_pad.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()
