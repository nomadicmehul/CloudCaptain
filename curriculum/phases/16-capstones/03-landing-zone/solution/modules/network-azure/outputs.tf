output "network_id" {
  description = "ID of the virtual network."
  value       = azurerm_virtual_network.this.id
}

output "cidr_block" {
  description = "Address space of the virtual network."
  value       = var.cidr_block
}

output "private_subnet_ids" {
  description = "Private subnet IDs, one per zone, in zone order."
  value       = azurerm_subnet.private[*].id
}

output "private_subnet_cidrs" {
  description = "Private subnet ranges: cidrsubnet(cidr_block, 4, i) for zone i."
  value       = local.private_subnet_cidrs
}

output "public_subnet_ids" {
  description = "Public subnet IDs for load balancers, one per zone."
  value       = azurerm_subnet.public[*].id
}

output "public_subnet_cidrs" {
  description = "Public subnet ranges: cidrsubnet(cidr_block, 4, 8 + i) for zone i."
  value       = local.public_subnet_cidrs
}

output "public_ingress_ports" {
  description = "Ports reachable from the internet; the landing zone allows only 443."
  value       = [for rule in local.public_ingress : rule.port]
}

