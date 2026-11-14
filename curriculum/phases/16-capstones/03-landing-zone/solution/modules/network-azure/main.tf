locals {
  private_subnet_cidrs = [for index, zone in var.zones : cidrsubnet(var.cidr_block, 4, index)]
  public_subnet_cidrs  = [for index, zone in var.zones : cidrsubnet(var.cidr_block, 4, 8 + index)]

  public_ingress = [
    { name = "lb-https", port = 443, protocol = "Tcp", cidr = "0.0.0.0/0" },
  ]
}

resource "azurerm_resource_group" "this" {
  name     = "${var.name}-network"
  location = var.region
  tags     = var.tags
}

resource "azurerm_virtual_network" "this" {
  name                = var.name
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  address_space       = [var.cidr_block]
  tags                = var.tags
}

resource "azurerm_subnet" "private" {
  count                           = length(var.zones)
  name                            = "${var.name}-private-${count.index}"
  resource_group_name             = azurerm_resource_group.this.name
  virtual_network_name            = azurerm_virtual_network.this.name
  address_prefixes                = [local.private_subnet_cidrs[count.index]]
  default_outbound_access_enabled = false
}

resource "azurerm_subnet" "public" {
  count                           = length(var.zones)
  name                            = "${var.name}-public-${count.index}"
  resource_group_name             = azurerm_resource_group.this.name
  virtual_network_name            = azurerm_virtual_network.this.name
  address_prefixes                = [local.public_subnet_cidrs[count.index]]
  default_outbound_access_enabled = false
}

resource "azurerm_network_security_group" "public" {
  name                = "${var.name}-lb"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  tags                = merge(var.tags, { role = "load-balancer" })

  dynamic "security_rule" {
    for_each = local.public_ingress
    content {
      name                       = security_rule.value.name
      priority                   = 100 + security_rule.key
      direction                  = "Inbound"
      access                     = "Allow"
      protocol                   = security_rule.value.protocol
      source_port_range          = "*"
      destination_port_range     = tostring(security_rule.value.port)
      source_address_prefix      = security_rule.value.cidr
      destination_address_prefix = "*"
    }
  }
}

resource "azurerm_network_security_group" "private" {
  name                = "${var.name}-app"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  tags                = merge(var.tags, { role = "app" })

  security_rule {
    name                       = "from-load-balancers"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "8443"
    source_address_prefixes    = local.public_subnet_cidrs
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "public" {
  count                     = length(var.zones)
  subnet_id                 = azurerm_subnet.public[count.index].id
  network_security_group_id = azurerm_network_security_group.public.id
}

resource "azurerm_subnet_network_security_group_association" "private" {
  count                     = length(var.zones)
  subnet_id                 = azurerm_subnet.private[count.index].id
  network_security_group_id = azurerm_network_security_group.private.id
}

resource "azurerm_network_watcher_flow_log" "this" {
  name                 = "${var.name}-flow-log"
  network_watcher_name = "NetworkWatcher_${var.region}"
  resource_group_name  = "NetworkWatcherRG"
  target_resource_id   = azurerm_virtual_network.this.id
  storage_account_id   = var.flow_log_destination
  enabled              = true
  tags                 = var.tags

  retention_policy {
    enabled = true
    days    = 90
  }
}
