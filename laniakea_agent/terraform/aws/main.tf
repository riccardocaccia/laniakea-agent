terraform {
  required_version = ">= 1.4.0"
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

  # SSH from Bastion (Port 22)
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["${var.bastion_ip}"]
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
    volume_size = var.storage_size != "" ? var.storage_size : 20
    volume_type = "gp3"
  }

  tags = {
    Name = "galaxy-${var.deployment_uuid}"
  }
}

# --- OUTPUT ---

output "vm_ip" {
  value       = aws_instance.galaxy_vm.public_ip
  description = "Indirizzo IP pubblico della VM su AWS"
}
