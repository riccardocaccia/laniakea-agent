"""
Removes ALL resources created by Terraform.

Auth logic mirrors terraform_agent.py:
  1. Try OIDC AAI token -> Keystone token
  2. Fall back to app credentials from Vault

State management:
  Terraform state is fetched from the API http backend (one state per
  deployment, table tf_states): destroy works for ANY deployment,
  from ANY agent.

Return value:
  run_destroy(job) -> bool
  True on success, False on failure. It never raises: it is called both
  as emergency cleanup inside run_orchestration (which must continue to
  CREATE_FAILED + email even if destroy fails) and by
  worker_wrapper.destroy_from_dict (which maps the bool to
  DELETE_COMPLETE / DELETE_FAILED).
"""

import json
import os
import docker
import logging
import requests
import laniakea_agent as _pkg
from laniakea_agent.vault_utils import get_provider_credentials
from laniakea_agent.auth_utils.openstack_auth import get_keystone_token
########################
import shutil
from laniakea_agent.api_client import API_BASE_URL, mint_backend_token

#TF_PLUGIN_CACHE_HOST = os.getenv("TF_PLUGIN_CACHE_DIR_HOST", "/var/cache/laniakea-tf-plugins")
TF_PLUGIN_CACHE_HOST = os.getenv(
    "TF_PLUGIN_CACHE_DIR_HOST",
    os.path.expanduser("~/.cache/laniakea-tf-plugins"),)


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
        shutil.rmtree(workdir)
    shutil.copytree(tf_dir, workdir)
    os.makedirs(TF_PLUGIN_CACHE_HOST, exist_ok=True)
    return workdir
########################

logger = logging.getLogger(__name__)

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


def _get_os_auth_destroy(job, os_data, secrets, uuid) -> tuple:
    """
    Same auth logic as terraform_agent._get_os_auth — OIDC first, app creds fallback.
    Returns (os_token, app_cred_id, app_cred_secret).
    """
    os_token        = ""
    app_cred_id     = ""
    app_cred_secret = ""

    if job.auth.aai_token and job.auth.aai_token.strip():
        logger.info(f"[{uuid}] AAI token found — exchanging for Keystone token (destroy)...")
        os_token = get_keystone_token(
            job.auth.aai_token,
            os_data.os_auth_url,
            os_data.os_project_id,
        ) or ""
        if not os_token:
            logger.warning(f"[{uuid}] OIDC→Keystone failed — falling back to app credentials (destroy).")

    if not os_token:
        app_cred_id     = secrets.get("app_credential_id", "")
        app_cred_secret = secrets.get("app_credential_secret", "")
        if app_cred_id and app_cred_secret:
            logger.info(f"[{uuid}] Using app credentials from Vault (destroy).")
        else:
            logger.error(f"[{uuid}] No auth available for destroy — Terraform may fail.")

    return os_token, app_cred_id, app_cred_secret


def _discover_nets_for_destroy(os_data, auth_token: str, uuid: str) -> tuple:
    """
    Run network discovery to get public/private net names.
    Falls back to job defaults if discovery fails.
    Returns (public_net_name, private_net_name, use_floating_ip).
    """
    from laniakea_agent.network_discovery import discover_networks
    from laniakea_agent.quota_check import _fetch_token_info, _get_catalog_endpoint

    neutron_url = os_data.endpoint_overrides_network or ""
    if not neutron_url and auth_token:
        try:
            token_info  = _fetch_token_info(os_data.os_auth_url, auth_token)
            catalog     = token_info.get("catalog", [])
            neutron_url = _get_catalog_endpoint(catalog, "network", os_data.region_name) or ""
        except Exception as exc:
            logger.warning(f"[{uuid}] Could not resolve Neutron URL for destroy: {exc}")

    if neutron_url and auth_token:
        try:
            net_info = discover_networks(
                neutron_url=neutron_url,
                os_token=auth_token,
                network_type=os_data.inputs.network_type,
            )
            return (
                net_info["public_net_name"],
                net_info["private_net_name"],
                net_info["use_floating_ip"],
            )
        except Exception as exc:
            logger.warning(f"[{uuid}] Network discovery failed for destroy: {exc} — using job defaults")

    return os_data.public_net_name, os_data.private_net_name, False


def run_destroy(job) -> bool:
    uuid     = job.deployment_uuid
    provider = job.selected_provider.lower()
    user_sub = job.get_sub()

    try:
        if provider == 'openstack':
            template_path = job.cloud_providers.openstack.template.path
        elif provider == 'aws':
            template_path = job.cloud_providers.aws.template.path
        else:
            logger.error(f"[{uuid}] Unknown provider: {provider}")
            return False

        tf_dir = _resolve_tf_dir(provider, template_path)
    except Exception as exc:
        logger.error(f"[{uuid}] Cannot resolve terraform dir for destroy: {exc}")
        return False

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
            os_data = job.cloud_providers.openstack

            os_token, app_cred_id, app_cred_secret = _get_os_auth_destroy(
                job, os_data, secrets, uuid
            )

            # For network discovery we need a token — if we only have app creds, get one
            discovery_token = os_token
            if not discovery_token and app_cred_id:
                try:
                    r = requests.post(
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

            public_net_name, private_net_name, use_floating_ip = _discover_nets_for_destroy(
                os_data, discovery_token, uuid
            )

            proxy_host = secrets.get("proxy_host") or os_data.private_network_proxy_host or "0.0.0.0"

            tf_vars.update({
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

        # per-deployment workdir + http backend: the state for THIS uuid is
        # pulled from the API, so destroy works for any past deployment.
        workdir = _prepare_workdir(tf_dir, str(uuid))
        tf_vars["TF_PLUGIN_CACHE_DIR"] = "/plugins"
        try:
            client.containers.run(
                image="hashicorp/terraform:1.5",
                entrypoint="/bin/sh",
                command=f"-c '{_tf_init_cmd(str(uuid))} && terraform destroy -auto-approve -no-color'",
                volumes={
                    workdir: {'bind': '/src', 'mode': 'rw'},
                    TF_PLUGIN_CACHE_HOST: {'bind': '/plugins', 'mode': 'rw'},
                },
                working_dir="/src",
                environment=tf_vars,
                remove=True,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        logger.info(f"[{uuid}] Resources destroyed successfully on {provider}.")
        return True

    except Exception as e:
        logger.error(f"[{uuid}] CRITICAL ERROR during destroy on {provider}: {e}")
        return False
