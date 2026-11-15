output "network_id" {
  description = "ID of the VPC."
  value       = aws_vpc.this.id
}

output "cidr_block" {
  description = "IPv4 range of the VPC."
  value       = aws_vpc.this.cidr_block
}

output "private_subnet_ids" {
  description = "Private subnet IDs, one per zone, in zone order."
  value       = aws_subnet.private[*].id
}

output "private_subnet_cidrs" {
  description = "Private subnet ranges: cidrsubnet(cidr_block, 4, i) for zone i."
  value       = local.private_subnet_cidrs
}

output "public_subnet_ids" {
  description = "Public subnet IDs for load balancers, one per zone."
  value       = aws_subnet.public[*].id
}

output "public_subnet_cidrs" {
  description = "Public subnet ranges: cidrsubnet(cidr_block, 4, 8 + i) for zone i."
  value       = local.public_subnet_cidrs
}

output "public_ingress_ports" {
  description = "Ports reachable from the internet; the landing zone allows only 443."
  value       = [for rule in local.public_ingress : rule.port]
}

output "route_table_id" {
  description = "Main route table of the VPC, used by the private subnets."
  value       = aws_vpc.this.main_route_table_id
}

output "lb_security_group_id" {
  description = "Security group for load balancers."
  value       = aws_security_group.lb.id
}

output "app_security_group_id" {
  description = "Security group for workloads behind the load balancers."
  value       = aws_security_group.app.id
}
