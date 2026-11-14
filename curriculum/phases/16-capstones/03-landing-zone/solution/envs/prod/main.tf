data "aws_caller_identity" "current" {}

locals {
  env  = "prod"
  name = "lz-${local.env}"
  tags = {
    owner         = var.owner
    env           = local.env
    "cost-center" = var.cost_center
  }
}

module "baseline" {
  source = "../../modules/baseline"
  name   = local.name
  tags   = local.tags
}

module "network" {
  source               = "../../modules/network"
  name                 = local.name
  env                  = local.env
  region               = var.region
  cidr_block           = var.cidr_block
  zones                = var.zones
  tags                 = local.tags
  flow_log_destination = module.baseline.log_bucket_arn
}

module "identity" {
  source                 = "../../modules/identity"
  name                   = local.name
  allowed_regions        = [var.region]
  trusted_principal_arns = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
  tags                   = local.tags
}
