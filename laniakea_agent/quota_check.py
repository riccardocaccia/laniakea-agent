"""
Two responsibilities:
  1. check_quota: called at job pickup before Terraform.
     Reads LIVE quota from OpenStack and compares with job requirements.
     Returns (ok: bool, reason: str).

  2. send_heartbeat(agent_id, provider, secrets) — called every 30s by the CLI.
     Reads current quota and POSTs to POST /internal/agents/heartbeat on the API.
     Failures are silently swallowed — a broken API must never crash the agent.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# How many times a job can be re-queued before being marked CREATE_FAILED
MAX_QUOTA_RETRIES = int(os.getenv("QUOTA_MAX_RETRIES", "3"))

# Field name in the job dict that tracks how many times it was re-queued
RETRY_COUNT_FIELD = "_quota_retry_count"


# OpenStack quota helper
def _get_openstack_quota(os_auth_url: str, os_token: str,
                         app_cred_id: str, app_cred_secret: str,
                         region: str) -> Optional[dict]:
    """
    Read current available quota from OpenStack.
    Returns dict with available instances, ram, cores, floating_ips.
    Returns None if the call fails.
    """
    try:
        import openstack
        conn = openstack.connect(
            auth_url=os_auth_url,
            auth_type="v3applicationcredential",
            application_credential_id=app_cred_id,
            application_credential_secret=app_cred_secret,
            region_name=region,
        ) if app_cred_id else openstack.connect(
            auth_url=os_auth_url,
            token=os_token,
            region_name=region,
        )

        limits = conn.compute.get_limits()
        absolute = limits.absolute

        return {
            "instances_available":    absolute.max_total_instances - absolute.total_instances_used,
            "ram_mb_available":       absolute.max_total_ram_size   - absolute.total_ram_used,
            "cores_available":        absolute.max_total_cores       - absolute.total_cores_used,
            "floating_ips_available": absolute.max_total_floating_ips - absolute.total_floating_ips_used,
        }
    except Exception as exc:
        logger.warning(f"[quota] Failed to read OpenStack quota: {exc}")
        return None


def _flavor_requirements(flavor_name: str, os_auth_url: str,
                          os_token: str, app_cred_id: str,
                          app_cred_secret: str, region: str) -> Optional[dict]:
    """
    Look up the flavor to get its RAM and vCPU requirements.
    Returns dict with ram_mb and cores, or None on failure.
    """
    try:
        import openstack
        conn = openstack.connect(
            auth_url=os_auth_url,
            auth_type="v3applicationcredential",
            application_credential_id=app_cred_id,
            application_credential_secret=app_cred_secret,
            region_name=region,
        ) if app_cred_id else openstack.connect(
            auth_url=os_auth_url,
            token=os_token,
            region_name=region,
        )

        flavor = conn.compute.find_flavor(flavor_name)
        if not flavor:
            return None
        return {
            "ram_mb": flavor.ram,
            "cores":  flavor.vcpus,
        }
    except Exception as exc:
        logger.warning(f"[quota] Failed to read flavor {flavor_name}: {exc}")
        return None


# Public interface

def check_quota(job) -> tuple:
    """
    Check if this agent has enough OpenStack quota to run the job.

    Returns (True, "") if quota is sufficient.
    Returns (False, reason) if not.

    Called by terraform_agent.run_orchestration() before Terraform.
    If quota is insufficient, the caller decides whether to retry or fail.
    """
    provider = job.selected_provider.lower()

    if provider != "openstack":
        # AWS quota check not implemented yet — always allow
        return True, ""

    os_data  = job.cloud_providers.openstack
    user_sub = job.get_sub()

    try:
        from laniakea_agent.vault_utils import get_provider_credentials
        secrets = get_provider_credentials(user_sub, provider)
    except Exception as exc:
        logger.warning(f"[quota] Could not read credentials for quota check: {exc}")
        return True, ""  # fail open — let Terraform try

    app_cred_id     = secrets.get("app_credential_id", "")
    app_cred_secret = secrets.get("app_credential_secret", "")
    os_token        = ""

    if job.auth.aai_token and job.auth.aai_token.strip():
        try:
            from laniakea_agent.auth_utils.openstack_auth import get_keystone_token
            os_token = get_keystone_token(
                job.auth.aai_token, os_data.os_auth_url, os_data.os_project_id
            ) or ""
        except Exception:
            pass

    # read live quota
    quota = _get_openstack_quota(
        os_auth_url=os_data.os_auth_url,
        os_token=os_token,
        app_cred_id=app_cred_id,
        app_cred_secret=app_cred_secret,
        region=os_data.region_name,
    )

    if quota is None:
        logger.warning("[quota] Could not read quota: proceeding anyway")
        return True, ""  # fail open

    # need at least 1 instance
    if quota["instances_available"] < 1:
        return False, (
            f"No instances available (used all quota). "
            f"Available: {quota['instances_available']}"
        )

    # check flavor requirements
    flavor_req = _flavor_requirements(
        flavor_name=os_data.inputs.flavor,
        os_auth_url=os_data.os_auth_url,
        os_token=os_token,
        app_cred_id=app_cred_id,
        app_cred_secret=app_cred_secret,
        region=os_data.region_name,
    )

    if flavor_req:
        if quota["ram_mb_available"] < flavor_req["ram_mb"]:
            return False, (
                f"Insufficient RAM quota. "
                f"Need {flavor_req['ram_mb']} MB, available {quota['ram_mb_available']} MB."
            )
        if quota["cores_available"] < flavor_req["cores"]:
            return False, (
                f"Insufficient core quota. "
                f"Need {flavor_req['cores']} cores, available {quota['cores_available']}."
            )

    # check floating IP if public network
    if os_data.inputs.network_type == "public" and quota["floating_ips_available"] < 1:
        return False, (
            f"No floating IPs available. "
            f"Available: {quota['floating_ips_available']}"
        )

    logger.info(
        f"[quota] Quota OK — instances: {quota['instances_available']}, "
        f"ram: {quota['ram_mb_available']} MB, cores: {quota['cores_available']}"
    )
    return True, ""


def send_heartbeat(agent_id: str, provider: str, secrets: dict,
                   os_auth_url: str = "", region: str = "",
                   os_token: str = "") -> None:
    """
    Read current quota and POST to /internal/agents/heartbeat.
    Called every 30 seconds by the CLI heartbeat loop.
    Failures are silently swallowed.
    """
    try:
        app_cred_id     = secrets.get("app_credential_id", "")
        app_cred_secret = secrets.get("app_credential_secret", "")

        quota = _get_openstack_quota(
            os_auth_url=os_auth_url,
            os_token=os_token,
            app_cred_id=app_cred_id,
            app_cred_secret=app_cred_secret,
            region=region,
        )

        from laniakea_agent import __version__
        from laniakea_agent.api_client import _make_client

        payload = {
            "provider": provider,
            "version":  __version__,
            "quota":    quota or {},
        }

        with _make_client() as client:
            client.post("/internal/agents/heartbeat", json=payload)

    except Exception as exc:
        logger.debug(f"[heartbeat] Failed to send heartbeat: {exc}")
