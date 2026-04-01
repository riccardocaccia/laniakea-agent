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
  region                        = var.os_region
  tenant_id                     = var.os_tenant_id
  token                         = var.os_token
  application_credential_id     = var.os_app_cred_id
  application_credential_secret = var.os_app_cred_secret
  allow_reauth                  = var.os_token != "" ? false : true

  endpoint_overrides = {
    "network"  = var.endpoint_network
    "volumev3" = var.endpoint_volumev3
    "image"    = var.endpoint_image
  }
}

# --- DATA SOURCES ---

data "openstack_networking_network_v2" "private_net" {
  name = var.private_network_name
}

data "openstack_networking_network_v2" "public_net" {
  name = var.public_network_name
}

# --- RESOURCES ---

# SSH key
resource "openstack_compute_keypair_v2" "vm_key" {
  name       = "rcaccia_key_${var.deployment_uuid}"
  public_key = var.ssh_public_key
}

# Security Group
resource "openstack_networking_secgroup_v2" "ssh_internal" {
  name        = "ssh-internal-${var.deployment_uuid}"
  description = "Accesso SSH limitato all'IP del Bastion"
}

resource "openstack_networking_secgroup_rule_v2" "ssh_from_bastion" {
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = "${var.bastion_ip}/32"
  security_group_id = openstack_networking_secgroup_v2.ssh_internal.id
}

# Security Group
resource "openstack_networking_secgroup_v2" "dynamic_sg" {
  name        = "sg-dynamic-${var.deployment_uuid}"
  description = "Porte aperte dinamicamente dall'orchestratore"
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

# Virtual Machine
resource "openstack_compute_instance_v2" "galaxy_vm" {
  name            = "galaxy-${var.deployment_uuid}"
  image_name      = var.image_name
  flavor_name     = var.flavor_name
  key_pair        = openstack_compute_keypair_v2.vm_key.name

  security_groups = [
    "default",
    openstack_networking_secgroup_v2.ssh_internal.name,
    openstack_networking_secgroup_v2.dynamic_sg.name
  ]

  # dynamic network selection
  # if network_type == 'public', uses public_net. Otherwise private_net.
  network {
    uuid = var.network_type == "public" ? data.openstack_networking_network_v2.public_net.id : data.openstack_networking_network_v2.private_net.id
  }
}

# --- OUTPUT ---

output "vm_ip" {
  # Instance IP 
  value       = openstack_compute_instance_v2.galaxy_vm.access_ip_v4
  description = "Indirizzo IP della VM creata"
}
