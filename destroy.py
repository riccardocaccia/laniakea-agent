import os
import json
import docker
import logging
from vault_utils import get_secrets
from auth_utils.openstack_auth import get_keystone_token

# create a logging istance (for the destroy -> __name__) used for the debugging
logger = logging.getLogger(__name__)

def run_destroy(job):
    """
    Teardown function to destroy infrastructure if deployment fails or is terminated.

    This function dynamically resolves the Terraform working directory based on the selected 
    cloud provider (OpenStack or AWS). It initializes a Docker client to execute Terraform 
    commands within an isolated environment. 

    Security credentials are retrieved 'just-in-time' from HashiCorp Vault:
    - For OpenStack: Uses either a temporary Keystone token (exchanged from an AAI token) 
      or Application Credentials.
    - For AWS: Retrieves Access Key and Secret Key based on the user's group.

    Finally, it launches a short-lived Docker container to run 'terraform destroy', 
    ensuring all cloud resources are purged and the host system remains clean.
    """
    uuid = job.deployment_uuid
    provider = job.selected_provider.lower()
    group = job.auth.group

    if provider == 'openstack':
        tf_dir = os.path.abspath(job.cloud_providers.openstack.template.path)
    elif provider == 'aws':
        tf_dir = os.path.abspath(job.cloud_providers.aws.template.path)
    else:
        logger.error(f"[{uuid}] Provider sconosciuto: {provider}")
        return

    logger.info(f"[{uuid}] starting DESTRUCTION on {provider} using {tf_dir}...")

    try:
        client = docker.from_env()
        secrets = get_secrets(f"SECRET/infrastructure/{provider}/{group}")

        if not secrets:
            raise Exception(f"No secrets found in the Vault to  perform the destroy on {provider}")

        public_key = secrets.get('ssh_key_public', 'dummy')

        tf_vars = {
            "TF_VAR_deployment_uuid": str(uuid),
            "TF_VAR_ssh_public_key": str(public_key).strip(),
            "TF_VAR_image_name": "dummy",
            "TF_VAR_bastion_ip": "0.0.0.0",
            # artificial list in json format
            "TF_VAR_open_ports": json.dumps([]),
        }

        if provider == 'openstack':
            os_data = job.cloud_providers.openstack
            os_token = ""
            app_cred_id = ""
            app_cred_secret = ""

            if job.auth.aai_token and job.auth.aai_token.strip():
                logger.info(f"[{uuid}] AAI token exchange for OpenstStack resources destroy...")
                # token retrieving
                os_token = get_keystone_token(
                    job.auth.aai_token,
                    os_data.os_auth_url,
                    os_data.os_project_id
                )
            else:
                app_cred_id = secrets.get('application_credential_id', "")
                app_cred_secret = secrets.get('application_credential_secret', "")

            tf_vars.update({
                "TF_VAR_os_auth_url": os_data.os_auth_url,
                "TF_VAR_os_tenant_id": os_data.os_project_id,
                "TF_VAR_os_token": os_token,
                "TF_VAR_os_app_cred_id": app_cred_id,
                "TF_VAR_os_app_cred_secret": app_cred_secret,
                "TF_VAR_os_region": os_data.region_name,
                "TF_VAR_private_network_name": os_data.private_net_name,
                "TF_VAR_public_network_name": os_data.public_net_name,
                "TF_VAR_endpoint_network": os_data.endpoint_overrides_network,
                "TF_VAR_endpoint_volumev3": os_data.endpoint_overrides_volumev3,
                "TF_VAR_endpoint_image": os_data.endpoint_overrides_image,
                "TF_VAR_flavor_name": "dummy",
                "TF_VAR_network_type": os_data.inputs.network_type,
                "TF_VAR_bastion_ip": os_data.private_network_proxy_host or "0.0.0.0",
            })

        elif provider == 'aws':
            aws_data = job.cloud_providers.aws
            tf_vars.update({
                "TF_VAR_aws_access_key": str(secrets.get('aws_access_key', '')).strip(),
                "TF_VAR_aws_secret_key": str(secrets.get('aws_secret_key', '')).strip(),
                "TF_VAR_aws_region": aws_data.region,
                "TF_VAR_instance_type": "t3.micro",
                "TF_VAR_storage_size": "20",
                "TF_VAR_network_type": "public",
                "TF_VAR_bastion_ip": aws_data.bastion_ip or "0.0.0.0",
            })

        # setting the docker container for terraform 
        client.containers.run(
            image="hashicorp/terraform:1.5",
            entrypoint="/bin/sh",
            command="-c 'terraform init -no-color && terraform destroy -auto-approve -no-color'",
            volumes={tf_dir: {'bind': '/src', 'mode': 'rw'}},
            working_dir="/src",
            environment=tf_vars,
            remove=True  # container destroyed automaticcaly afther the command
        )
        logger.info(f"[{uuid}] Succesfully destroyed all the resources on {provider}.")

    except Exception as e:
        logger.error(f"[{uuid}] CRITICAL ERROR during the destruction on {provider}: {e}")
