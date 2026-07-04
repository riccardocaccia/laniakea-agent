"""
Two responsibilities:

  1. check_quota: called at job pickup before Terraform.
     Reads LIVE quota from OpenStack using the Keystone token already
     exchanged from the AAI token. Returns (ok: bool, reason: str).

  2. send_heartbeat(agent_id, provider, os_auth_url, region, os_token)
     called every 30s by the CLI heartbeat loop.
     Reads current quota and POSTs to POST /internal/agents/heartbeat.
     Failures are silently swallowed — a broken API must never crash the agent.

Implementation note:
  Uses direct REST calls against Keystone/Nova/Neutron/Cinder via `requests`.
  Does NOT use the openstack SDK — confirmed via diagnostic that
  openstack.connection.Connection(session=...) built from a manually
  constructed keystoneauth1 Session fails to populate the service catalog
  on this cloud (ReCaS-Bari), even though the token itself is valid and
  scoped and a raw GET /v3/auth/tokens call returns a full 10-entry catalog.
"""

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

MAX_QUOTA_RETRIES = int(os.getenv("QUOTA_MAX_RETRIES", "3"))
RETRY_COUNT_FIELD = "_quota_retry_count"


def _fetch_token_info(os_auth_url: str, os_token: str) -> dict:
    """
    GET /v3/auth/tokens using the token itself as both X-Auth-Token and
    X-Subject-Token. Returns the 'token' object (project, roles, catalog).
    Raises on failure.
    """
    resp = requests.get(
        f"{os_auth_url}/auth/tokens",
        headers={
            "X-Auth-Token": os_token,
            "X-Subject-Token": os_token,
        },
        verify=False,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("token", {})


def _get_catalog_endpoint(catalog: list, svc_type: str, region: str, interface: str = "public") -> Optional[str]:
    """Extract an endpoint URL from a Keystone service catalog by type/region/interface."""
    for svc in catalog:
        if svc.get("type") == svc_type:
            for ep in svc.get("endpoints", []):
                ep_region = ep.get("region") or ep.get("region_id")
                if ep.get("interface") == interface and ep_region == region:
                    return ep["url"].rstrip("/")
    return None


def _get_openstack_quota(os_auth_url: str, os_token: str, region: str) -> Optional[dict]:
    """
    Read current available quota from OpenStack using a Keystone token,
    via direct REST calls to Nova/Neutron/Cinder.
    Returns dict with available compute + optional network/volume resources.
    Returns None if the call fails.
    """
    if not os_token:
        logger.warning("[quota] No Keystone token available for quota check")
        return None

    try:
        token_info = _fetch_token_info(os_auth_url, os_token)
        catalog    = token_info.get("catalog", [])
        project_id = token_info.get("project", {}).get("id", "")

        if not catalog:
            logger.warning("[quota] Service catalog is empty for this token")
            return None

        # Compute (Nova) — mandatory
        compute_url = _get_catalog_endpoint(catalog, "compute", region)
        if not compute_url:
            logger.warning(f"[quota] No 'compute' endpoint found for region {region}")
            return None

        limits_resp = requests.get(
            f"{compute_url}/limits",
            headers={"X-Auth-Token": os_token},
            verify=False,
            timeout=10,
        )
        limits_resp.raise_for_status()
        ab = limits_resp.json()["limits"]["absolute"]

        quota = {
            "instances_available": ab["maxTotalInstances"] - ab["totalInstancesUsed"],
            "cores_available":     ab["maxTotalCores"]     - ab["totalCoresUsed"],
            "ram_mb_available":    ab["maxTotalRAMSize"]   - ab["totalRAMUsed"],
        }

        # Security groups (Neutron) — optional, only checked if the cloud exposes it
        try:
            network_url = _get_catalog_endpoint(catalog, "network", region)
            if network_url and project_id:
                net_resp = requests.get(
                    f"{network_url}/v2.0/quotas/{project_id}/details.json",
                    headers={"X-Auth-Token": os_token},
                    verify=False,
                    timeout=10,
                )
                if net_resp.ok:
                    nq = net_resp.json().get("quota", {})
                    if "security_group" in nq:
                        quota["security_groups_available"] = (
                            nq["security_group"]["limit"] - nq["security_group"]["used"]
                        )
        except Exception as exc:
            logger.debug(f"[quota] Security group quota unavailable: {exc}")

        return quota

    except Exception as exc:
        logger.warning(f"[quota] Failed to read OpenStack quota: {exc}")
        return None


def _flavor_requirements(
    flavor_name: str, os_auth_url: str, os_token: str, region: str
) -> Optional[dict]:
    """
    Look up the flavor RAM and vCPU requirements via direct REST call to Nova.
    Returns dict with ram_mb and cores, or None on failure.
    """
    if not os_token:
        return None

    try:
        token_info = _fetch_token_info(os_auth_url, os_token)
        catalog    = token_info.get("catalog", [])

        compute_url = _get_catalog_endpoint(catalog, "compute", region)
        if not compute_url:
            return None

        # try direct lookup by name (Nova accepts name or ID on /flavors/{id})
        resp = requests.get(
            f"{compute_url}/flavors/detail",
            headers={"X-Auth-Token": os_token},
            verify=False,
            timeout=10,
        )
        if not resp.ok:
            return None

        flavors = resp.json().get("flavors", [])
        flavor  = next((f for f in flavors if f["name"] == flavor_name), None)
        if not flavor:
            logger.warning(f"[quota] Flavor '{flavor_name}' not found")
            return None

        return {"ram_mb": flavor["ram"], "cores": flavor["vcpus"]}

    except Exception as exc:
        logger.warning(f"[quota] Failed to read flavor {flavor_name}: {exc}")
        return None


def check_quota(job) -> tuple:
    """
    Check if this agent has enough OpenStack quota to run the job.
    """
    provider = job.selected_provider.lower()

    if provider != "openstack":
        return True, ""

    os_data  = job.cloud_providers.openstack
    os_token = ""

    # Estrazione del target_provider dal JSON
    target_provider = ""
    try:
        if hasattr(job, 'orchestrator') and job.orchestrator:
            if hasattr(job.orchestrator, 'target_provider'):
                target_provider = str(job.orchestrator.target_provider).lower().strip()
            elif isinstance(job.orchestrator, dict):
                target_provider = str(job.orchestrator.get("target_provider", "")).lower().strip()
    except Exception:
        target_provider = ""

    # STRADA RECAS: Usa lo scambio OIDC -> Keystone se il token è presente
    if target_provider == "openstack_recas":
        if job.auth.aai_token and job.auth.aai_token.strip():
            try:
                from laniakea_agent.auth_utils.openstack_auth import get_keystone_token
                os_token = get_keystone_token(
                    job.auth.aai_token, os_data.os_auth_url, os_data.os_project_id
                ) or ""
            except Exception as exc:
                logger.warning(f"[quota] AAI→Keystone exchange failed: {exc}")

    # STRADA GARR (o fallback ReCaS se lo scambio sopra non ha prodotto un token): Genera token via App Credentials
    if not os_token:
        try:
            from laniakea_agent.vault_utils import get_provider_credentials
            secrets         = get_provider_credentials(job.get_sub(), "openstack")
            app_cred_id     = secrets.get("app_credential_id", "")
            app_cred_secret = secrets.get("app_credential_secret", "")

            if app_cred_id and app_cred_secret:
                token_resp = requests.post(
                    f"{os_data.os_auth_url}/auth/tokens",
                    json={
                        "auth": {
                            "identity": {
                                "methods": ["application_credential"],
                                "application_credential": {
                                    "id":     app_cred_id,
                                    "secret": app_cred_secret,
                                }
                            }
                        }
                    },
                    verify=False,
                    timeout=10,
                )
                if token_resp.ok:
                    os_token = token_resp.headers.get("X-Subject-Token", "")
                    logger.info("[quota] Keystone token obtained via app credentials")
                else:
                    logger.warning(f"[quota] App credential token exchange failed: HTTP {token_resp.status_code}")
        except Exception as exc:
            logger.warning(f"[quota] Could not get token via app credentials: {exc}")

    if not os_token:
        logger.warning("[quota] No Keystone token available — skipping quota check")
        return True, ""

    quota = _get_openstack_quota(os_data.os_auth_url, os_token, os_data.region_name)

    if quota is None:
        logger.warning("[quota] Could not read quota — skipping")
        return True, ""

    # Compute — mandatory
    if quota["instances_available"] < 1:
        return False, f"No instances available (quota exhausted). Available: {quota['instances_available']}"

    flavor_req = _flavor_requirements(os_data.inputs.flavor, os_data.os_auth_url, os_token, os_data.region_name)
    if flavor_req:
        if quota["ram_mb_available"] < flavor_req["ram_mb"]:
            return False, f"Insufficient RAM. Need {flavor_req['ram_mb']} MB, available {quota['ram_mb_available']} MB."
        if quota["cores_available"] < flavor_req["cores"]:
            return False, f"Insufficient cores. Need {flavor_req['cores']}, available {quota['cores_available']}."

    if quota.get("security_groups_available", 1) < 1:
        return False, "No security groups available (quota exhausted)."

    logger.info(f"[quota] OK — instances: {quota['instances_available']}, cores: {quota['cores_available']}, ram: {quota['ram_mb_available']} MB")
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
