output "vpc_id" {
  description = "VPC of this environment."
  value       = module.network.network_id
}

output "private_subnet_ids" {
  description = "Private subnets, one per zone."
  value       = module.network.private_subnet_ids
}

output "role_arns" {
  description = "Deploy and read-only roles."
  value       = module.identity.role_arns
}

output "log_bucket_arn" {
  description = "Log archive bucket that receives the flow logs."
  value       = module.baseline.log_bucket_arn
}
