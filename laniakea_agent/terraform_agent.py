"""
Terraform orchestration agent for Laniakea.

State management:
  All deployment state writes go through the API via api_client.update_deployment_status()
  The agent has NO direct database access!

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
from laniakea_agent.api_client import update_deployment_status, push_log_line
from laniakea_agent.auth_utils.openstack_auth import get_keystone_token
from laniakea_agent.vault_utils import get_provider_credentials
from laniakea_agent.ansible_agent import run_ansible_step
from laniakea_agent.destroy import run_destroy
from laniakea_agent.notifier import send_success, send_failure

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

LOG_DIR = os.getenv("DEPLOYMENT_LOG_DIR", "/var/log/laniakea-agent")
os.makedirs(LOG_DIR, exist_ok=True)

# Terraform provider map 
# Maps provider/template names to the terraform config directory inside the
# installed package. 
#NOTE: Adding a new provider = add one line here.
import laniakea_agent as _pkg
_PKG_TERRAFORM = os.path.join(os.path.dirname(_pkg.__file__), "terraform")
PROVIDER_TERRAFORM_MAP: dict = {
    # openstack aliases
    "openstack":        os.path.join(_PKG_TERRAFORM, "openstack_recas"), # NOTE: recas treated as default
    "openstack_recas":  os.path.join(_PKG_TERRAFORM, "openstack_recas"),
    "openstack_garr":   os.path.join(_PKG_TERRAFORM, "openstack_garr"),
    # aws
    "aws":              os.path.join(_PKG_TERRAFORM, "aws"),
    # ...
}

class _ApiPushHandler(logging.Handler):
    """
    Silently forwards every log record to the API via push_log_line().
    Failures are swallowed.
    """
    def __init__(self, deployment_uuid: str):
        super().__init__()
        self._uuid = deployment_uuid

    def emit(self, record: logging.LogRecord) -> None:
        try:
            push_log_line(self._uuid, record.levelname, self.format(record))
        except Exception:
            pass


def _get_deployment_logger(deployment_uuid: str) -> logging.Logger:
    """
    Return a logger bound to a single deployment.
    Writes to: local file + API push + stdout.
    """
    dep_logger = logging.getLogger(f"deployment.{deployment_uuid}")
    if dep_logger.handlers:
        return dep_logger

    dep_logger.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

    # 1. local file
    log_path = os.path.join(LOG_DIR, f"terraform_{deployment_uuid}.log")
    fh = logging.FileHandler(log_path)
    fh.setFormatter(fmt)
    dep_logger.addHandler(fh)

    # 2. API push
    ph = _ApiPushHandler(deployment_uuid)
    ph.setFormatter(fmt)
    dep_logger.addHandler(ph)

    # 3. propagate to root (stdout)
    dep_logger.propagate = True

    return dep_logger


# Pydantic models 
class OpenPort(BaseModel):
    port:     int
    protocol: str
    cidr:     str

class AuthConfig(BaseModel):
    aai_token: Optional[str] = None
    sub:       str
    group:     str = "default"         # NOTE: .....

class OpenStackInputs(BaseModel):
    flavor:       str
    image:        str
    network_type: str = "private"      # NOTE: .....
    open_ports:   list[OpenPort] = []

class AWSInputs(BaseModel):
    instance_type: str
    image:         str
    network_type:  str = "public"      # NOTE: see what is the best choice
    open_ports:    list[OpenPort] = []

class TemplateConfig(BaseModel):
    """
    template.path selects which terraform config to use.
    Valid values: openstack, openstack_recas, openstack_garr, aws.
    The path is resolved against the installed package — no local files needed.
    """
    url:    str = ""
    path:   str = "openstack_recas"   # default provider
    branch: str = "main"

class OpenStackProvider(BaseModel):
    os_auth_url:                 str
    os_project_id:               str
    region_name:                 str = "RegionOne"       # NOTE: these choice works only for recas (default prov.) 
    private_net_name:            str = "private_net"     # NOTE: as before
    public_net_name:             str = "public_net"      # NOTE: ...
    endpoint_overrides_network:  str
    endpoint_overrides_volumev3: str
    endpoint_overrides_image:    str
    private_network_proxy_host:  Optional[str] = None
    template:                    TemplateConfig = TemplateConfig()
    inputs:                      OpenStackInputs

class AWSProvider(BaseModel):
    region:     str
    bastion_ip: Optional[str] = None
    template:   TemplateConfig = TemplateConfig(path="aws")
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
    user_email:        Optional[str] = None
    requested_by:      Optional[str] = None
    vm_ip:             Optional[str] = None

    def get_sub(self) -> str:
        return self.user_sub or self.auth.sub

    def get_username(self) -> str:
        return self.requested_by or self.auth.sub[:8]


# Orchestration 

def _resolve_tf_dir(provider: str, template_path: str) -> str:
    """
    Resolve the terraform config directory from the installed package.

    Priority:
      1. template.path exact match in PROVIDER_TERRAFORM_MAP
      2. provider name match 
      3. Raise if nothing found.
    """
    # try the explicit template path first
    tf_dir = PROVIDER_TERRAFORM_MAP.get(template_path)
    # fall back to provider name
    if not tf_dir:
        tf_dir = PROVIDER_TERRAFORM_MAP.get(provider)
    if not tf_dir or not os.path.isdir(tf_dir):
        raise Exception(
            f"No terraform config found for provider='{provider}' "
            f"template='{template_path}'. "
            f"Available: {list(PROVIDER_TERRAFORM_MAP.keys())}"
        )
    return tf_dir


def run_orchestration(job: Job):
    """
    End-to-end lifecycle of a cloud deployment.

    State transitions:
      QUEUED -> CREATE_IN_PROGRESS -> CREATE_COMPLETE
                                   -> CREATE_FAILED
    """
    uuid     = job.deployment_uuid
    provider = job.selected_provider.lower()
    user_sub = job.get_sub()
    email    = job.user_email
    username = job.get_username()
    dlog     = _get_deployment_logger(uuid)

    # resolve terraform config dir from the installed package
    try:
        if provider == 'openstack':
            template_path = job.cloud_providers.openstack.template.path
        elif provider == 'aws':
            template_path = job.cloud_providers.aws.template.path
        else:
            raise Exception(f"Unknown provider: {provider}")

        tf_dir = _resolve_tf_dir(provider, template_path)
        dlog.info(f"[{uuid}] Terraform config: {tf_dir}")

    except Exception as exc:
        dlog.error(f"[{uuid}] Provider resolution failed: {exc}")
        update_deployment_status(uuid, "CREATE_FAILED", status_reason=str(exc))
        return

    dlog.info(f"[{uuid}] Provisioning started on {provider} for user {user_sub[:8]}...")

    # auth check if API rejects the token abort before touching cloud resources
    ok = update_deployment_status(uuid, "CREATE_IN_PROGRESS")
    if not ok:
        raise PermissionError(
            f"[{uuid}] Unauthorized: AGENT_MASTER_PASSWORD mismatch between agent and API. "
            f"No cloud resources were created. "
            f"Fix the password on both sides and re-enqueue the deployment."
        )

    # quota check before touching cloud resources
    from laniakea_agent.quota_check import check_quota, MAX_QUOTA_RETRIES, RETRY_COUNT_FIELD

    retry_count = job.__dict__.get(RETRY_COUNT_FIELD, 0)
    quota_ok, quota_reason = check_quota(job)

    if not quota_ok:
        retry_count += 1
        dlog.warning(f"[{uuid}] Insufficient quota: {quota_reason} (attempt {retry_count}/{MAX_QUOTA_RETRIES})")

        if retry_count >= MAX_QUOTA_RETRIES:
            update_deployment_status(uuid, "CREATE_FAILED",
                status_reason=f"No agent with sufficient quota after {MAX_QUOTA_RETRIES} attempts: {quota_reason}")
            send_failure(email, username, uuid, reason=f"Quota exhausted: {quota_reason}")
            return

        # re-queue the job with incremented retry count
        from laniakea_agent.queue_utils import requeue_job
        requeue_job(job, retry_count)
        update_deployment_status(uuid, "QUEUED",
            status_reason=f"Re-queued: insufficient quota ({quota_reason})")
        return

    try:
        client = docker.from_env()

        dlog.info(f"[{uuid}] Reading credentials from Vault...")
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
                dlog.info(f"[{uuid}] AAI token found: exchanging for Keystone token...")
                os_token = get_keystone_token(
                    job.auth.aai_token,
                    os_data.os_auth_url,
                    os_data.os_project_id,
                )
                if not os_token:
                    raise Exception("Keystone token exchange failed.")
            else:
                dlog.info(f"[{uuid}] No AAI token: using app credentials from Vault...")
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
        dlog.info(f"[{uuid}] Running Terraform container for {provider} ({tf_dir})...")
        client.containers.run(
            image="hashicorp/terraform:1.5",
            entrypoint="/bin/sh",
            command="-c 'terraform init -no-color && terraform apply -auto-approve -no-color'",
            volumes={tf_dir: {'bind': '/src', 'mode': 'rw'}},
            working_dir="/src",
            environment=tf_vars,
            remove=True,
        )

        # retrieve VM IP
        dlog.info(f"[{uuid}] Retrieving vm_ip from Terraform output...")
        vm_ip_bytes = client.containers.run(
            image="hashicorp/terraform:1.5",
            command="output -raw vm_ip",
            volumes={tf_dir: {'bind': '/src', 'mode': 'ro'}},
            working_dir="/src",
            remove=True,
        )
        vm_ip     = vm_ip_bytes.decode('utf-8').strip()
        job.vm_ip = vm_ip

        dlog.info(f"[{uuid}] Waiting 30s for SSH on Rocky...")
        time.sleep(30)
        dlog.info(f"[{uuid}] Infrastructure ready. IP: {vm_ip}")

        # Ansible configuration
        # resolve repo_url_template.yml from the installed package
        _repo_url_tpl = os.path.join(os.path.dirname(_pkg.__file__), "repo_url_template.yml")
        with open(_repo_url_tpl, "r") as yf:
            tpl = yaml.safe_load(yf)

        pb_url  = tpl['resources']['ansible']['playbook']
        req_url = tpl['resources']['ansible']['requirements']

        ansible_ok = run_ansible_step(job, pb_url, req_url)

        if not ansible_ok:
            dlog.error(f"[{uuid}] Ansible failed: running emergency destroy...")
            run_destroy(job)
            update_deployment_status(
                uuid, "CREATE_FAILED",
                status_reason="Configuration step (Ansible) failed. Resources destroyed.",
            )
            send_failure(email, username, uuid, reason="Configuration step (Ansible) failed. Resources have been cleaned up.")
        else:
            update_deployment_status(
                uuid, "CREATE_COMPLETE",
                outputs=json.dumps({"vm_ip": vm_ip}),
            )
            dlog.info(f"[{uuid}] Deployment completed successfully.")
            send_success(email, username, uuid, vm_ip=vm_ip)

    except Exception as e:
        dlog.error(f"[{uuid}] Critical error: {e}")
        run_destroy(job)
        update_deployment_status(uuid, "CREATE_FAILED", status_reason=str(e))
        send_failure(email, username, uuid, reason=str(e))


if __name__ == "__main__":
    with open("deployment_info.json", "r") as f:
        raw_data = json.load(f)
    job = Job(**raw_data)
    run_orchestration(job)

