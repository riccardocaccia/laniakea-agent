import os
import stat
import logging
from ansible_worker import AnsibleWorker

# create a logging istance (for the ansible_agent -> __name__) used for the debugging
logger = logging.getLogger(__name__)

def run_ansible_step(job, playbook_url, requirements_url):
    """
    Passes the needed info to the ansible worker funcions in order to 
    configure the machine with the correct software.
    """
    uuid = job.deployment_uuid
    key_path = f"keys/{uuid}.pem"
    
    worker = AnsibleWorker(playbook_url, requirements_url, uuid)
    
    try:
        success_prep, msg = worker.prepare_environment()
        if not success_prep:
            logger.error(f"[{uuid}] ERROR in Ansible preparation: {msg}")
            return False

        # bastion identification
        bastion = "0.0.0.0"
        if job.selected_provider.lower() == 'openstack':
            os_data = job.cloud_providers.openstack
            if os_data.inputs.network_type == 'private':
                bastion = os_data.private_network_proxy_host or "0.0.0.0"

        success= worker.execute_deployment(
            target_ip=job.vm_ip,
            ssh_key_path=key_path,
            bastion_ip=bastion
        )
        
        worker.cleanup() # clean /tmp/uuid
        return success

    except Exception as e:
        logger.error(f"[{uuid}] EXCEPTION in an Ansible step: {e}")
        worker.cleanup()
        return False
