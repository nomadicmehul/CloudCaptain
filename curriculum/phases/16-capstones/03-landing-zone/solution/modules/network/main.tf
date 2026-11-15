locals {
  private_subnet_cidrs = [for index, zone in var.zones : cidrsubnet(var.cidr_block, 4, index)]
  public_subnet_cidrs  = [for index, zone in var.zones : cidrsubnet(var.cidr_block, 4, 8 + index)]

  public_ingress = [
    { name = "lb-https", port = 443, protocol = "tcp", cidr = "0.0.0.0/0" },
  ]
}

resource "aws_vpc" "this" {
  cidr_block           = var.cidr_block
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(var.tags, { Name = var.name })
}

resource "aws_subnet" "private" {
  count                   = length(var.zones)
  vpc_id                  = aws_vpc.this.id
  cidr_block              = local.private_subnet_cidrs[count.index]
  availability_zone       = var.zones[count.index]
  map_public_ip_on_launch = false
  tags                    = merge(var.tags, { Name = "${var.name}-private-${var.zones[count.index]}", tier = "private" })
}

resource "aws_subnet" "public" {
  count                   = length(var.zones)
  vpc_id                  = aws_vpc.this.id
  cidr_block              = local.public_subnet_cidrs[count.index]
  availability_zone       = var.zones[count.index]
  map_public_ip_on_launch = false
  tags                    = merge(var.tags, { Name = "${var.name}-public-${var.zones[count.index]}", tier = "public" })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name}-igw" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name}-public", tier = "public" })
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  count          = length(var.zones)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_default_security_group" "this" {
  vpc_id = aws_vpc.this.id
  tags   = merge(var.tags, { Name = "${var.name}-default-closed" })
}

resource "aws_security_group" "lb" {
  name        = "${var.name}-lb"
  description = "Load balancers: HTTPS from the internet"
  vpc_id      = aws_vpc.this.id

  dynamic "ingress" {
    for_each = local.public_ingress
    content {
      description = ingress.value.name
      from_port   = ingress.value.port
      to_port     = ingress.value.port
      protocol    = ingress.value.protocol
      cidr_blocks = [ingress.value.cidr]
    }
  }

  egress {
    description = "To the workloads in this network"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.cidr_block]
  }

  tags = merge(var.tags, { Name = "${var.name}-lb", role = "load-balancer" })
}

resource "aws_security_group" "app" {
  name        = "${var.name}-app"
  description = "Workloads: only from the load balancers"
  vpc_id      = aws_vpc.this.id

  ingress {
    description     = "From the load balancers"
    from_port       = 8443
    to_port         = 8443
    protocol        = "tcp"
    security_groups = [aws_security_group.lb.id]
  }

  egress {
    description = "HTTPS to anywhere"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${var.name}-app", role = "app" })
}

resource "aws_flow_log" "this" {
  vpc_id                   = aws_vpc.this.id
  traffic_type             = "ALL"
  log_destination_type     = "s3"
  log_destination          = var.flow_log_destination
  max_aggregation_interval = 60
  tags                     = merge(var.tags, { Name = "${var.name}-flow-log" })
}
