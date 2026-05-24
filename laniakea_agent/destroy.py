"""
Removes ALL resources created by Terraform:
  - VM instance
  - SSH keypair
  - Security groups
  - Floating IP (if any)

Auth logic for OpenStack (same as terraform_agent):
  - If job.auth.aai_token is present exchange it for a Keystone token
  - Otherwise use app credentials from Vault
"""

import json
import os
import docker
import logging
import laniakea_agent as _pkg
from laniakea_agent.vault_utils import get_provider_credentials
from laniakea_agent.auth_utils.openstack_auth import get_keystone_token

logger = logging.getLogger(__name__)

#  Terraform provider map (same as terraform_agent.py) 
_PKG_TERRAFORM = os.path.join(os.path.dirname(_pkg.__file__), "terraform")

PROVIDER_TERRAFORM_MAP: dict = {
    "openstack":       os.path.join(_PKG_TERRAFORM, "openstack_recas"),
    "openstack_recas": os.path.join(_PKG_TERRAFORM, "openstack_recas"),
    "openstack_garr":  os.path.join(_PKG_TERRAFORM, "openstack_garr"),
    "aws":             os.path.join(_PKG_TERRAFORM, "aws"),
}


def _resolve_tf_dir(provider: str, template_path: str) -> str:
    tf_dir = PROVIDER_TERRAFORM_MAP.get(template_path) or PROVIDER_TERRAFORM_MAP.get(provider)
    if not tf_dir or not os.path.isdir(tf_dir):
        raise Exception(
            f"No terraform config found for provider='{provider}' "
            f"template='{template_path}'. "
            f"Available: {list(PROVIDER_TERRAFORM_MAP.keys())}"
        )
    return tf_dir


def run_destroy(job):
    uuid     = job.deployment_uuid
    provider = job.selected_provider.lower()
    user_sub = job.get_sub()

    # resolve terraform config dir from the installed package
    try:
        if provider == 'openstack':
            template_path = job.cloud_providers.openstack.template.path
        elif provider == 'aws':
            template_path = job.cloud_providers.aws.template.path
        else:
            logger.error(f"[{uuid}] Unknown provider: {provider}")
            return

        tf_dir = _resolve_tf_dir(provider, template_path)
    except Exception as exc:
        logger.error(f"[{uuid}] Cannot resolve terraform dir for destroy: {exc}")
        return

    logger.info(f"[{uuid}] Starting DESTROY on {provider} ({tf_dir})...")

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

            if job.auth.aai_token and job.auth.aai_token.strip():
                logger.info(f"[{uuid}] AAI token found — exchanging for Keystone token (destroy)...")
                os_token = get_keystone_token(
                    job.auth.aai_token,
                    os_data.os_auth_url,
                    os_data.os_project_id,
                )
                if not os_token:
                    logger.warning(f"[{uuid}] AAI → Keystone exchange failed, trying app credentials...")
                    app_cred_id     = secrets.get("app_credential_id", "")
                    app_cred_secret = secrets.get("app_credential_secret", "")
            else:
                logger.info(f"[{uuid}] No AAI token — using app credentials from Vault (destroy)...")
                app_cred_id     = secrets.get("app_credential_id", "")
                app_cred_secret = secrets.get("app_credential_secret", "")
                if not app_cred_id or not app_cred_secret:
                    logger.error(
                        f"[{uuid}] No AAI token and no app credentials — destroy may fail."
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
                "TF_VAR_flavor_name":          os_data.inputs.flavor,
                "TF_VAR_network_type":         os_data.inputs.network_type,
                "TF_VAR_bastion_ip":           proxy_host,
            })

        elif provider == 'aws':
            aws_data = job.cloud_providers.aws
            tf_vars.update({
                "TF_VAR_aws_access_key": secrets.get("access_key", ""),
                "TF_VAR_aws_secret_key": secrets.get("secret_key", ""),
                "TF_VAR_aws_region":     aws_data.region,
                "TF_VAR_instance_type":  aws_data.inputs.instance_type,
                "TF_VAR_network_type":   aws_data.inputs.network_type,
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

