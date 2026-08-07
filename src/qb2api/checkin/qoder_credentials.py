"""Qoder check-in credential derivation from a chat PAT."""

from __future__ import annotations

import logging
from collections.abc import Callable

from qb2api.providers.qoder_auth import QoderError, QoderSession

logger = logging.getLogger("qb2api.checkin.qoder_credentials")


async def derive_qoder_checkin(
    pat: str,
    *,
    session_factory: Callable[[str], QoderSession] = QoderSession,
) -> tuple[str, str] | None:
    session = session_factory(pat)
    try:
        await session.authenticate()
    except QoderError as error:
        logger.warning("qoder check-in derivation failed (http=%s)", error.status_code)
        return None
    except Exception as error:
        logger.warning("qoder check-in derivation failed: %s", type(error).__name__)
        return None
    finally:
        await session.close()
    access_token = session.security_oauth_token
    refresh_token = session.refresh_token
    if not access_token or not refresh_token:
        logger.warning(
            "qoder check-in derivation returned incomplete credentials (access=%s refresh=%s)",
            bool(access_token),
            bool(refresh_token),
        )
        return None
    return access_token, refresh_token
