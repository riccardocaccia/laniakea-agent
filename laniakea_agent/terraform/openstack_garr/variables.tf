variable "os_auth_url"          { type = string }
variable "os_tenant_id"         { type = string }
variable "os_region"            { type = string  default = "RegionOne" }
variable "os_token"             { type = string  default = "" }
variable "os_app_cred_id"       { type = string  default = "" }
variable "os_app_cred_secret"   { type = string  default = "" }

variable "deployment_uuid"      { type = string }
variable "ssh_public_key"       { type = string }
variable "image_name"           { type = string }
variable "flavor_name"          { type = string }

variable "network_type"         { type = string  default = "public" }
variable "public_network_name"  { type = string }
variable "private_network_name" { type = string }

# "true" when the cloud uses floating IPs (GARR-style)
# "false" when the cloud has direct public IPs (ReCaS-style)
variable "use_floating_ip"      { type = bool    default = false }

variable "bastion_ip"           { type = string  default = "0.0.0.0" }

variable "open_ports" {
  type = list(object({
    port     = number
    protocol = string
    cidr     = string
  }))
  default = []
}

# ReCaS only — endpoint overrides (omit on GARR)
variable "endpoint_network"  { type = string  default = "" }
variable "endpoint_volumev3" { type = string  default = "" }
variable "endpoint_image"    { type = string  default = "" }

