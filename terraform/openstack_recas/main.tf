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

# 1. Chiave SSH
resource "openstack_compute_keypair_v2" "vm_key" {
  name       = "rcaccia_key_${var.deployment_uuid}"
  public_key = var.ssh_public_key
}

# 2. Security Group: SSH (Bastion o Diretto)
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

# 3. Security Group: Porte dinamiche (es. porte 80, 443, 22 per IP utente)
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

# 4. Istanza Virtual Machine
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

  # SELEZIONE DINAMICA DELLA RETE:
  # Se network_type è 'public', usa public_net. Altrimenti usa private_net.
  network {
    uuid = var.network_type == "public" ? data.openstack_networking_network_v2.public_net.id : data.openstack_networking_network_v2.private_net.id
  }
}

# --- OUTPUT ---

output "vm_ip" {
  # Restituisce l'IP dell'istanza (sarà pubblico o privato in base alla rete scelta sopra)
  value       = openstack_compute_instance_v2.galaxy_vm.access_ip_v4
  description = "Indirizzo IP della VM creata"
}
