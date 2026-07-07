"""
Terraform orchestration agent for Laniakea.

State management:
  All deployment state writes go through the API via api_client.update_deployment_status()
  The agent has NO direct database access!

Auth logic for OpenStack:
  1. If job.auth.aai_token is present, try to exchange it for a Keystone token
     (works on ReCaS which has recas-bari as identity provider)
  2. If that fails or no AAI token, fall back to app credentials from Vault
     (works on GARR and any cloud with app credentials)
  3. If neither works, raise and fail the deployment

Notifications:
  - On success: sends email with VM IP
  - On failure: sends email with error reason
"""

import json
import docker
import os
import re
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
##############
import shutil
from laniakea_agent.api_client import API_BASE_URL, mint_backend_token

#TF_PLUGIN_CACHE_HOST = os.getenv("TF_PLUGIN_CACHE_DIR_HOST", "/var/cache/laniakea-tf-plugins")
TF_PLUGIN_CACHE_HOST = os.getenv(
                "TF_PLUGIN_CACHE_DIR_HOST",os.path.expanduser("~/.cache/laniakea-tf-plugins"),)


def _tf_init_cmd(uuid: str) -> str:
    """
    Build the terraform init command configured for the API http backend.
    State is stored per-deployment in the API (PostgreSQL), so the agent
    is fully stateless and any agent can destroy any deployment.
    """
    base  = f"{API_BASE_URL}/internal/tfstate/{uuid}"
    token = mint_backend_token()
    cfg = (
        f"-backend-config=address={base} "
        f"-backend-config=lock_address={base}/lock "
        f"-backend-config=unlock_address={base}/lock "
        f"-backend-config=lock_method=POST "
        f"-backend-config=unlock_method=DELETE "
        f"-backend-config=username=agent "
        f"-backend-config=password={token} "
        f"-backend-config=skip_cert_verification=true"
    )
    return f"terraform init -no-color {cfg}"


