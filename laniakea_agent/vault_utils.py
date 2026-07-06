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

def get_user_credentials(user_sub: str) -> dict:
    """
    Sends a request to Vault pointing to the user's dedicated Key-Value path: 
    
                        secret/data/<USER_ID>/credentials. 
 
    It extracts the dictionary containing all the user's accumulated secrets.
    """
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
        # NOTE: act here for secret mod.
        return {
            "ssh_key":               all_creds.get("openstack_ssh_key"),
            "ssh_private_key":       all_creds.get("ssh_private_key"),   
            #"proxy_host":            all_creds.get("openstack_proxy_host", "0.0.0.0"),
            "proxy_host":            all_creds.get("openstack_proxy_host", ""),
            "app_credential_id":     all_creds.get("openstack_app_credential_id"),
            "app_credential_secret": all_creds.get("openstack_app_credential_secret"),
        }

    elif provider == "aws":
        # NOTE: act here for secret mod.
        return {
            "ssh_key":               all_creds.get("aws_ssh_key"),
            "ssh_private_key":       all_creds.get("ssh_private_key"),            
            "access_key":            all_creds.get("aws_access_key"),
            "secret_key":            all_creds.get("aws_secret_key"),
            "bastion_ip":            all_creds.get("aws_bastion_ip", "0.0.0.0"),
        }

    else:
        raise ValueError(f"Unknown provider: {provider}")
