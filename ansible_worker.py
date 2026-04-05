import os
import subprocess
import shutil
import requests
import logging

logger = logging.getLogger(__name__)

# NOTE: move from here, not interesting... also update from rcaccia to laniakea
GROUP_VARS_URL = "https://raw.githubusercontent.com/riccardocaccia/laniakea-nebula/clean-main/terraform/ansible/group_vars/galaxy.yml"
NGINX_TEMPLATE_URL = "https://raw.githubusercontent.com/riccardocaccia/laniakea-nebula/clean-main/terraform/ansible/templates/nginx/galaxy.j2"
ANSIBLE_VENV = "/tmp/ansible-venv"

class AnsibleWorker:
    def __init__(self, playbook_url, requirements_url, uuid):
        self.playbook_url = playbook_url
        self.requirements_url = requirements_url
        self.uuid = uuid
        self.base_dir = f"/tmp/{uuid}"
        self.playbook_path = os.path.join(self.base_dir, "deploy.yml")
        self.requirements_path = os.path.join(self.base_dir, "requirements.yml")
        self.group_vars_path = os.path.join(self.base_dir, "group_vars", "all.yml")
        self.nginx_template_path = os.path.join(self.base_dir, "templates", "nginx", "galaxy.j2")

    def prepare_environment(self):
        try:
            os.makedirs(self.base_dir, exist_ok=True)
            os.makedirs(os.path.join(self.base_dir, "group_vars"), exist_ok=True)
            os.makedirs(os.path.join(self.base_dir, "templates", "nginx"), exist_ok=True)

            with open(self.playbook_path, "wb") as f:
                f.write(requests.get(self.playbook_url).content)
            with open(self.requirements_path, "wb") as f:
                f.write(requests.get(self.requirements_url).content)
            with open(self.group_vars_path, "wb") as f:
                f.write(requests.get(GROUP_VARS_URL).content)
            with open(self.nginx_template_path, "wb") as f:
                f.write(requests.get(NGINX_TEMPLATE_URL).content)

            return True, "OK"
        except Exception as e:
            return False, str(e)

    def execute_deployment(self, target_ip, ssh_key_path, bastion_ip=None):
        """
        Run CLI command to copy & download playbooks.
        Make a proxy jump if a bastion is specified.
        """
        ssh_base = (
            f"ssh -i {ssh_key_path} "
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
        )
        scp_base = (
            f"scp -i {ssh_key_path} "
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
        )

        if bastion_ip and bastion_ip != "0.0.0.0":
            proxy = (
                f"-o ProxyCommand='ssh -i {ssh_key_path} "
                f"-o StrictHostKeyChecking=no -W %h:%p rocky@{bastion_ip}'"
            )
            ssh_base += proxy + " "
            scp_base += proxy + " "

        remote = f"rocky@{target_ip}"

        steps = [
            # 1. Create directory strucfure VM
            f"{ssh_base} {remote} 'sudo mkdir -p /tmp/galaxy-deploy/group_vars /tmp/galaxy-deploy/templates/nginx && sudo chown -R rocky /tmp/galaxy-deploy'",

            # 2. Copy all files
            f"{scp_base} {self.playbook_path} {remote}:/tmp/galaxy-deploy/deploy.yml",
            f"{scp_base} {self.requirements_path} {remote}:/tmp/galaxy-deploy/requirements.yml",
            f"{scp_base} {self.group_vars_path} {remote}:/tmp/galaxy-deploy/group_vars/all.yml",
            f"{scp_base} {self.nginx_template_path} {remote}:/tmp/galaxy-deploy/templates/nginx/galaxy.j2",

            # 3. Create a virtualenv and install ansible
            f"{ssh_base} {remote} 'sudo dnf install -y python3-pip git && sudo python3 -m venv {ANSIBLE_VENV} && sudo {ANSIBLE_VENV}/bin/pip install ansible \"virtualenv<20.22\"'",

            # 4. Install ansible roles
            f"{ssh_base} {remote} 'sudo {ANSIBLE_VENV}/bin/ansible-galaxy install -r /tmp/galaxy-deploy/requirements.yml -p /tmp/galaxy-deploy/roles'",

            # 5. execute playbooks in local as root 
            (
                f"{ssh_base} {remote} "
                f"'cd /tmp/galaxy-deploy && "
                f"sudo ANSIBLE_ROLES_PATH=/tmp/galaxy-deploy/roles "
                f"{ANSIBLE_VENV}/bin/ansible-playbook "
                f"-i localhost, -c local "
                f"-e \"target_hosts=localhost\" "   
                f"/tmp/galaxy-deploy/deploy.yml "
                f"2>&1 | sudo tee /tmp/galaxy-deploy/ansible.log; "
                f"exit ${{PIPESTATUS[0]}}'"
            ),
        ]

        # debug helper
        for i, step in enumerate(steps):
            logger.info(f"[{self.uuid}] Step {i+1}/{len(steps)}: {step[:80]}...")
            res = subprocess.run(step, shell=True)
            if res.returncode != 0:
                logger.error(f"[{self.uuid}] Step {i+1} fallito.")
                return False

        return True

    def cleanup(self):
        """
        Clean all the directories after the process
        """
        if os.path.exists(self.base_dir):
            shutil.rmtree(self.base_dir)
            logger.info(f"[{self.uuid}] Pulizia cartella temporanea completata.")
