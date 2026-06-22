import os
import logging
from laniakea_agent.ansible_worker import AnsibleWorker
from laniakea_agent.vault_utils import get_provider_credentials

logger = logging.getLogger(__name__)


def run_ansible_step(job, playbook_url, requirements_url):
    """
    Reads the SSH private key from Vault, writes it to a temp file,
    runs Ansible against the deployed VM, then deletes the key from disk.
    """
    uuid     = job.deployment_uuid
    user_sub = job.user_sub or job.auth.sub
    provider = job.selected_provider.lower()

    # read SSH private key from Vault 
    try:
        secrets = get_provider_credentials(user_sub, provider)
    except Exception as exc:
        logger.error(f"[{uuid}] Failed to read credentials from Vault: {exc}")
        return False

    ssh_private_key = secrets.get("ssh_private_key")
    # NOTE: the private key is required to procede, store it in the vault 
    if not ssh_private_key:
        logger.error(
            f"[{uuid}] ssh_private_key not found in Vault "
            f"(path: secret/data/{user_sub}/credentials) : cannot connect to VM"
        )
        return False

    # write key to /tmp  (never stored permanently) 
    key_path = f"/tmp/{uuid}.pem"
    try:
        with open(key_path, "w") as f:
            f.write(ssh_private_key)
        # Necessary to linux to trust the file    
        os.chmod(key_path, 0o600)
        logger.info(f"[{uuid}] SSH key written to {key_path}")
    except Exception as exc:
        logger.error(f"[{uuid}] Failed to write SSH key to disk: {exc}")
        return False

    # run Ansible 
    worker = AnsibleWorker(playbook_url, requirements_url, uuid)
    try:
        success_prep, msg = worker.prepare_environment()
        if not success_prep:
            logger.error(f"[{uuid}] Ansible preparation failed: {msg}")
            return False
        
        # NOTE: private - public managing
        # initialize the bastion to a non existing network
        bastion = "0.0.0.0"
        if provider == 'openstack':
            os_data = job.cloud_providers.openstack
            if os_data.inputs.network_type == 'private':
                # if provate network: assign the variable or 0.0.0.0 to aboid crash
                bastion = os_data.private_network_proxy_host or "0.0.0.0"

        success = worker.execute_deployment(
            target_ip=job.vm_ip,
            ssh_key_path=key_path,
            bastion_ip=bastion,
        )
        return success

    except Exception as exc:
        logger.error(f"[{uuid}] Exception in Ansible step: {exc}")
        return False

    finally:
        # always clean up temp key and ansible working dir
        worker.cleanup()
        if os.path.exists(key_path):
            os.remove(key_path)
            logger.info(f"[{uuid}] SSH key removed from disk")

