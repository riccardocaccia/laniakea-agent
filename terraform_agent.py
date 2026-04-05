"""
Terraform orchestration agent for Laniakea.

Auth logic for OpenStack:
  - If job.auth.aai_token is present → exchange it for a Keystone token (runtime)
  - Otherwise → use app credentials from Vault (stored at profile setup)

Notifications:
  - On success: sends email with VM IP
  - On failure: sends email with error reason
"""

import json
import docker
import os
import yaml
import logging
import time
from typing import Optional
from pydantic import BaseModel
from db_handlers import start_log_deployment, update_log_status
from auth_utils.openstack_auth import get_keystone_token
from vault_utils import get_provider_credentials
from ansible_agent import run_ansible_step
from destroy import run_destroy
from notifier import send_success, send_failure

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("orchestrator.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================================
# Pydantic models
# ============================================================

class OpenPort(BaseModel):
    port:     int
    protocol: str
    cidr:     str

class AuthConfig(BaseModel):
    aai_token: Optional[str] = None
    sub:       str
    group:     str = "default"

class OpenStackInputs(BaseModel):
    flavor:       str
    image:        str
    network_type: str = "private"
    open_ports:   list[OpenPort] = []

class AWSInputs(BaseModel):
    instance_type: str
    image:         str
    network_type:  str = "private"
    open_ports:    list[OpenPort] = []

class TemplateConfig(BaseModel):
    url:    str = ""
    path:   str = "terraform/openstack"
    branch: str = "main"

class OpenStackProvider(BaseModel):
    os_auth_url:                 str
    os_project_id:               str
    region_name:                 str = "RegionOne"
    private_net_name:            str = "private_net"
    public_net_name:             str = "public_net"
    endpoint_overrides_network:  str
    endpoint_overrides_volumev3: str
    endpoint_overrides_image:    str
    private_network_proxy_host:  Optional[str] = None
    template:                    TemplateConfig = TemplateConfig()
    inputs:                      OpenStackInputs

class AWSProvider(BaseModel):
    region:     str
    bastion_ip: Optional[str] = None
    template:   TemplateConfig = TemplateConfig(path="terraform/aws")
    inputs:     AWSInputs

class CloudProviders(BaseModel):
    aws:       Optional[AWSProvider] = None
    openstack: Optional[OpenStackProvider] = None

class Job(BaseModel):
    deployment_uuid:   str
    auth:              AuthConfig
    selected_provider: str
    cloud_providers:   CloudProviders
    user_sub:          Optional[str] = None
    user_email:        Optional[str] = None   # aggiunto dall'API nel job_data
    requested_by:      Optional[str] = None   # username leggibile per le email
    vm_ip:             Optional[str] = None

    def get_sub(self) -> str:
        return self.user_sub or self.auth.sub

    def get_username(self) -> str:
        return self.requested_by or self.auth.sub[:8]

# ============================================================
# Orchestration
# ============================================================

def run_orchestration(job: Job):
    uuid     = job.deployment_uuid
    provider = job.selected_provider.lower()
    user_sub = job.get_sub()
    email    = job.user_email
    username = job.get_username()

    if provider == 'openstack':
        tf_dir = os.path.abspath(job.cloud_providers.openstack.template.path)
    elif provider == 'aws':
        tf_dir = os.path.abspath(job.cloud_providers.aws.template.path)
    else:
        logger.error(f"[{uuid}] Unknown provider: {provider}")
        return

    logger.info(f"[{uuid}] Provisioning started on {provider} for user {user_sub[:8]}...")
    start_log_deployment(uuid)
    update_log_status(uuid, "INFRASTRUCTURE_PROVISIONING_TERRAFORM")

    try:
        client = docker.from_env()

        # ── Leggi credenziali da Vault ────────────────────────────────────────
        logger.info(f"[{uuid}] Reading credentials from Vault...")
        secrets = get_provider_credentials(user_sub, provider)

        ssh_key = secrets.get("ssh_key")
        if not ssh_key:
            raise Exception("ssh_key not found in Vault credentials!")

        tf_vars = {
            "TF_VAR_deployment_uuid": str(uuid),
            "TF_VAR_ssh_public_key":  str(ssh_key).strip(),
        }

        # specific variables divided by providers
        if provider == 'openstack':
            os_data         = job.cloud_providers.openstack
            os_token        = ""
            app_cred_id     = ""
            app_cred_secret = ""

            if job.auth.aai_token and job.auth.aai_token.strip():
                logger.info(f"[{uuid}] AAI token found — exchanging for Keystone token...")
                os_token = get_keystone_token(
                    job.auth.aai_token,
                    os_data.os_auth_url,
                    os_data.os_project_id,
                )
                if not os_token:
                    raise Exception("AAI -> Keystone token exchange failed.")
            else:
                logger.info(f"[{uuid}] No AAI token — using app credentials from Vault...")
                app_cred_id     = secrets.get("app_credential_id", "")
                app_cred_secret = secrets.get("app_credential_secret", "")
                if not app_cred_id or not app_cred_secret:
                    raise Exception(
                        "No AAI token in job and no app credentials found in Vault. "
                        "Cannot authenticate to OpenStack."
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
                "TF_VAR_image_name":           os_data.inputs.image,
                "TF_VAR_network_type":         os_data.inputs.network_type,
                "TF_VAR_bastion_ip":           proxy_host,
                "TF_VAR_open_ports":           json.dumps([p.model_dump() for p in os_data.inputs.open_ports]),
            })

        elif provider == 'aws':
            aws_data   = job.cloud_providers.aws
            access_key = secrets.get("access_key")
            secret_key = secrets.get("secret_key")
            if not access_key or not secret_key:
                raise Exception("AWS access_key or secret_key not found in Vault credentials!")

            tf_vars.update({
                "TF_VAR_aws_access_key": access_key,
                "TF_VAR_aws_secret_key": secret_key,
                "TF_VAR_aws_region":     aws_data.region,
                "TF_VAR_instance_type":  aws_data.inputs.instance_type,
                "TF_VAR_image_name":     str(aws_data.inputs.image).strip(),
                "TF_VAR_network_type":   aws_data.inputs.network_type,
                "TF_VAR_bastion_ip":     secrets.get("bastion_ip") or aws_data.bastion_ip or "0.0.0.0",
                "TF_VAR_open_ports":     json.dumps([p.model_dump() for p in aws_data.inputs.open_ports]),
            })

        #Terraform apply
        logger.info(f"[{uuid}] Running Terraform container for {provider}...")
        client.containers.run(
            image="hashicorp/terraform:1.5",
            entrypoint="/bin/sh",
            command="-c 'terraform init -no-color && terraform apply -auto-approve -no-color'",
            volumes={tf_dir: {'bind': '/src', 'mode': 'rw'}},
            working_dir="/src",
            environment=tf_vars,
            remove=True,
        )

        # Ip retrieving
        logger.info(f"[{uuid}] Retrieving vm_ip from Terraform output...")
        vm_ip_bytes = client.containers.run(
            image="hashicorp/terraform:1.5",
            command="output -raw vm_ip",
            volumes={tf_dir: {'bind': '/src', 'mode': 'ro'}},
            working_dir="/src",
            remove=True,
        )
        vm_ip     = vm_ip_bytes.decode('utf-8').strip()
        job.vm_ip = vm_ip

        logger.info(f"[{uuid}] Waiting 30s for SSH on Rocky...")
        time.sleep(30)

        update_log_status(uuid, "INFRASTRUCTURE_READY", ip_address=vm_ip)
        logger.info(f"[{uuid}] Infrastructure ready. IP: {vm_ip}")

        # ansible steps
        with open("repo_url_template.yml", "r") as yf:
            tpl = yaml.safe_load(yf)

        pb_url  = tpl['resources']['ansible']['playbook']
        req_url = tpl['resources']['ansible']['requirements']

        ansible_ok = run_ansible_step(job, pb_url, req_url)

        if not ansible_ok:
            logger.error(f"[{uuid}] Ansible failed — running emergency destroy...")
            run_destroy(job)
            update_log_status(uuid, "FAILED", logs="Ansible failed. Resources destroyed.")
            send_failure(email, username, uuid, reason="Configuration step (Ansible) failed. Resources have been cleaned up.")
        else:
            update_log_status(uuid, "READY")
            logger.info(f"[{uuid}] Deployment completed successfully.")
            send_success(email, username, uuid, vm_ip=vm_ip)

    except Exception as e:
        logger.error(f"[{uuid}] Critical error: {e}")
        run_destroy(job)
        update_log_status(uuid, "FAILED", logs=str(e))
        send_failure(email, username, uuid, reason=str(e))


#test

if __name__ == "__main__":
    with open("deployment_info.json", "r") as f:
        raw_data = json.load(f)
    job = Job(**raw_data)
    run_orchestration(job)
