variable "os_auth_url" {}

variable "os_app_cred_id" {
  type    = string
  default = null
}

variable "open_ports" {
  type = list(object({
    port     = number
    protocol = string
    cidr     = string
  }))
  default = []
}

variable "network_type" {
  type        = string
  description = "Tipo di rete: 'public' o 'private'"
  default     = "private"
}

variable "private_network_name" { 
  type = string 
}

variable "public_network_name" { 
  type = string 
}

variable "os_app_cred_secret" {
  type    = string
  default = null
}

variable "os_token" {
  type    = string
  default = null
}

variable "os_tenant_id" {
  type = string
}

variable "os_region" {
  type    = string
  default = "RegionOne"
}

variable "endpoint_network" {
  type    = string
  default = ""
}

variable "endpoint_volumev3" {
  type    = string
  default = ""
}

variable "endpoint_image" {
  type    = string
  default = ""
}

variable "ssh_public_key" {
  type = string
}

variable "bastion_ip" {
  type = string
}

variable "image_name" {
  type = string
}

variable "flavor_name" {
  type = string
}

variable "deployment_uuid" {
  type = string
}
