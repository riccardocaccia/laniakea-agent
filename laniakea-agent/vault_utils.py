"""
Vault utilities for the Laniakea agent.

Credentials are stored per-user under:
    secret/data/<user_sub>/credentials

The agent reads them at runtime using a read-only token.
"""

import logging
import os
import hvac

logger = logging.getLogger(__name__)

# NOTE: keep the default? maybe as localhost
VAULT_ADDR       = os.getenv("VAULT_ADDR", "")
VAULT_TOKEN      = os.getenv("VAULT_READER_TOKEN", "")
VAULT_TLS_VERIFY = os.getenv("VAULT_TLS_VERIFY", "false").lower() == "true"
VAULT_MOUNT      = "secret"


def get_vault_client() -> hvac.Client:
    client = hvac.Client(
        url=VAULT_ADDR,
        token=VAULT_TOKEN,
        verify=VAULT_TLS_VERIFY,
    )
    #if not client.is_authenticated():
    #    raise RuntimeError("Vault: authentication failed — check VAULT_READER_TOKEN")
    return client


def get_user_credentials(user_sub: str) -> dict:
    client     = get_vault_client()
    vault_path = f"{user_sub}/credentials"

    try:
        response = client.secrets.kv.v2.read_secret_version(
            mount_point=VAULT_MOUNT,
            path=vault_path,
            raise_on_deleted_version=True,
        )
        secrets = response["data"]["data"]
        logger.info(f"[Vault] Credentials read for user {user_sub[:8]}...")
        return secrets

    except Exception as exc:
        raise RuntimeError(
            f"Vault: cannot read credentials for user {user_sub[:8]}... "
            f"(path: {VAULT_MOUNT}/data/{vault_path}): {exc}"
        )


def get_provider_credentials(user_sub: str, provider: str) -> dict:
    all_creds = get_user_credentials(user_sub)
    provider  = provider.lower()

    if provider == "openstack":
        return {
            "ssh_key":               all_creds.get("openstack_ssh_key"),
            "proxy_host":            all_creds.get("openstack_proxy_host", "0.0.0.0"),
            "app_credential_id":     all_creds.get("openstack_app_credential_id"),
            "app_credential_secret": all_creds.get("openstack_app_credential_secret"),
        }

    elif provider == "aws":
        return {
            "ssh_key":    all_creds.get("aws_ssh_key"),
            "access_key": all_creds.get("aws_access_key"),
            "secret_key": all_creds.get("aws_secret_key"),
            "bastion_ip": all_creds.get("aws_bastion_ip", "0.0.0.0"),
        }

    else:
        raise ValueError(f"Unknown provider: {provider}")
