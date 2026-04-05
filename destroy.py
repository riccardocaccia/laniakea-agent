"""
Destroy module — teardown infrastructure on failure or explicit delete.

Auth logic for OpenStack (same as terraform_agent):
  - If job.auth.aai_token is present → exchange it for a Keystone token
  - Otherwise → use app credentials from Vault
"""

import json
import os
import docker
import logging
from vault_utils import get_provider_credentials
from auth_utils.openstack_auth import get_keystone_token

logger = logging.getLogger(__name__)


def run_destroy(job):
    uuid     = job.deployment_uuid
    provider = job.selected_provider.lower()
    user_sub = job.get_sub()

    if provider == 'openstack':
        tf_dir = os.path.abspath(job.cloud_providers.openstack.template.path)
    elif provider == 'aws':
        tf_dir = os.path.abspath(job.cloud_providers.aws.template.path)
    else:
        logger.error(f"[{uuid}] Unknown provider: {provider}")
        return

    logger.info(f"[{uuid}] Starting DESTROY on {provider}...")

    try:
        client  = docker.from_env()
        secrets = get_provider_credentials(user_sub, provider)

        ssh_key = secrets.get("ssh_key", "dummy")

        tf_vars = {
            "TF_VAR_deployment_uuid": str(uuid),
            "TF_VAR_ssh_public_key":  str(ssh_key).strip(),
            "TF_VAR_image_name":      "dummy",
            "TF_VAR_bastion_ip":      "0.0.0.0",
            "TF_VAR_open_ports":      json.dumps([]),
        }

        if provider == 'openstack':
            os_data         = job.cloud_providers.openstack
            os_token        = ""
            app_cred_id     = ""
            app_cred_secret = ""

            # NOTE: find a way to manage app credential and aai token
            # AAI token rules over app credential
            if job.auth.aai_token and job.auth.aai_token.strip():
                logger.info(f"[{uuid}] AAI token found — exchanging for Keystone token (destroy)...")
                os_token = get_keystone_token(
                    job.auth.aai_token,
                    os_data.os_auth_url,
                    os_data.os_project_id,
                )
                if not os_token:
                    logger.warning(f"[{uuid}] AAI → Keystone exchange failed during destroy, trying app credentials...")
                    app_cred_id     = secrets.get("app_credential_id", "")
                    app_cred_secret = secrets.get("app_credential_secret", "")
            else:
                # No AAI token → use app credentials from Vault
                logger.info(f"[{uuid}] No AAI token — using app credentials from Vault (destroy)...")
                app_cred_id     = secrets.get("app_credential_id", "")
                app_cred_secret = secrets.get("app_credential_secret", "")
                
                # error
                if not app_cred_id or not app_cred_secret:
                    logger.error(
                        f"[{uuid}] No AAI token and no app credentials in Vault — "
                        "destroy may fail."
                    )

            proxy_host = secrets.get("proxy_host") or os_data.private_network_proxy_host or "0.0.0.0"

            tf_vars.update({
                "TF_VAR_os_auth_url":          os_data.os_auth_url,
                "TF_VAR_os_tenant_id":         os_data.os_project_id,
                "TF_VAR_os_token":             os_token,
                "TF_VAR_os_app_cred_id":       app_cred_id,
                "TF_VAR_os_app_cred_secret":   app_cred_secret,
                "TF_VAR_os_region":            os_data.region_name,
                "TF_VAR_private_network_name": os_data.private_net_name,
                "TF_VAR_public_network_name":  os_data.public_net_name,
                "TF_VAR_endpoint_network":     os_data.endpoint_overrides_network,
                "TF_VAR_endpoint_volumev3":    os_data.endpoint_overrides_volumev3,
                "TF_VAR_endpoint_image":       os_data.endpoint_overrides_image,
                "TF_VAR_flavor_name":          "dummy",                              # placeholder
                "TF_VAR_network_type":         os_data.inputs.network_type,
                "TF_VAR_bastion_ip":           proxy_host,
            })

        elif provider == 'aws':
            aws_data = job.cloud_providers.aws
            tf_vars.update({
                "TF_VAR_aws_access_key": secrets.get("access_key", ""),
                "TF_VAR_aws_secret_key": secrets.get("secret_key", ""),
                "TF_VAR_aws_region":     aws_data.region,
                "TF_VAR_instance_type":  "t3.micro",
                "TF_VAR_storage_size":   "20",
                "TF_VAR_network_type":   "public",
                "TF_VAR_bastion_ip":     secrets.get("bastion_ip") or aws_data.bastion_ip or "0.0.0.0",
            })

        client.containers.run(
            image="hashicorp/terraform:1.5",
            entrypoint="/bin/sh",
            command="-c 'terraform init -no-color && terraform destroy -auto-approve -no-color'",
            volumes={tf_dir: {'bind': '/src', 'mode': 'rw'}},
            working_dir="/src",
            environment=tf_vars,
            remove=True,
        )
        logger.info(f"[{uuid}] Resources destroyed successfully on {provider}.")

    except Exception as e:
        logger.error(f"[{uuid}] CRITICAL ERROR during destroy on {provider}: {e}")
