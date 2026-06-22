"""
Installation script executed ONCE on the agent VM.
It does three things:
  1. Adds ~/.local/bin to PATH in ~/.bashrc
  2. Creates the working directory and the .env template
  3. Creates the log directory

Usage:
    laniakea-agent-install
    laniakea-agent-install --workdir /example/laniakea-agent
"""

import argparse
import os
import sys
import subprocess


def _add_to_path():
    """
    Adds ~/.local/bin to PATH in ~/.bashrc if not already present
    """
    bashrc = os.path.expanduser("~/.bashrc")
    line = 'export PATH=$HOME/.local/bin:$PATH'

    if os.path.exists(bashrc):
        with open(bashrc, "r") as f:
            content = f.read()
        if ".local/bin" in content:
            print("[path] ~/.local/bin already in PATH... skipping")
            return
    
    with open(bashrc, "a") as f:
        f.write(f"\n# added by laniakea-agent-install\n{line}\n")
    # also apply immediately to current process
    os.environ["PATH"] = os.path.expanduser("~/.local/bin") + ":" + os.environ.get("PATH", "")
    print(f"[path] added ~/.local/bin to PATH in ~/.bashrc")
    print(f"[path] PATH updated in the current session")


def _create_workdir(workdir: str):
    """
    creates the working directory with the .env template
    """
    os.makedirs(workdir, exist_ok=True)
    env_path = os.path.join(workdir, ".env")

    if os.path.exists(env_path):
        print(f"[workdir] .env already exists in {workdir}... skipping")
    else:
        template = """\
# ── Agent identity ────────────────────────────────────────────────────────────
AGENT_ID=laniakea-agent-1

# ── Agent pool password (must match the API) ──────────────────────────────────
AGENT_MASTER_PASSWORD=

# ── Laniakea API ──────────────────────────────────────────────────────────────
LANIAKEA_API_URL=https://IP_VM_API:8443/laniakea_core/v1.0
AGENT_CA_CERT=

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_HOST=
REDIS_PORT=1908
REDIS_PASSWORD=

# ── Vault ─────────────────────────────────────────────────────────────────────
VAULT_ADDR=
VAULT_READER_TOKEN=
VAULT_TLS_VERIFY=false

# ── Email notifications ───────────────────────────────────────────────────────
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=Laniakea <noreply@example.com>

# ── Logs ──────────────────────────────────────────────────────────────────────
DEPLOYMENT_LOG_DIR=/var/log/laniakea-agent
"""
        with open(env_path, "w") as f:
            f.write(template)
        os.chmod(env_path, 0o600)
        print(f"[workdir] .env template created in {env_path}")

    print(f"[workdir] working directory: {workdir}")


def _create_log_dir():
    """
    Creates /var/log/laniakea-agent with correct permissions.
    """
    log_dir = "/var/log/laniakea-agent"
    if os.path.exists(log_dir):
        print(f"[logs] {log_dir} already exists... skipping")
        return

    try:
        os.makedirs(log_dir, exist_ok=True)
        # chown to current user
        uid = os.getuid()
        gid = os.getgid()
        os.chown(log_dir, uid, gid)
        print(f"[logs] log directory created: {log_dir}")
    except PermissionError:
        print(f"[logs] insufficient permissions: please run:")
        print(f"       sudo mkdir -p {log_dir} && sudo chown $USER:$USER {log_dir}")


def main():
    """
    Once the installation is completed this function print
    a guide for the user.
    """
    parser = argparse.ArgumentParser(
        prog="laniakea-agent-install",
        description="Initial setup of the VM for laniakea-agent.",
    )
    parser.add_argument(
        "--workdir", "-w",
        default=os.path.expanduser("~/laniakea-agent"),
        help="Agent working directory (default: ~/laniakea-agent)",
    )
    args = parser.parse_args()

    print("\n=== laniakea-agent-install ===\n")

    _add_to_path()
    _create_log_dir()
    _create_workdir(args.workdir)

    print(f"""
=== Setup completed ===

  1. Reload your PATH:
       source ~/.bashrc

  2. Fill in the .env file:
       nano {args.workdir}/.env

  3. Start the agent:
       cd {args.workdir}
       laniakea-agent --queue openstack
""")


if __name__ == "__main__":
    main()
