data "aws_caller_identity" "current" {}

locals {
  env  = "hub"
  name = "lz-${local.env}"
  tags = {
    owner         = var.owner
    env           = local.env
    "cost-center" = var.cost_center
  }
  spoke_vpc_ids = { for env, found in data.aws_vpcs.spoke : env => found.ids[0] if length(found.ids) == 1 }
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

data "aws_vpcs" "spoke" {
  for_each = toset(var.spoke_envs)

  tags = {
    env  = each.key
    Name = "lz-${each.key}"
  }
}

data "aws_vpc" "spoke" {
  for_each = local.spoke_vpc_ids
  id       = each.value
}

resource "aws_vpc_peering_connection" "spoke" {
  for_each    = local.spoke_vpc_ids
  vpc_id      = module.network.network_id
  peer_vpc_id = each.value
  auto_accept = true
  tags        = merge(local.tags, { Name = "lz-hub-to-${each.key}" })
}

resource "aws_route" "hub_to_spoke" {
  for_each                  = local.spoke_vpc_ids
  route_table_id            = module.network.route_table_id
  destination_cidr_block    = data.aws_vpc.spoke[each.key].cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.spoke[each.key].id
}

resource "aws_route" "spoke_to_hub" {
  for_each                  = local.spoke_vpc_ids
  route_table_id            = data.aws_vpc.spoke[each.key].main_route_table_id
  destination_cidr_block    = module.network.cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.spoke[each.key].id
}
