import json
import docker
import os
import yaml
import logging
import time
from typing import Optional
from pydantic import BaseModel, ValidationError, model_validator
from db_handlers import start_log_deployment, update_log_status
from auth_utils.openstack_auth import get_keystone_token
from vault_utils import get_secrets
from ansible_agent import run_ansible_step
from destroy import run_destroy

# Logging configuration for debugging. Prints custom debug messages to help the debug process
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("orchestrator.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class OpenPort(BaseModel):
    """
    Port class specifically used for a specific input that user can specify:

    - which port to open in the deployed machine.
    """
    port: int
    protocol: str
    cidr: str

class AuthConfig(BaseModel):
    """
    AAI token class.
    """
    aai_token: Optional[str] = None
    sub: str
    group: str = "default"

class OpenStackInputs(BaseModel):
    """
    OpenStack required inpusts for the customization of the VM.
    """
    flavor: str
    image: str
    network_type: str = "private"
    open_ports: list[OpenPort] = []

class AWSInputs(BaseModel):
    """
    AWS required inputs for the customization of the VM.
    """
    instance_type: str
    image: str
    network_type: str = "private"
    open_ports: list[OpenPort] = []

class TemplateConfig(BaseModel):
    """
    Template configuration informations.
    """
    url: str = ""
    path: str = "terraform/openstack"
    branch: str = "main"

class OpenStackProvider(BaseModel):
    """
    OpenStack PROVIDER information used for the deployment.
    """
    os_auth_url: str
    os_project_id: str
    region_name: str = "RegionOne"
    private_net_name: str = "private_net"
    public_net_name: str = "public_net"
    endpoint_overrides_network: str
    endpoint_overrides_volumev3: str
    endpoint_overrides_image: str
    private_network_proxy_host: Optional[str] = None
    template: TemplateConfig = TemplateConfig()
    inputs: OpenStackInputs

class AWSProvider(BaseModel):
    """
    AWS PROVIDER information used for the deployment.
    """
    region: str
    bastion_ip: Optional[str] = None
    template: TemplateConfig = TemplateConfig(path="terraform/aws")
    inputs: AWSInputs

class CloudProviders(BaseModel):
    """
    Chosen provider.
    """
    aws: Optional[AWSProvider] = None
    openstack: Optional[OpenStackProvider] = None

class Job(BaseModel):
    """
    Basic job despcription containing an unique uuid and other information
    useful for the deployment.
    """
    deployment_uuid: str
    auth: AuthConfig
    selected_provider: str
    cloud_providers: CloudProviders
    vm_ip: Optional[str] = None

def run_orchestration(job: Job):
    """
    Core orchestration engine responsible for the end-to-end lifecycle of a cloud deployment.

    The function follows a strict sequential pipeline:
    1. Infrastructure Initialization: Resolves the local Terraform template paths and 
       synchronizes the deployment status with the tracking database.
    2. Secure Credential Sourcing: Interfaces with HashiCorp Vault to retrieve sensitive 
       SSH keys and provider-specific API credentials (Keystone tokens or AWS keys).
    3. Containerized Provisioning: Deploys a transient Docker container running Terraform 
       to create the virtual infrastructure, ensuring environment parity and portability.
    4. State Retrieval: Extracts the newly created VM's IP address from Terraform's 
       state output to facilitate the next configuration phase.
    5. Configuration Management: Hands over the control to the Ansible Agent for 
       automated software stack installation.
    6. Automated Rollback (Fail-Safe): Implements a 'Destroy-on-Failure' policy. If any 
       step in the Ansible configuration fails, it triggers an emergency cleanup to 
       delete the VM, preventing billing leakages and 'ghost' resources.
    """
    uuid = job.deployment_uuid
    provider = job.selected_provider.lower()
    group = job.auth.group
    
    if provider == 'openstack':
        template_path = job.cloud_providers.openstack.template.path
    elif provider == 'aws':
        template_path = job.cloud_providers.aws.template.path

    tf_dir = os.path.abspath(template_path)

    logger.info(f"[{uuid}] Provisioning started on {provider}...")
    start_log_deployment(uuid)
    update_log_status(uuid, "INFRASTRUCTURE_PROVISIONING_TERRAFORM")

    try:
        client = docker.from_env()
        
        # Vault secret retrieving
        secrets = get_secrets(f"SECRET/infrastructure/{provider}/{group}")
        if not secrets:
            raise Exception(f"ERROR: did not find any secrets for the group: {group}")
        
        public_key = secrets.get('ssh_key_public')
        if not public_key:
            raise Exception("ERROR: ssh_key_public not found in the Vault!")

        tf_vars = {
            "TF_VAR_deployment_uuid": str(uuid),
            "TF_VAR_ssh_public_key": str(public_key).strip()
        }

        # specific variables for different providers
        if provider == 'openstack':
            os_data = job.cloud_providers.openstack
            os_token = ""
            app_cred_id = ""
            app_cred_secret = ""

            if job.auth.aai_token and job.auth.aai_token.strip():
                os_token = get_keystone_token(job.auth.aai_token, os_data.os_auth_url, os_data.os_project_id)
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
                "TF_VAR_flavor_name": os_data.inputs.flavor,
                "TF_VAR_image_name": os_data.inputs.image,
                "TF_VAR_network_type": os_data.inputs.network_type,
                "TF_VAR_bastion_ip": os_data.private_network_proxy_host or "0.0.0.0",
                "TF_VAR_open_ports": json.dumps([p.model_dump() for p in os_data.inputs.open_ports])
            })

        elif provider == 'aws':
            aws_data = job.cloud_providers.aws
            tf_vars.update({
                "TF_VAR_aws_access_key": secrets['aws_access_key'],
                "TF_VAR_aws_secret_key": secrets['aws_secret_key'],
                "TF_VAR_aws_region": aws_data.region,
                "TF_VAR_instance_type": aws_data.inputs.instance_type,
                "TF_VAR_image_name": str(aws_data.inputs.image).strip(),
                "TF_VAR_network_type": aws_data.inputs.network_type,
                "TF_VAR_bastion_ip": aws_data.bastion_ip or "0.0.0.0",
                "TF_VAR_open_ports": json.dumps([p.model_dump() for p in aws_data.inputs.open_ports])
            })

        
        logger.info(f"[{uuid}] Running a container with Terraform for {provider}...")
        # Docker container informations
        client.containers.run(
            image="hashicorp/terraform:1.5",
            entrypoint="/bin/sh",
            command="-c 'terraform init -no-color && terraform apply -auto-approve -no-color'",
            volumes={tf_dir: {'bind': '/src', 'mode': 'rw'}},
            working_dir="/src",
            environment=tf_vars,
            remove=True
        )

        # recupero IP della vm creata
        logger.info(f"[{uuid}] output vm_ip retrieving...")

        vm_ip_bytes = client.containers.run(
            image="hashicorp/terraform:1.5",
            command="output -raw vm_ip",
            volumes={tf_dir: {'bind': '/src', 'mode': 'ro'}},   # read only because we are looking for the ip
            working_dir="/src",
            remove=True
        )
        # converting the ip to human-readable
        vm_ip = vm_ip_bytes.decode('utf-8').strip()
        job.vm_ip = vm_ip
        
        # Wait the deployment to be completed
        logger.info(f"[{uuid}] Wait 30 seconds for the SSH on Rocky...")
        time.sleep(30)

        update_log_status(uuid, "INFRASTRACTURE_READY", ip_address=vm_ip)
        logger.info(f"[{uuid}] Infrastructure successfully deployed. IP: {vm_ip}")

        ########### PARSING TEMPLATE YAML
        with open("repo_url_template.yml", "r") as yf:
            tpl = yaml.safe_load(yf)
            
        pb_url = tpl['resources']['ansible']['playbook']
        req_url = tpl['resources']['ansible']['requirements']

        # ANSIBLE STEP + FAIL-SAFE
        ansible_ok = run_ansible_step(job, pb_url, req_url)

        if not ansible_ok:
            logger.error(f"[{uuid}] Ansible has failed! Running emergency DESTROY...")
            run_destroy(job) # clean the broken VM on OpenStack/AWS
            update_log_status(uuid, "FAILED", logs="Ansible has failed! Resources destroyed.")
        else:
            update_log_status(uuid, "READY")
            logger.info(f"[{uuid}] Deployment successfully completed.")

    except Exception as e:
        logger.error(f"[{uuid}] Critical Terraform ERROR: {e}")
        run_destroy(job)
        update_log_status(uuid, "FAILED", logs=str(e))


# main block

if __name__ == "__main__":
    try:
        with open("deployment_info.json", "r") as f:
            raw_data = json.load(f)
        
        job_request = Job(**raw_data)
        run_orchestration(job_request)
    except Exception as e:
        logger.error(f"Errore caricamento Job: {e}")
