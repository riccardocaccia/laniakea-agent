terraform {
  required_version = ">= 1.4.0"
  backend "http" {}
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

# --- SSH KEY ---

resource "openstack_compute_keypair_v2" "vm_key" {
  name       = "laniakea_key_${var.deployment_uuid}"
  public_key = var.ssh_public_key
}

# --- SECURITY GROUPS ---

resource "openstack_networking_secgroup_v2" "ssh_sg" {
  name        = "ssh-sg-${var.deployment_uuid}"
  description = "SSH access"
}

resource "openstack_networking_secgroup_rule_v2" "ssh_rule_bastion" {
  count             = var.network_type == "private" ? 1 : 0
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = "${var.bastion_ip}/32"
  security_group_id = openstack_networking_secgroup_v2.ssh_sg.id
}

resource "openstack_networking_secgroup_rule_v2" "ssh_rule_open" {
  count             = var.network_type == "public" ? 1 : 0
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
  description = "Ports opened dynamically from orchestrator"
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

# --- VM ---

locals {
  # cloud-init snippet to format and mount the data volume
  mount_snippet = <<-EOT
    runcmd:
      - |
        for i in $(seq 1 30); do [ -b /dev/vdb ] && break; sleep 5; done
        if ! blkid /dev/vdb; then mkfs.ext4 -L data /dev/vdb; fi
        mkdir -p /data
        echo 'LABEL=data /data ext4 defaults,nofail 0 2' >> /etc/fstab
        mount -a
  EOT

  # applied only when storage is requested
  mount_data_volume = var.storage_size_gb > 0 ? local.mount_snippet : ""
}

resource "openstack_compute_instance_v2" "galaxy_vm" {
  name        = "${var.vm_name}-${var.deployment_uuid}"
  image_name  = var.image_name
  flavor_name = var.flavor_name
  key_pair    = openstack_compute_keypair_v2.vm_key.name

  security_groups = [
    "default",
    openstack_networking_secgroup_v2.ssh_sg.name,
    openstack_networking_secgroup_v2.dynamic_sg.name,
  ]

user_data = <<-USERDATA
#cloud-config
users:
  - default
  - name: rocky
    sudo: ["ALL=(ALL) NOPASSWD:ALL"]
    groups: wheel
    shell: /bin/bash
append_to_groups: true
${local.mount_data_volume}
USERDATA

  network {
    uuid = var.network_type == "public" ? data.openstack_networking_network_v2.public_net.id : data.openstack_networking_network_v2.private_net.id
  }
}

# --- STORAGE ---

resource "openstack_blockstorage_volume_v3" "data" {
  count = var.storage_size_gb > 0 ? 1 : 0
  name  = "${var.vm_name}-${var.deployment_uuid}-data"
  size  = var.storage_size_gb
}

resource "openstack_compute_volume_attach_v2" "data_attach" {
  count       = var.storage_size_gb > 0 ? 1 : 0
  instance_id = openstack_compute_instance_v2.galaxy_vm.id
  volume_id   = openstack_blockstorage_volume_v3.data[0].id
}


# --- FLOATING IP (only when topology=floating_ip AND network_type=public) ---

# Alloca una FIP nuova SOLO se l'agent non ne ha trovata una libera da riusare
resource "openstack_networking_floatingip_v2" "fip" {
  count = var.use_floating_ip && var.existing_fip == "" ? 1 : 0
  pool  = var.public_network_name
}

locals {
  vm_fip = var.use_floating_ip ? (
    var.existing_fip != "" ? var.existing_fip : openstack_networking_floatingip_v2.fip[0].address
  ) : ""
}

resource "openstack_compute_floatingip_associate_v2" "fip_assoc" {
  count       = var.use_floating_ip ? 1 : 0
  floating_ip = local.vm_fip
  instance_id = openstack_compute_instance_v2.galaxy_vm.id
}

# --- OUTPUT ---

output "vm_ip" {
  value       = var.use_floating_ip ? local.vm_fip : openstack_compute_instance_v2.galaxy_vm.access_ip_v4
  description = "IP address to reach the VM"
}


