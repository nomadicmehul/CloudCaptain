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
  description = "Google Cloud region of the subnets, for example europe-west3."

  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]$", var.region))
    error_message = "region must look like europe-west3."
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
  description = "Zones the workloads spread over, at least two, for example [\"europe-west3-a\", \"europe-west3-b\"]; GCP subnets are regional, so one subnet pair is created per zone."

  validation {
    condition     = length(var.zones) >= 2 && length(var.zones) <= 4 && length(distinct(var.zones)) == length(var.zones)
    error_message = "zones must list two to four different zones."
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
  description = "Name of the Cloud Storage bucket that receives the VPC flow logs through a log sink."

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$", var.flow_log_destination))
    error_message = "flow_log_destination must be a Cloud Storage bucket name such as lz-dev-log-archive."
  }
}
