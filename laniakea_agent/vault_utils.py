"""
Vault utilities for the Laniakea agent.

Credential layout (written by the Core API, never by the agent):

    secret/data/<user_sub>/credentials                  <- global: ssh_key, ssh_private_key
    secret/data/<user_sub>/service_creds/<name>         <- per-service entries saved from
                                                           the dashboard Service Credentials
                                                           page (openstack app creds, aws keys)

Credential resolution for a deployment, in priority order:
  1. explicit entry chosen in the form (job.credentials_name)
  2. automatic match: openstack by auth_url, aws by field prefix
  3. fallback: global/legacy flat path only

The agent reads at runtime with a read-only token.
"""

import logging
import os
import hvac

logger = logging.getLogger(__name__)

# NOTE: keep? secret is okay?
VAULT_MOUNT = "secret"

# NOTE: keep the default? maybe as localhost
def get_vault_client() -> hvac.Client:
    """
    It reads the vault address (VAULT_ADDR) and the agent's dedicated
    vault token (VAULT_READER_TOKEN). If it doesn't find them in the .env file,
    it blocks execution with an immediate error.
    """
    vault_addr  = os.getenv("VAULT_ADDR", "")
    vault_token = os.getenv("VAULT_READER_TOKEN", "")
    vault_tls   = os.getenv("VAULT_TLS_VERIFY", "false").lower() == "true"

    if not vault_addr or not vault_token:
        raise RuntimeError(
            "Vault: VAULT_ADDR or VAULT_READER_TOKEN not set. "
            "Check your .env file."
        )

    client = hvac.Client(url=vault_addr, token=vault_token, verify=vault_tls)
    return client


def _read_path(sub_path: str) -> dict:
    """
    Read one Vault KV2 path, {} if missing or unreadable.
    """
    client = get_vault_client()
    try:
        response = client.secrets.kv.v2.read_secret_version(
            mount_point=VAULT_MOUNT,
            path=sub_path,
            raise_on_deleted_version=True,
        )
        return response["data"]["data"] or {}
    except Exception:
        return {}


def _list_path(sub_path: str) -> list:
    """
    List child entries of a Vault KV2 path, [] if missing.
    """
    client = get_vault_client()
    try:
        response = client.secrets.kv.v2.list_secrets(
            mount_point=VAULT_MOUNT,
            path=sub_path,
        )
        return [k.rstrip("/") for k in response["data"]["keys"]]
    except Exception:
        return []


def _norm(u: str) -> str:
    """Normalize an auth URL for comparison (trailing slash, case)."""
    return (u or "").rstrip("/").lower()


def _map_fields(all_creds: dict, provider: str) -> dict:
    """
    Map raw Vault fields to the internal credential names used by the agent.
    Double names keep both the new form fields and the legacy flat layout working.
    """
    provider = provider.lower()

    if provider == "openstack":
        return {
            "ssh_key":               all_creds.get("ssh_key") or all_creds.get("openstack_ssh_key"),
            "ssh_private_key":       all_creds.get("ssh_private_key"),
            "proxy_host":            all_creds.get("openstack_proxy_host", ""),
            "app_credential_id":     all_creds.get("openstack_app_credential_id"),
            "app_credential_secret": all_creds.get("openstack_app_credential_secret"),
        }

    elif provider == "aws":
        return {
            "ssh_key":               all_creds.get("ssh_key") or all_creds.get("aws_ssh_key"),
            "ssh_private_key":       all_creds.get("ssh_private_key"),
            "access_key":            all_creds.get("aws_access_key") or all_creds.get("aws_access_key_id"),
            "secret_key":            all_creds.get("aws_secret_key") or all_creds.get("aws_secret_access_key"),
            "bastion_ip":            all_creds.get("aws_bastion_ip", "0.0.0.0"),
        }

    else:
        raise ValueError(f"Unknown provider: {provider}")


def get_user_credentials(user_sub: str) -> dict:
    """
    Legacy helper: read the user's global path. Kept for backward
    compatibility with any caller still using it.
    """
    secrets = _read_path(f"{user_sub}/credentials")
    if not secrets:
        raise RuntimeError(
            f"Vault: cannot read credentials for user {user_sub[:8]}... "
            f"(path: {VAULT_MOUNT}/data/{user_sub}/credentials)"
        )
    logger.info(f"[Vault] Credentials read for user {user_sub[:8]}...")
    return secrets


def get_provider_credentials(user_sub: str, provider: str,
                             os_auth_url: str = "", credentials_name: str = "") -> dict:
    """
    Resolve the credentials for a deployment.

    The global path is always read first (ssh keys live there); the matching
    service_creds entry is merged on top and wins on conflicts.
    """
    merged = _read_path(f"{user_sub}/credentials")

    # 1) explicit entry chosen in the deployment form
    if credentials_name:
        entry = _read_path(f"{user_sub}/service_creds/{credentials_name}")
        if entry:
            logger.info(f"[Vault] Using service_creds '{credentials_name}' for user {user_sub[:8]}...")
            merged.update(entry)
            return _map_fields(merged, provider)
        logger.warning(
            f"[Vault] service_creds '{credentials_name}' not found for user "
            f"{user_sub[:8]}... — falling back to automatic matching."
        )

    # 2) automatic match: openstack by auth_url, aws by field prefix
    for name in _list_path(f"{user_sub}/service_creds"):
        entry = _read_path(f"{user_sub}/service_creds/{name}")
        if provider == "openstack" and _norm(entry.get("openstack_auth_url")) == _norm(os_auth_url):
            logger.info(f"[Vault] Matched service_creds '{name}' by auth_url for user {user_sub[:8]}...")
            merged.update(entry)
            break
        if provider == "aws" and any(k.startswith("aws_") for k in entry):
            logger.info(f"[Vault] Matched service_creds '{name}' (aws) for user {user_sub[:8]}...")
            merged.update(entry)
            break

    # 3) fallback: whatever the global/legacy flat path contains
    logger.info(f"[Vault] Credentials resolved for user {user_sub[:8]}...")
    return _map_fields(merged, provider)
