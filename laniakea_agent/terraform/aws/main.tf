terraform {
  required_version = ">= 1.4.0"
  # State lives in the platform API (PostgreSQL) via the http backend,
  # exactly like the OpenStack templates. Without this block Terraform
  # silently IGNORES every -backend-config flag and keeps a LOCAL state
  # inside the ephemeral workdir — which made every AWS destroy a no-op.
  backend "http" {}
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region     = var.aws_region
  access_key = var.aws_access_key
  secret_key = var.aws_secret_key
}

# --- RESOURCES ---

# SSH key
resource "aws_key_pair" "deployer_key" {
  key_name   = "rcaccia-key-${var.deployment_uuid}"
  public_key = var.ssh_public_key
}

# Security Group (Firewall)
resource "aws_security_group" "main_sg" {
  name        = "securgroup-${var.deployment_uuid}"
  description = "Security group for Galaxy deployment"

  # SSH from Bastion (Port 22) — only when a bastion is configured.
  # Accepts both a plain IP (normalized to /32) and a full CIDR.
  # Port 22 from anywhere is already granted via open_ports.
  dynamic "ingress" {
    for_each = var.bastion_ip == "" ? [] : [
      can(regex("/", var.bastion_ip)) ? var.bastion_ip : "${var.bastion_ip}/32"
    ]
    content {
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = [ingress.value]
    }
  }

  # dynamic rule from json 
  dynamic "ingress" {
    for_each = var.open_ports
    content {
      from_port   = ingress.value.port
      to_port     = ingress.value.port
      protocol    = ingress.value.protocol
      cidr_blocks = [ingress.value.cidr]
    }
  }

  # outgoing connection (allowing everything)
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# EC2 instance
resource "aws_instance" "galaxy_vm" {
  ami           = var.image_name  # Qui passeremo l'AMI ID di Rocky 9
  instance_type = var.instance_type
  key_name      = aws_key_pair.deployer_key.key_name

  vpc_security_group_ids = [aws_security_group.main_sg.id]

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }

  # Format and mount the data volume at /data (parity with the OpenStack
  # templates). Script defined in locals: HCL does not allow a heredoc
  # directly inside a conditional expression.
  user_data = local.data_size > 0 ? local.mount_data_script : null

  tags = {
    Name = "${var.vm_name}-${var.deployment_uuid}"
  }
}

locals {
  data_size = var.storage_size != "" ? tonumber(var.storage_size) : 0

  # On nitro instances the attached EBS shows up as /dev/nvme1n1.
  mount_data_script = <<-EOT
    #!/bin/bash
    DEV=""
    for i in $(seq 1 60); do
      for d in /dev/nvme1n1 /dev/xvdf /dev/sdf; do
        if [ -b "$d" ]; then DEV="$d"; break 2; fi
      done
      sleep 2
    done
    [ -z "$DEV" ] && exit 0
    blkid "$DEV" >/dev/null 2>&1 || mkfs.ext4 -L data "$DEV"
    mkdir -p /data
    grep -q 'LABEL=data' /etc/fstab || echo 'LABEL=data /data ext4 defaults,nofail 0 2' >> /etc/fstab
    mount -a
  EOT
}

# Dedicated data volume (created only when a size was requested)
resource "aws_ebs_volume" "data" {
  count             = local.data_size > 0 ? 1 : 0
  availability_zone = aws_instance.galaxy_vm.availability_zone
  size              = local.data_size
  type              = "gp3"
  tags = {
    Name = "${var.vm_name}-${var.deployment_uuid}-data"
  }
}

resource "aws_volume_attachment" "data_attach" {
  count       = local.data_size > 0 ? 1 : 0
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data[0].id
  instance_id = aws_instance.galaxy_vm.id
}

# --- OUTPUT ---

output "vm_ip" {
  value       = aws_instance.galaxy_vm.public_ip
  description = "Indirizzo IP pubblico della VM su AWS"
}

