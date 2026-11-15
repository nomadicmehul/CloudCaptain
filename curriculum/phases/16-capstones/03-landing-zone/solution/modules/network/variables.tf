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
  description = "AWS region of the VPC, for example eu-central-1."

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]$", var.region))
    error_message = "region must look like eu-central-1."
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
  description = "Availability zones for the subnets, at least two, for example [\"eu-central-1a\", \"eu-central-1b\"]."

  validation {
    condition     = length(var.zones) >= 2 && length(var.zones) <= 4 && length(distinct(var.zones)) == length(var.zones)
    error_message = "zones must list two to four different availability zones."
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
  description = "ARN of the S3 bucket that receives the VPC flow logs."

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:s3:::[a-z0-9][a-z0-9.-]{1,61}[a-z0-9](/.*)?$", var.flow_log_destination))
    error_message = "flow_log_destination must be an S3 bucket ARN such as arn:aws:s3:::lz-dev-log-archive."
  }
}
