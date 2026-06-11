"""
Two responsibilities:

  1. check_quota: called at job pickup before Terraform.
     Reads LIVE quota from OpenStack using the Keystone token already
     exchanged from the AAI token. Returns (ok: bool, reason: str).

  2. send_heartbeat(agent_id, provider, os_auth_url, region, os_token)
     called every 30s by the CLI heartbeat loop.
     Reads current quota and POSTs to POST /internal/agents/heartbeat.
     Failures are silently swallowed — a broken API must never crash the agent.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

MAX_QUOTA_RETRIES = int(os.getenv("QUOTA_MAX_RETRIES", "3"))
RETRY_COUNT_FIELD = "_quota_retry_count"


def _get_openstack_quota(os_auth_url: str, os_token: str, region: str) -> Optional[dict]:
    """
    Read current available quota from OpenStack using a Keystone token.
    Returns dict with available compute + optional network/volume resources.
    Returns None if the call fails.
    """
    if not os_token:
        logger.warning("[quota] No Keystone token available for quota check")
        return None

    try:
        import openstack
        conn = openstack.connect(
            auth_url=os_auth_url,
            auth_type="v3token",
            auth={"token": os_token},
            region_name=region,
        )

        limits = conn.compute.get_limits()
        ab = limits.absolute

        quota = {
            "instances_available": ab.max_total_instances - ab.total_instances_used,
            "cores_available":     ab.max_total_cores     - ab.total_cores_used,
            "ram_mb_available":    ab.max_total_ram_size  - ab.total_ram_used,
        }

        # Network quota — optional, not all clouds expose this
        try:
            net_quota = conn.network.get_quota(conn.current_project_id)
            quota["security_groups_available"] = (
                net_quota.security_group - net_quota.security_group_used
            )
            quota["networks_available"] = (
                net_quota.network - net_quota.network_used
            )
        except Exception:
            pass

        # Volume quota — optional
        try:
            vol_quota = conn.block_storage.get_quota_set(conn.current_project_id)
            quota["volumes_available"] = (
                vol_quota.volumes - vol_quota.volumes_used
            )
            quota["volume_storage_available"] = (
                vol_quota.gigabytes - vol_quota.gigabytes_used
            )
        except Exception:
            pass

        return quota

    except Exception as exc:
        logger.warning(f"[quota] Failed to read OpenStack quota: {exc}")
        return None


def _flavor_requirements(
    flavor_name: str, os_auth_url: str, os_token: str, region: str
) -> Optional[dict]:
    """
    Look up the flavor to get its RAM and vCPU requirements.
    Returns dict with ram_mb and cores, or None on failure.
    """
    if not os_token:
        return None

    try:
        import openstack
        conn = openstack.connect(
            auth_url=os_auth_url,
            auth_type="v3token",
            auth={"token": os_token},
            region_name=region,
        )
        flavor = conn.compute.find_flavor(flavor_name)
        if not flavor:
            return None
        return {"ram_mb": flavor.ram, "cores": flavor.vcpus}

    except Exception as exc:
        logger.warning(f"[quota] Failed to read flavor {flavor_name}: {exc}")
        return None


def check_quota(job) -> tuple:
    """
    Check if this agent has enough OpenStack quota to run the job.
    Uses the Keystone token exchanged from the AAI token in the job.

    Returns (True, "") if quota is sufficient or cannot be determined.
    Returns (False, reason) if quota is provably insufficient.
    """
    provider = job.selected_provider.lower()

    if provider != "openstack":
        # AWS quota check not implemented yet — always allow
        return True, ""

    os_data  = job.cloud_providers.openstack
    os_token = ""

    if job.auth.aai_token and job.auth.aai_token.strip():
        try:
            from laniakea_agent.auth_utils.openstack_auth import get_keystone_token
            os_token = get_keystone_token(
                job.auth.aai_token, os_data.os_auth_url, os_data.os_project_id
            ) or ""
        except Exception as exc:
            logger.warning(f"[quota] AAI→Keystone exchange failed: {exc}")

    if not os_token:
        logger.warning("[quota] No Keystone token — skipping quota check")
        return True, ""

    quota = _get_openstack_quota(os_data.os_auth_url, os_token, os_data.region_name)

    if quota is None:
        logger.warning("[quota] Could not read quota — skipping")
        return True, ""

    # Compute — mandatory
    if quota["instances_available"] < 1:
        return False, (
            f"No instances available (quota exhausted). "
            f"Available: {quota['instances_available']}"
        )

    flavor_req = _flavor_requirements(
        os_data.inputs.flavor, os_data.os_auth_url, os_token, os_data.region_name
    )
    if flavor_req:
        if quota["ram_mb_available"] < flavor_req["ram_mb"]:
            return False, (
                f"Insufficient RAM. "
                f"Need {flavor_req['ram_mb']} MB, available {quota['ram_mb_available']} MB."
            )
        if quota["cores_available"] < flavor_req["cores"]:
            return False, (
                f"Insufficient cores. "
                f"Need {flavor_req['cores']}, available {quota['cores_available']}."
            )

    # Network — optional, only checked if the cloud exposes it
    if quota.get("security_groups_available", 1) < 1:
        return False, "No security groups available (quota exhausted)."

    if quota.get("networks_available", 1) < 1:
        return False, "No networks available (quota exhausted)."

    # Volume — optional
    if quota.get("volumes_available", 1) < 1:
        return False, "No volumes available (quota exhausted)."

    logger.info(
        f"[quota] OK — instances: {quota['instances_available']}, "
        f"cores: {quota['cores_available']}, "
        f"ram: {quota['ram_mb_available']} MB"
    )
    return True, ""


def send_heartbeat(
    agent_id: str,
    provider: str,
    os_auth_url: str = "",
    region: str = "",
    os_token: str = "",
) -> None:
    """
    Read current quota and POST to /internal/agents/heartbeat.
    Called every 30 seconds by the CLI heartbeat loop.
    Failures are silently swallowed.
    """
    try:
        quota = _get_openstack_quota(os_auth_url, os_token, region) if provider == "openstack" else {}

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
