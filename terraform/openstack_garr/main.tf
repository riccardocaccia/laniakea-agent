terraform {
  required_version = ">= 1.4.0"
  required_providers {
    openstack = {
      source  = "terraform-provider-openstack/openstack"
      version = "~> 1.53.0"
    }
  }
}

provider "openstack" {
  auth_url                      = var.os_auth_url
  tenant_id                     = var.os_tenant_id
  region			= var.os_region
  token                         = var.os_token
  application_credential_id     = var.os_app_cred_id
  application_credential_secret = var.os_app_cred_secret
  allow_reauth                  = var.os_token != "" ? false : true
}

data "openstack_networking_network_v2" "private_net" {
  name = var.private_network_name
}

data "openstack_networking_network_v2" "floating_net" {
  name = var.public_network_name
}

resource "openstack_compute_keypair_v2" "vm_key" {
  name       = "rcaccia_key_${var.deployment_uuid}"
  public_key = var.ssh_public_key
}

resource "openstack_networking_secgroup_v2" "ssh_sg" {
  name        = "ssh-sg-${var.deployment_uuid}"
  description = "SSH access"
}

resource "openstack_networking_secgroup_rule_v2" "ssh_rule" {
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = "0.0.0.0/0"
  security_group_id = openstack_networking_secgroup_v2.ssh_sg.id
}

resource "openstack_networking_secgroup_v2" "dynamic_sg" {
  name        = "sg-dynamic-${var.deployment_uuid}"
  description = "Porte dinamiche"
}

resource "openstack_networking_secgroup_rule_v2" "rules" {
  for_each          = { for idx, p in var.open_ports : idx => p }
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = each.value.protocol
  port_range_min    = each.value.port
  port_range_max    = each.value.port
  remote_ip_prefix  = each.value.cidr
  security_group_id = openstack_networking_secgroup_v2.dynamic_sg.id
}

# Porta di rete per la VM
resource "openstack_networking_port_v2" "vm_port" {
  name               = "port-${var.deployment_uuid}"
  network_id         = data.openstack_networking_network_v2.private_net.id
  admin_state_up     = true
  security_group_ids = [
    openstack_networking_secgroup_v2.ssh_sg.id,
    openstack_networking_secgroup_v2.dynamic_sg.id
  ]
}

resource "openstack_compute_instance_v2" "galaxy_vm" {
  name        = "galaxy-${var.deployment_uuid}"
  image_name  = var.image_name
  flavor_name = var.flavor_name
  key_pair    = openstack_compute_keypair_v2.vm_key.name

  user_data = <<-EOF
users:
  - default
  - name: rocky
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    groups: wheel
    shell: /bin/bash
append_to_groups: true
EOF

  network {
    port = openstack_networking_port_v2.vm_port.id
  }
}

# Alloca floating IP dalla rete pubblica
resource "openstack_networking_floatingip_v2" "fip" {
  pool = var.public_network_name
}

# Associa floating IP alla VM
resource "openstack_compute_floatingip_associate_v2" "fip_assoc" {
  floating_ip = openstack_networking_floatingip_v2.fip.address
  instance_id = openstack_compute_instance_v2.galaxy_vm.id
}

output "vm_ip" {
  value       = openstack_networking_floatingip_v2.fip.address
  description = "Floating IP della VM"
}
