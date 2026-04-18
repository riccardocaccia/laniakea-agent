"""
Calls the Laniakea Queue API to update deployment status.
Auth: JWT signed with AGENT_MASTER_PASSWORD (HMAC-SHA256).
      No certificates needed just the shared master password.

HTCondor-style pool password model:
  - one master password governs all agents
  - to revoke ALL agents: change the password on API + all agents and restart
"""

import jwt
import logging
import os
import time
import uuid
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

# Base URL of the Laniakea queue API (HTTPS default port)
API_BASE_URL = os.getenv("LANIAKEA_API_URL", "https://example:8443")
AGENT_MASTER_PASSWORD = os.getenv("AGENT_MASTER_PASSWORD", "")
AGENT_ID = os.getenv("AGENT_ID", "laniakea-agent")

# NOTE: CA cert to verify the API server's TLS certificate
AGENT_CA_CERT = os.getenv("AGENT_CA_CERT", "certs/ca.crt")

TOKEN_TTL_SECONDS = 300 # short lived token

# ============================================================
# Token generation
# ============================================================

def _mint_token() -> str:
    """
    Generate a short-lived JWT signed with the master password.

    Payload:
      sub: agent identity (AGENT_ID from .env)
      iat: issued at
      exp: expires in TOKEN_TTL_SECONDS
      jti:  unique token ID
    """
    if not AGENT_MASTER_PASSWORD:
        raise RuntimeError("AGENT_MASTER_PASSWORD is not set.")

    now = int(time.time())
    payload = {
        "sub": AGENT_ID,
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, AGENT_MASTER_PASSWORD, algorithm="HS256")


# ============================================================
# Internal helper
# ============================================================

def _make_client() -> httpx.Client:
    """
    Build an httpx Client with:
      - Authorization: Bearer <JWT>  for agent authentication
      - TLS server verification via CA cert (prevents MITM)
    """
    token = _mint_token()

    # Use the CA cert if it exists, otherwise fall back to system bundle.
    # The CA cert here verifies the API SERVER certificate — not client auth.
    verify: str | bool = AGENT_CA_CERT if os.path.exists(AGENT_CA_CERT) else True

    return httpx.Client(
        base_url=API_BASE_URL,
        headers={"Authorization": f"Bearer {token}"},
        verify=verify,
        timeout=30,
    )


# ============================================================
# Public interface called by terraform_agent.py
# ============================================================

def update_deployment_status(
    deployment_uuid: str, new_status: str, status_reason: Optional[str] = None, outputs: Optional[str] = None,)-> bool:
    # NOTE: add email, not every time only the first
    """
    PATCH /internal/deployments/{uuid}/status on the queue API.

    Parameters
    ----------
    deployment_uuid : str
        UUID of the deployment to update.
    new_status : str
        One of: CREATE_IN_PROGRESS, CREATE_COMPLETE, CREATE_FAILED,
                UPDATE_IN_PROGRESS, UPDATE_FAILED.
    status_reason : str, optional
        Human-readable reason (used on FAILED states).
    outputs : str, optional
        JSON string with deployment outputs (e.g. vm_ip).

    Returns
    -------
    bool
        True on success, False if the API rejected or was unreachable.
        The caller decides whether to raise or continue.
    """
    payload = {"status": new_status.upper()}
    if status_reason:
        payload["status_reason"] = status_reason
    if outputs:
        payload["outputs"] = outputs

    try:
        with _make_client() as client:
            response = client.patch(
                f"/internal/deployments/{deployment_uuid}/status",
                json=payload,
            )

        if response.status_code == 200:
            logger.info("[%s] Status updated to %s via API.", deployment_uuid, new_status)
            return True

        # 409 = invalid transition (e.g. agent tried to go COMPLETE -> IN_PROGRESS)
        # 404 = deployment not found in DB (API never saw the QUEUED write)
        logger.error(
            "[%s] API rejected status update to %s: HTTP %s — %s",
            deployment_uuid,
            new_status,
            response.status_code,
            response.text,
        )
        return False

    except httpx.RequestError as exc:
        logger.error(
            "[%s] Could not reach API to update status: %s", deployment_uuid, exc
        )
        return False
