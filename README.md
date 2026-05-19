# laniakea-api-agent

Cloud deployment worker agent for the Laniakea orchestration platform.
Consumes jobs from Redis queues and orchestrates VM provisioning via Terraform and Ansible.

## Installation

```bash
pip install laniakea-api-agent
```

## Configuration

Create a `.env` file in your working directory:

```bash
# Redis
REDIS_HOST=your-redis-host
REDIS_PORT=6379
REDIS_PASSWORD=your-redis-password

# Laniakea API
LANIAKEA_API_URL=https://your-api-host:8443/laniakea_core/v1.0
AGENT_MASTER_PASSWORD=your-shared-secret
AGENT_ID=laniakea-agent-1

# Vault
VAULT_ADDR=https://your-vault:8200
VAULT_TOKEN=your-vault-token

# Logs
DEPLOYMENT_LOG_DIR=/var/log/laniakea-agent
```

## Usage

```bash
# listen on openstack queue (default)
laniakea-agent

# listen on a specific queue
laniakea-agent --queue openstack

# listen on multiple queues
laniakea-agent --queue openstack --queue aws

# use a custom .env file
laniakea-agent --env /etc/laniakea/agent.env
```

## Requirements

- Python 3.10+
- Docker (for Terraform containers)
- Ansible installed on the host

