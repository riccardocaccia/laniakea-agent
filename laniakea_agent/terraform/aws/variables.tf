variable "aws_region" {
  type    = string
  default = "eu-south-1"
}

variable "aws_access_key" {
  type      = string
  sensitive = true
}

variable "aws_secret_key" {
  type      = string
  sensitive = true
}

variable "deployment_uuid" {
  type = string
}

variable "ssh_public_key" {
  type = string
}

variable "bastion_ip" {
  type    = string
  default = ""
}

variable "image_name" {
  type        = string
  description = "AMI ID of the IMAGE (es. Rocky 9)"
}

variable "instance_type" {
  type    = string
  default = "t3.xlarge"
}

variable "storage_size" {
  type    = string
  default = "20"
}

variable "open_ports" {
  type = list(object({
    port     = number
    protocol = string
    cidr     = string
  }))
  default = []
}

#
variable "network_type" {
  type    = string
  default = "public"
}

variable "vm_name" {
  type    = string
  default = "LANIAKEA-vm01"
}

