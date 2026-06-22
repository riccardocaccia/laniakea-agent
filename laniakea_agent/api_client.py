"""
Calls the Laniakea Queue API to update deployment status and to send logs.
Auth: JWT signed with AGENT_MASTER_PASSWORD (HMAC-SHA256).
      No certificates needed just the shared master password.

pool password model:
  - one master password governs all agents
  - to revoke ALL agents: change the password on API and restart
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
# FIXME: default vaules to be mantained? useless? 
API_BASE_URL = os.getenv("LANIAKEA_API_URL", "https://.......:8443")
AGENT_MASTER_PASSWORD = os.getenv("AGENT_MASTER_PASSWORD", "")
AGENT_ID = os.getenv("AGENT_ID", "laniakea-agent")

# NOTE: CA cert to verify the API server's TLS certificate
AGENT_CA_CERT = os.getenv("AGENT_CA_CERT", "certs/ca.crt")

# token lifespan
TOKEN_TTL_SECONDS = 300 # short lived token

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
        raise RuntimeError("AGENT_MASTER_PASSWORD is not set. Set it to proceed")

    now = int(time.time())
    payload = {
        "sub": AGENT_ID,
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, AGENT_MASTER_PASSWORD, algorithm="HS256")


def _make_client() -> httpx.Client:
    """
    Build an httpx Client configuring:
      - Authorization: Bearer <JWT> for agent authentication
      - TLS server verification via CA cert
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


# Public interface called by terraform_agent.py
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
        JSON string with deployment outputs 

    Returns
    -------
    bool
        True on success, False if the API rejected or was unreachable.
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

def push_log_line(deployment_uuid: str, level: str, message: str) -> None:
    """
    POST /internal/deployments/{uuid}/logs  on the Queue API.

    Sends a single formatted log line so the API can accumulate it in
    logs/orchestrator-{uuid}.log on the API VM. The dashboard reads that
    file via GET /api/deployments/{uuid}/logs.

    Failures are silently swallowed.
    """
    payload = {
        "level":   level.upper(),
        "message": message,
        }
    try:
        with _make_client() as client:
            client.post(f"/internal/deployments/{deployment_uuid}/logs", json=payload,)
    except Exception:
        pass  # never crash the agent because of a log push failure

