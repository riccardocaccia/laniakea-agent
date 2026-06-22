# FIXME: update

from laniakea_agent.vault_utils import get_secrets
import logging

logger = logging.getLogger(__name__)

def get_aws_credentials(group: str):
    """
    AWS keys retrieving from Vault.
    """
    try:
        # TODO: modify once the final vault is up
        vault_path = f"SECRET/infrastructure/aws/{group}"
        secrets = get_secrets(vault_path)
        
        if not secrets:
            # NOTE : TEST!!! remove default implementation
            logger.warning(f"No secret found for the group: {group}, TRYING DEFAULT...")
            secrets = get_secrets("infrastructure/aws/default")

        return {
            "access": secrets.get("access_key"),
            "secret": secrets.get("secret_key")
        }

    except Exception as e:
        logger.error(f"Error in AWS credential retrieving from Vault: {e}")
        return None
