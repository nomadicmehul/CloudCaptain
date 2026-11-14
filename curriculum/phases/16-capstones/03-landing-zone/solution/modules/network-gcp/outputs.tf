output "network_id" {
  description = "ID of the VPC network."
  value       = google_compute_network.this.id
}

output "cidr_block" {
  description = "IPv4 range the subnets are cut from."
  value       = var.cidr_block
}

output "private_subnet_ids" {
  description = "Private subnet IDs, one per zone, in zone order."
  value       = google_compute_subnetwork.private[*].id
}

output "private_subnet_cidrs" {
  description = "Private subnet ranges: cidrsubnet(cidr_block, 4, i) for zone i."
  value       = local.private_subnet_cidrs
}

output "public_subnet_ids" {
  description = "Public subnet IDs for load balancers, one per zone."
  value       = google_compute_subnetwork.public[*].id
}

output "public_subnet_cidrs" {
  description = "Public subnet ranges: cidrsubnet(cidr_block, 4, 8 + i) for zone i."
  value       = local.public_subnet_cidrs
}

output "public_ingress_ports" {
  description = "Ports reachable from the internet; the landing zone allows only 443."
  value       = [for rule in local.public_ingress : rule.port]
}

