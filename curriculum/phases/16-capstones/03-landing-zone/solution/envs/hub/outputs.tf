output "vpc_id" {
  description = "Hub VPC."
  value       = module.network.network_id
}

output "peering_connection_ids" {
  description = "Peering connections to the spokes that existed at the last apply."
  value       = { for env, peering in aws_vpc_peering_connection.spoke : env => peering.id }
}

output "role_arns" {
  description = "Deploy and read-only roles of the hub."
  value       = module.identity.role_arns
}
