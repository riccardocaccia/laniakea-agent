"""
Terraform orchestration agent for Laniakea.

State management:
  All deployment state writes go through the API via api_client.update_deployment_status()
  The agent has NO direct database access!

WARNING: improve this logic, now I have application credentials only for GARR
Auth logic for OpenStack:
  - If job.auth.aai_token is present exchange it for a Keystone token
  - Otherwise use app credentials from Vault

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
from api_client import update_deployment_status         
from auth_utils.openstack_auth import get_keystone_token
from vault_utils import get_provider_credentials
from ansible_agent import run_ansible_step
from destroy import run_destroy
from notifier import send_success, send_failure

# Logging configuration for debugging. Prints custom debug messages to help the debug process
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

LOG_DIR = os.getenv("DEPLOYMENT_LOG_DIR", "/var/log/laniakea-agent")
os.makedirs(LOG_DIR, exist_ok=True)

# ============================================================
# Custom handler — pushes each log line to the API
# ============================================================

class _ApiPushHandler(logging.Handler):
    """
    Silently forwards every log record to the API via push_log_line().
    The API appends each line to logs/orchestrator-{uuid}.log on the API VM,
    making it readable by the dashboard without exposing the agent VM directly.

    Failures are swallowed — a broken API connection must never crash the agent.
    """

    def __init__(self, deployment_uuid: str):
        super().__init__()
        self._uuid = deployment_uuid

    def emit(self, record: logging.LogRecord) -> None:
        from api_client import push_log_line  # lazy import avoids circular dep at module load
        try:
            push_log_line(self._uuid, record.levelname, self.format(record))
        except Exception:
            pass  # never crash the agent over a log push failure


# ============================================================
# Per-deployment logger factory
# ============================================================

def _get_deployment_logger(deployment_uuid: str) -> logging.Logger:
    """
    Return a logger bound to a single deployment.

    Writes to three sinks:
      1. logs/orchestrator-{uuid}.log  — local file on the agent VM (useful for SSH debug)
      2. API push handler              — POST /internal/deployments/{uuid}/logs on every line
      3. root StreamHandler            — stdout / systemd journal (via propagate=True)
    """
    dep_logger = logging.getLogger(f"deployment.{deployment_uuid}")
    if dep_logger.handlers:
        return dep_logger  # already configured for this uuid in this process

    dep_logger.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

    # 1. Local file for direct inspection on the agent VM
    log_path = os.path.join(LOG_DIR, f"terraform_{deployment_uuid}.log")
    fh = logging.FileHandler(log_path)
    fh.setFormatter(fmt)
    dep_logger.addHandler(fh)

    # 2. API push so the dashboard can read it via GET /api/deployments/{uuid}/logs
    ph = _ApiPushHandler(deployment_uuid)
    ph.setFormatter(fmt)
    dep_logger.addHandler(ph)

    # 3. Propagate to root handled by basicConfig above
    dep_logger.propagate = True

    return dep_logger


# ========================
# Pydantic models  
# ========================

class OpenPort(BaseModel):
    """
    Class specifically used for a specific input that user can specify:

    - which port to open in the deployed machine.
    """
    port:     int
    protocol: str
    cidr:     str

class AuthConfig(BaseModel):
    """
    AAI token class.
    """
    aai_token: Optional[str] = None
    sub:       str
    group:     str = "default"

class OpenStackInputs(BaseModel):
    """
    OpenStack required inpusts for the customization of the VM.
    """
    flavor:       str
    image:        str
    network_type: str = "private"
    open_ports:   list[OpenPort] = []

class AWSInputs(BaseModel):
    """
    AWS required inpusts for the customization of the VM.
    """
    instance_type: str
    image:         str
    network_type:  str = "public"        # NOTE: see which is better by default
    open_ports:    list[OpenPort] = []

class TemplateConfig(BaseModel):
    #NOTE: I already have repo_url.yml is it necessary??
    """
    Template configuration informations.
    """
    url:    str = ""
    path:   str = "terraform/openstack"
    branch: str = "main"

class OpenStackProvider(BaseModel):
    """
    OpenStack PROVIDER information used for the deployment.
    """
    os_auth_url:                 str
    os_project_id:               str
    region_name:                 str = "RegionOne"     # NOTE: these defualt work only for recas
    private_net_name:            str = "private_net"
    public_net_name:             str = "public_net"
    endpoint_overrides_network:  str
    endpoint_overrides_volumev3: str
    endpoint_overrides_image:    str
    private_network_proxy_host:  Optional[str] = None
    template:                    TemplateConfig = TemplateConfig()
    inputs:                      OpenStackInputs

class AWSProvider(BaseModel):
    """
    AWS PROVIDER information used for the deployment.
    """
    region:     str
    bastion_ip: Optional[str] = None
    template:   TemplateConfig = TemplateConfig(path="terraform/aws")
    inputs:     AWSInputs

class CloudProviders(BaseModel):
    aws:       Optional[AWSProvider] = None
    openstack: Optional[OpenStackProvider] = None

class Job(BaseModel):
    """
    Basic job despcription containing an unique uuid and other useful
    information for the deployment.
    """
    deployment_uuid:   str
    auth:              AuthConfig
    selected_provider: str
    cloud_providers:   CloudProviders
    user_sub:          Optional[str] = None
    user_email:        Optional[str] = None  # added from api in job_data
    requested_by:      Optional[str] = None  # username
    vm_ip:             Optional[str] = None

    def get_sub(self) -> str:
        return self.user_sub or self.auth.sub

    def get_username(self) -> str:
        return self.requested_by or self.auth.sub[:8]

# ============================================================
# Orchestration
# ============================================================

def run_orchestration(job: Job):
    """
    Lifecycle of a cloud deployment.

    State transitions reported to the API:
      QUEUED: 
        -> CREATE_IN_PROGRESS  (agent starts)
        -> CREATE_COMPLETE     (all steps succeeded)
        -> CREATE_FAILED       (any step failed, VM destroyed)
        -> UPDATE_...          (TODO)
    """
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
        update_deployment_status(uuid, "CREATE_FAILED", status_reason=f"Unknown provider: {provider}")
        return

    logger.info(f"[{uuid}] Provisioning started on {provider} for user {user_sub[:8]}...")

    #check on the password validity
    # Signal to the dashboard that work has started.
    # If the API rejects the token abort here before touching any cloud resource.
    # The deployment stays in QUEUED state in the DB
    ok = update_deployment_status(uuid, "CREATE_IN_PROGRESS")
    if not ok:
        raise PermissionError(
            f"[{uuid}] Unauthorized: AGENT_MASTER_PASSWORD mismatch between agent and API. "
            f"No cloud resources were created. "
            f"Fix the password on both sides and re-enqueue the deployment."
        )

        # NOTE: not sense implementing this 
        # retry to insert the job in the queue
        #raise RuntimeError(
         #   f"[{uuid}] Agent auth failed job released back to queue for retry by another agent."

    
    try:
        client = docker.from_env()

        # vault reading
        logger.info(f"[{uuid}] Reading credentials from Vault...")
        secrets = get_provider_credentials(user_sub, provider)
        ssh_key = secrets.get("ssh_key")
        if not ssh_key:
            raise Exception("ssh_key not found in Vault credentials!")

        tf_vars = {
            "TF_VAR_deployment_uuid": str(uuid),
            "TF_VAR_ssh_public_key":  str(ssh_key).strip(),
        }

        if provider == 'openstack':
            os_data         = job.cloud_providers.openstack
            os_token        = ""
            app_cred_id     = ""
            app_cred_secret = ""

            if job.auth.aai_token and job.auth.aai_token.strip():
                logger.info(f"[{uuid}] AAI token found: exchanging for Keystone token...")
                os_token = get_keystone_token(
                    job.auth.aai_token,
                    os_data.os_auth_url,
                    os_data.os_project_id,
                )
                if not os_token:
                    raise Exception("Keystone token exchange failed.")
            else:
                logger.info(f"[{uuid}] No AAI token: using app credentials from Vault...")
                app_cred_id     = secrets.get("app_credential_id", "")
                app_cred_secret = secrets.get("app_credential_secret", "")
                if not app_cred_id or not app_cred_secret:
                    raise Exception(
                        "No AAI token in job and no app credentials in Vault. "
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
            # SECRET
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

        # Terraform apply
        logger.info(f"[{uuid}] Running Terraform container for {provider}...")
        # Docker informations
        client.containers.run(
            image="hashicorp/terraform:1.5",
            entrypoint="/bin/sh",
            command="-c 'terraform init -no-color && terraform apply -auto-approve -no-color'",
            volumes={tf_dir: {'bind': '/src', 'mode': 'rw'}},
            working_dir="/src",
            environment=tf_vars,
            remove=True,
        )

        #retrieve VM IP
        logger.info(f"[{uuid}] Retrieving vm_ip from Terraform output...")
        vm_ip_bytes = client.containers.run(
            image="hashicorp/terraform:1.5",
            command="output -raw vm_ip",
            volumes={tf_dir: {'bind': '/src', 'mode': 'ro'}},  # read only mode 
            working_dir="/src",
            remove=True,
        )

        # converting the ip to human-readable
        vm_ip     = vm_ip_bytes.decode('utf-8').strip()
        job.vm_ip = vm_ip

        # wait for the vm to boot
        logger.info(f"[{uuid}] Waiting 30s for SSH on Rocky...")
        time.sleep(30)
        logger.info(f"[{uuid}] Infrastructure ready. IP: {vm_ip}")

        # Ansible configuration
        with open("repo_url_template.yml", "r") as yf:
            tpl = yaml.safe_load(yf)

        pb_url  = tpl['resources']['ansible']['playbook']
        req_url = tpl['resources']['ansible']['requirements']

        # Safe fail in ansible_agent.py -> run_amsible_step()
        ansible_ok = run_ansible_step(job, pb_url, req_url)

        if not ansible_ok:
            logger.error(f"[{uuid}] Ansible failed: running emergency destroy...")
            run_destroy(job)    # clean the broken VM
            update_deployment_status(
                uuid, "CREATE_FAILED",
                status_reason="Configuration step (Ansible) failed. Resources destroyed.",
            )
            send_failure(email, username, uuid, reason="Configuration step (Ansible) failed. Resources have been cleaned up.")
        else:
            # report success with vm_ip stored in outputs field
            update_deployment_status(
                uuid, "CREATE_COMPLETE",
                outputs=json.dumps({"vm_ip": vm_ip}),
            )
            logger.info(f"[{uuid}] Deployment completed successfully.")
            send_success(email, username, uuid, vm_ip=vm_ip)

    except Exception as e:
        logger.error(f"[{uuid}] Critical error: {e}")
        run_destroy(job)      # clean the broken VM
        update_deployment_status(uuid, "CREATE_FAILED", status_reason=str(e))
        send_failure(email, username, uuid, reason=str(e))

# main
if __name__ == "__main__":
    with open("deployment_info.json", "r") as f:
        raw_data = json.load(f)
    job = Job(**raw_data)
    run_orchestration(job)
