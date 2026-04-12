"""
Thin HTTP client that calls the Laniakea queue API to update deployment status
uses mutual TLS: the agent presents its client certificate on every request.
The API never receives DB credentials, all state writes go through this client.
"""

import logging
import os
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

# Base URL of the Laniakea queue API (HTTPS default port)
API_BASE_URL = os.getenv("LANIAKEA_API_URL", "https://example:8443")

# NOTE: to be created and imported on the API
# paths to the agent's own cert/key and the CA cert to verify the API server
AGENT_CERT    = os.getenv("AGENT_CERT",    "certs/agent.crt")
AGENT_KEY     = os.getenv("AGENT_KEY",     "certs/agent.key")
AGENT_CA_CERT = os.getenv("AGENT_CA_CERT", "certs/ca.crt")

# ============================================================
# Internal helper
# ============================================================

def _make_client() -> httpx.Client:
    """
    Build an httpx Client configured for mutual TLS.

    cert  = (agent.crt, agent.key)  — identity presented to the API
    verify = ca.crt                 — CA used to validate the API server cert

    Mirrors exactly how WireGuard uses key pairs: each side authenticates
    the other with certs signed by the shared CA.  No Bearer token needed.
    """
    return httpx.Client(
        base_url=API_BASE_URL,
        cert=(AGENT_CERT, AGENT_KEY),
        verify=AGENT_CA_CERT,
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
