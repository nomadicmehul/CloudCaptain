variable "name" {
  type        = string
  description = "Prefix for role and policy names; roles become <name>-deploy and <name>-readonly."

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,40}$", var.name))
    error_message = "name must be lowercase letters, digits or dashes and start with a letter."
  }
}

variable "allowed_regions" {
  type        = list(string)
  description = "Regions where the roles may act; every other region is denied by the guardrail."

  validation {
    condition     = length(var.allowed_regions) > 0 && alltrue([for region in var.allowed_regions : can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]$", region))])
    error_message = "allowed_regions must list at least one region such as eu-central-1."
  }
}

variable "trusted_principal_arns" {
  type        = list(string)
  description = "IAM principals allowed to assume the roles, for example the account root or a CI role."

  validation {
    condition     = length(var.trusted_principal_arns) > 0 && alltrue([for arn in var.trusted_principal_arns : can(regex("^arn:aws[a-z-]*:iam::[0-9]{12}:(root|role/.+|user/.+)$", arn))])
    error_message = "trusted_principal_arns must list IAM ARNs such as arn:aws:iam::123456789012:root."
  }
}

variable "tags" {
  type        = map(string)
  description = "Tags for every role and policy; must include owner, env and cost-center."

  validation {
    condition     = alltrue([for key in ["owner", "env", "cost-center"] : length(trimspace(lookup(var.tags, key, ""))) > 0])
    error_message = "tags must set owner, env and cost-center."
  }
}
