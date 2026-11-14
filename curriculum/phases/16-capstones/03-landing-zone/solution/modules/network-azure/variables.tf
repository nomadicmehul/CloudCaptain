variable "name" {
  type        = string
  description = "Prefix for every resource name, for example lz-dev."

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}$", var.name))
    error_message = "name must be 2 to 31 lowercase letters, digits or dashes and start with a letter."
  }
}

variable "env" {
  type        = string
  description = "Environment this network belongs to: hub, dev or prod."

  validation {
    condition     = contains(["hub", "dev", "prod"], var.env)
    error_message = "env must be hub, dev or prod."
  }
}

variable "region" {
  type        = string
  description = "Azure region of the virtual network, for example germanywestcentral."

  validation {
    condition     = can(regex("^[a-z][a-z0-9]+$", var.region))
    error_message = "region must be an Azure region name such as germanywestcentral."
  }
}

variable "cidr_block" {
  type        = string
  description = "IPv4 range of the VPC: a /16 to /20 network inside the landing zone allocation 10.0.0.0/8."

  validation {
    condition = try(
      cidrhost(var.cidr_block, 0) == split("/", var.cidr_block)[0] &&
      cidrhost("${split("/", var.cidr_block)[0]}/8", 0) == "10.0.0.0" &&
      tonumber(split("/", var.cidr_block)[1]) >= 16 &&
      tonumber(split("/", var.cidr_block)[1]) <= 20,
      false
    )
    error_message = "cidr_block must be a /16 to /20 network address inside 10.0.0.0/8, for example 10.1.0.0/16."
  }
}

variable "zones" {
  type        = list(string)
  description = "Availability zones the workloads spread over, at least two, for example [\"1\", \"2\"]; Azure subnets span zones, so one subnet pair is created per zone."

  validation {
    condition     = length(var.zones) >= 2 && length(var.zones) <= 3 && length(distinct(var.zones)) == length(var.zones) && alltrue([for zone in var.zones : contains(["1", "2", "3"], zone)])
    error_message = "zones must list two or three of the zones 1, 2 and 3."
  }
}

variable "tags" {
  type        = map(string)
  description = "Tags for every resource; must include owner, env and cost-center."

  validation {
    condition     = alltrue([for key in ["owner", "env", "cost-center"] : length(trimspace(lookup(var.tags, key, ""))) > 0])
    error_message = "tags must set owner, env and cost-center."
  }
}

variable "flow_log_destination" {
  type        = string
  description = "Resource ID of the storage account that receives the virtual network flow logs."

  validation {
    condition     = can(regex("^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.Storage/storageAccounts/[a-z0-9]{3,24}$", var.flow_log_destination))
    error_message = "flow_log_destination must be a storage account resource ID."
  }
}
