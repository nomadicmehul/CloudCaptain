variable "aws_endpoint" {
  type        = string
  default     = null
  description = "URL of a local AWS emulator such as moto (lz sets it from LZ_AWS_ENDPOINT). Leave null for a real account."

  validation {
    condition     = var.aws_endpoint == null || can(regex("^https?://(127\\.0\\.0\\.1|localhost)(:[0-9]+)?/?$", var.aws_endpoint))
    error_message = "aws_endpoint must point at a local emulator such as http://127.0.0.1:5000, or be null for a real account."
  }
}

locals {
  emulated = var.aws_endpoint != null
}

provider "aws" {
  region = var.region

  access_key                  = local.emulated ? "testing" : null
  secret_key                  = local.emulated ? "testing" : null
  skip_credentials_validation = local.emulated
  skip_requesting_account_id  = local.emulated
  skip_metadata_api_check     = local.emulated
  s3_use_path_style           = local.emulated

  endpoints {
    ec2      = var.aws_endpoint
    iam      = var.aws_endpoint
    s3       = var.aws_endpoint
    sts      = var.aws_endpoint
    logs     = var.aws_endpoint
    dynamodb = var.aws_endpoint
    kms      = var.aws_endpoint
  }
}