def _prepare_workdir(tf_dir: str, uuid: str) -> str:
    """
    Copy the terraform config to a per-deployment workdir so that
    concurrent jobs never share .terraform / lock files.
    """
    workdir = f"/tmp/laniakea-tf-{uuid}"
    if os.path.exists(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    if os.path.exists(workdir):   # residui
        workdir = f"{workdir}-{int(time.time())}"
    shutil.copytree(tf_dir, workdir)
    os.makedirs(TF_PLUGIN_CACHE_HOST, exist_ok=True)
    return workdir

###############

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

LOG_DIR = os.getenv("DEPLOYMENT_LOG_DIR", "/var/log/laniakea-agent")
os.makedirs(LOG_DIR, exist_ok=True)

import laniakea_agent as _pkg
_PKG_TERRAFORM = os.path.join(os.path.dirname(_pkg.__file__), "terraform")

PROVIDER_TERRAFORM_MAP: dict = {
    "openstack":        os.path.join(_PKG_TERRAFORM, "openstack_recas"),
    "openstack_recas":  os.path.join(_PKG_TERRAFORM, "openstack_recas"),
    "openstack_garr":   os.path.join(_PKG_TERRAFORM, "openstack_garr"),
    "aws":              os.path.join(_PKG_TERRAFORM, "aws"),
}


class _ApiPushHandler(logging.Handler):
    def __init__(self, deployment_uuid: str):
        super().__init__()
        self._uuid = deployment_uuid

    def emit(self, record: logging.LogRecord) -> None:
        try:
            push_log_line(self._uuid, record.levelname, record.getMessage())
        except Exception:
            pass


def _get_deployment_logger(deployment_uuid: str) -> logging.Logger:
    dep_logger = logging.getLogger(f"deployment.{deployment_uuid}")
    if dep_logger.handlers:
        return dep_logger

    dep_logger.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

    log_path = os.path.join(LOG_DIR, f"terraform_{deployment_uuid}.log")
    fh = logging.FileHandler(log_path)
    fh.setFormatter(fmt)
    dep_logger.addHandler(fh)

    ph = _ApiPushHandler(deployment_uuid)
    ph.setFormatter(fmt)
    dep_logger.addHandler(ph)

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
    group:     str = "default"

class OpenStackInputs(BaseModel):
    flavor:       str
    hostname:     Optional[str] = "LANIAKEA-vm01"
    image:        str
    network_type: str = "private"
    open_ports:   list[OpenPort] = []
    storage_size: Optional[str] = ""

class AWSInputs(BaseModel):
    instance_type: str
    hostname:     Optional[str] = "LANIAKEA-vm01"
    image:         str
    network_type:  str = "public"
    open_ports:    list[OpenPort] = []

class TemplateConfig(BaseModel):
    url:    str = ""
    path:   str = "openstack_recas"
    branch: str = "main"

class OpenStackProvider(BaseModel):
    os_auth_url:                 str
    os_project_id:               str
    region_name:                 str = "RegionOne"
    private_net_name:            str = ""
    public_net_name:             str = ""
    endpoint_overrides_network:  str = ""
    endpoint_overrides_volumev3: str = ""
    endpoint_overrides_image:    str = ""
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
    service_type:      Optional[str] = "galaxy"   # galaxy | vm
    user_sub:          Optional[str] = None
    user_email:        Optional[str] = None
    requested_by:      Optional[str] = None
    vm_ip:             Optional[str] = None

    def get_sub(self) -> str:
        return self.user_sub or self.auth.sub

    def get_username(self) -> str:
        return self.requested_by or self.auth.sub[:8]


def _resolve_tf_dir(provider: str, template_path: str) -> str:
    tf_dir = PROVIDER_TERRAFORM_MAP.get(template_path)
    if not tf_dir:
        tf_dir = PROVIDER_TERRAFORM_MAP.get(provider)
    if not tf_dir or not os.path.isdir(tf_dir):
        raise Exception(
            f"No terraform config found for provider='{provider}' "
            f"template='{template_path}'. "
            f"Available: {list(PROVIDER_TERRAFORM_MAP.keys())}"
        )
    return tf_dir


def _get_os_auth(job, os_data, secrets, dlog) -> tuple:
    """
    Resolve OpenStack authentication credentials.

    Returns (os_token, app_cred_id, app_cred_secret).
    Priority:
      1. OIDC AAI token → Keystone token (ReCaS)
      2. App credentials from Vault (GARR and any other cloud)
    Raises if neither is available.
    """
    uuid            = job.deployment_uuid
    os_token        = ""
    app_cred_id     = ""
    app_cred_secret = ""

    # Step 1 — try OIDC → Keystone exchange
    if job.auth.aai_token and job.auth.aai_token.strip():
        dlog.info(f"[{uuid}] AAI token found: exchanging for Keystone token...")
        os_token = get_keystone_token(
            job.auth.aai_token,
            os_data.os_auth_url,
            os_data.os_project_id,
        ) or ""
        if os_token:
            dlog.info(f"[{uuid}] Keystone token obtained via OIDC exchange.")
        else:
            dlog.warning(f"[{uuid}] OIDC→Keystone exchange failed — falling back to app credentials.")

    # Step 2 — fall back to app credentials from Vault
    if not os_token:
        app_cred_id     = secrets.get("app_credential_id", "")
        app_cred_secret = secrets.get("app_credential_secret", "")
        if app_cred_id and app_cred_secret:
            dlog.info(f"[{uuid}] Using app credentials from Vault.")
        else:
            raise Exception(
                "No Keystone token and no app credentials in Vault. "
                "Cannot authenticate to OpenStack. "
                "Either provide an AAI token or store app credentials via /profile/credentials."
            )

    return os_token, app_cred_id, app_cred_secret


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

    ok = update_deployment_status(uuid, "CREATE_IN_PROGRESS")
    if not ok:
        raise PermissionError(
            f"[{uuid}] Unauthorized: AGENT_MASTER_PASSWORD mismatch between agent and API. "
            f"No cloud resources were created."
        )

    from laniakea_agent.quota_check import check_quota, MAX_QUOTA_RETRIES, RETRY_COUNT_FIELD

    retry_count = job.__dict__.get(RETRY_COUNT_FIELD, 0)
    quota_ok, quota_reason = check_quota(job)

    if not quota_ok:
        retry_count += 1
        dlog.warning(f"[{uuid}] Insufficient quota: {quota_reason} (attempt {retry_count}/{MAX_QUOTA_RETRIES})")

        if retry_count >= MAX_QUOTA_RETRIES:
            update_deployment_status(uuid, "CREATE_FAILED",
                status_reason=f"Quota exhausted after {MAX_QUOTA_RETRIES} attempts: {quota_reason}")
            send_failure(email, username, uuid, reason=f"Quota exhausted: {quota_reason}")
            return

        from laniakea_agent.queue_utils import requeue_job
        requeue_job(job, retry_count)
        update_deployment_status(uuid, "QUEUED",
            status_reason=f"Re-queued: insufficient quota ({quota_reason})")
        return

    try:
        client = docker.from_env()

        dlog.info(f"[{uuid}] Reading credentials from Vault...")
        #secrets = get_provider_credentials(user_sub, provider)
        secrets = get_provider_credentials(user_sub, provider,
                      os_auth_url=os_data.os_auth_url if provider == 'openstack' else "")
        ssh_key = secrets.get("ssh_key")
        if not ssh_key:
            raise Exception("ssh_key not found in Vault credentials!")

        tf_vars = {
            "TF_VAR_deployment_uuid": str(uuid),
            "TF_VAR_ssh_public_key":  str(ssh_key).strip(),
        }

        if provider == 'openstack':
            os_data = job.cloud_providers.openstack

            # Resolve auth — OIDC first, app credentials as fallback
            os_token, app_cred_id, app_cred_secret = _get_os_auth(
                job, os_data, secrets, dlog
            )

            # Network discovery — needs a token for Neutron calls
            # If we only have app credentials, get a token for discovery
            discovery_token = os_token
            if not discovery_token and app_cred_id:
                try:
                    import requests as _req
                    r = _req.post(
                        f"{os_data.os_auth_url}/auth/tokens",
                        json={"auth": {"identity": {
                            "methods": ["application_credential"],
                            "application_credential": {
                                "id": app_cred_id, "secret": app_cred_secret
                            }
                        }}},
                        verify=False, timeout=10,
                    )
                    if r.ok:
                        discovery_token = r.headers.get("X-Subject-Token", "")
                except Exception:
                    pass

            from laniakea_agent.network_discovery import discover_networks
            from laniakea_agent.quota_check import _fetch_token_info, _get_catalog_endpoint

            neutron_url = os_data.endpoint_overrides_network or ""
            if not neutron_url and discovery_token:
                try:
                    token_info  = _fetch_token_info(os_data.os_auth_url, discovery_token)
                    catalog     = token_info.get("catalog", [])
                    neutron_url = _get_catalog_endpoint(catalog, "network", os_data.region_name) or ""
                except Exception as exc:
                    dlog.warning(f"[{uuid}] Could not resolve Neutron URL from catalog: {exc}")

            if neutron_url and discovery_token:
                try:
                    net_info = discover_networks(
                        neutron_url=neutron_url,
                        os_token=discovery_token,
                        network_type=os_data.inputs.network_type,
                    )
                    public_net_name  = net_info["public_net_name"]
                    private_net_name = net_info["private_net_name"]
                    use_floating_ip  = net_info["use_floating_ip"]
                    dlog.info(
                        f"[{uuid}] Network discovery: topology={net_info['topology']!r} "
                        f"public={public_net_name!r} private={private_net_name!r} "
                        f"floating_ip={use_floating_ip}"
                    )
                except Exception as exc:
                    dlog.warning(f"[{uuid}] Network discovery failed: {exc} — using job defaults")
                    public_net_name  = os_data.public_net_name
                    private_net_name = os_data.private_net_name
                    use_floating_ip  = False
            else:
                dlog.warning(f"[{uuid}] No Neutron URL or token — using job defaults")
                public_net_name  = os_data.public_net_name
                private_net_name = os_data.private_net_name
                use_floating_ip  = False

            proxy_host = secrets.get("proxy_host") or os_data.private_network_proxy_host or "0.0.0.0"

            tf_vars.update({
                "TF_VAR_vm_name":              os_data.inputs.hostname or "LANIAKEA-vm01",
                "TF_VAR_os_auth_url":          os_data.os_auth_url,
                "TF_VAR_os_tenant_id":         os_data.os_project_id,
                "TF_VAR_os_token":             os_token,
                "TF_VAR_os_app_cred_id":       app_cred_id,
                "TF_VAR_os_app_cred_secret":   app_cred_secret,
                "TF_VAR_os_region":            os_data.region_name,
                "TF_VAR_private_network_name": private_net_name,
                "TF_VAR_public_network_name":  public_net_name,
                "TF_VAR_use_floating_ip":      "true" if use_floating_ip else "false",
                "TF_VAR_endpoint_network":     os_data.endpoint_overrides_network,
                "TF_VAR_endpoint_volumev3":    os_data.endpoint_overrides_volumev3,
                "TF_VAR_endpoint_image":       os_data.endpoint_overrides_image,
                "TF_VAR_flavor_name":          os_data.inputs.flavor,
                "TF_VAR_image_name":           os_data.inputs.image,
                "TF_VAR_network_type":         os_data.inputs.network_type,
                "TF_VAR_bastion_ip":           proxy_host,
                "TF_VAR_open_ports":           json.dumps([p.model_dump() for p in os_data.inputs.open_ports]),
                "TF_VAR_storage_size_gb":      str(int(re.match(r'(\d+)', os_data.inputs.storage_size or '0 ').group(1))),
            })

        elif provider == 'aws':
            aws_data   = job.cloud_providers.aws
            access_key = secrets.get("access_key")
            secret_key = secrets.get("secret_key")
            if not access_key or not secret_key:
                raise Exception("AWS access_key or secret_key not found in Vault credentials!")

            tf_vars.update({
                "TF_VAR_vm_name":        aws_data.inputs.hostname or "LANIAKEA-vm01",
                "TF_VAR_aws_access_key": access_key,
                "TF_VAR_aws_secret_key": secret_key,
                "TF_VAR_aws_region":     aws_data.region,
                "TF_VAR_instance_type":  aws_data.inputs.instance_type,
                "TF_VAR_image_name":     str(aws_data.inputs.image).strip(),
                "TF_VAR_network_type":   aws_data.inputs.network_type,
                "TF_VAR_bastion_ip":     secrets.get("bastion_ip") or aws_data.bastion_ip or "0.0.0.0",
                "TF_VAR_open_ports":     json.dumps([p.model_dump() for p in aws_data.inputs.open_ports]),
            })

        # Terraform apply (per-deployment workdir + http backend)
        workdir = _prepare_workdir(tf_dir, str(uuid))
        tf_vars["TF_PLUGIN_CACHE_DIR"] = "/plugins"
        dlog.info(f"[{uuid}] Running Terraform container for {provider} ({workdir})...")

        try:
            client.containers.run(
                image="hashicorp/terraform:1.5",
                entrypoint="/bin/sh",
                command=f"-c '{_tf_init_cmd(str(uuid))} && terraform apply -auto-approve -no-color'",
                volumes={
                    workdir: {'bind': '/src', 'mode': 'rw'},
                    TF_PLUGIN_CACHE_HOST: {'bind': '/plugins', 'mode': 'rw'},
                },
                working_dir="/src",
                user=f"{os.getuid()}:{os.getgid()}",
                environment=tf_vars,
                remove=True,
            )

            # retrieve VM IP (reads state via the backend config stored in .terraform)
            dlog.info(f"[{uuid}] Retrieving vm_ip from Terraform output...")
            vm_ip_bytes = client.containers.run(
                image="hashicorp/terraform:1.5",
                command="output -raw vm_ip",
                volumes={
                    workdir: {'bind': '/src', 'mode': 'rw'},
                    TF_PLUGIN_CACHE_HOST: {'bind': '/plugins', 'mode': 'rw'},   # <- mancava
                },
                working_dir="/src",
                user=f"{os.getuid()}:{os.getgid()}",
                remove=True,
            )

        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        vm_ip     = vm_ip_bytes.decode('utf-8').strip()
        job.vm_ip = vm_ip

        dlog.info(f"[{uuid}] Waiting 30s for SSH on Rocky...")
        time.sleep(30)
        dlog.info(f"[{uuid}] Infrastructure ready. IP: {vm_ip}")
        
    
        # Ansible config
        # Configuration step: depends on the requested service type.
        # 'vm'     -> plain VM, infrastructure only, skip Ansible
        # 'galaxy' -> full Galaxy configuration via Ansible (default)
        service_type = (job.service_type or "galaxy").lower()

        if service_type == "vm":
            dlog.info(f"[{uuid}] service_type=vm: infrastructure only, skipping Ansible step.")
            ansible_ok = True
        else:
            # resolve repo_url_template.yml from the installed package
            _repo_url_tpl = os.path.join(os.path.dirname(_pkg.__file__), "repo_url_template.yml")
            with open(_repo_url_tpl, "r") as yf:
                tpl = yaml.safe_load(yf)

            pb_url  = tpl['resources']['ansible']['playbook']
            req_url = tpl['resources']['ansible']['requirements']

            ansible_ok = run_ansible_step(job, pb_url, req_url)

        if not ansible_ok:
            dlog.error(f"[{uuid}] Ansible failed: running emergency destroy...")
            destroyed = run_destroy(job)
            reason = "Configuration step (Ansible) failed. Resources destroyed." if destroyed \
                    else "Ansible failed AND emergency destroy FAILED"
            update_deployment_status(uuid, "CREATE_FAILED", status_reason=reason)
            send_failure(email, username, uuid,
                reason="Configuration step (Ansible) failed. Resources have been cleaned up.")
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
