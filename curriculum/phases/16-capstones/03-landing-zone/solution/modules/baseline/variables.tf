variable "name" {
  type        = string
  description = "Prefix for the account baseline resources; the log archive bucket becomes <name>-log-archive."

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,40}$", var.name))
    error_message = "name must be lowercase letters, digits or dashes and start with a letter."
  }
}

variable "log_retention_days" {
  type        = number
  description = "Days to keep flow logs and other logs in the archive before they expire."
  default     = 365

  validation {
    condition     = var.log_retention_days >= 90 && var.log_retention_days <= 3650
    error_message = "log_retention_days must be between 90 and 3650."
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
