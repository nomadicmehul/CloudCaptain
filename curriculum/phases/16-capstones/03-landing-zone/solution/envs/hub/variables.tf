variable "region" {
  type        = string
  description = "Home region of the landing zone."
  default     = "eu-central-1"
}

variable "cidr_block" {
  type        = string
  description = "VPC range of this environment, from the landing zone allocation."
}

variable "zones" {
  type        = list(string)
  description = "Availability zones for the subnets."
}

variable "owner" {
  type        = string
  description = "Team that owns this environment."
}

variable "cost_center" {
  type        = string
  description = "Cost center billed for this environment."
}

variable "spoke_envs" {
  type        = list(string)
  description = "Spoke environments to peer with; a spoke that is not applied yet is skipped until the next hub apply."
  default     = ["dev", "prod"]
}
