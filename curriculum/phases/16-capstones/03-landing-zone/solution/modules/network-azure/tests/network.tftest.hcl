mock_provider "azurerm" {
  mock_resource "azurerm_resource_group" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/lz-dev-network"
    }
  }

  mock_resource "azurerm_virtual_network" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/lz-dev-network/providers/Microsoft.Network/virtualNetworks/lz-dev"
    }
  }

  mock_resource "azurerm_subnet" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/lz-dev-network/providers/Microsoft.Network/virtualNetworks/lz-dev/subnets/subnet"
    }
  }

  mock_resource "azurerm_network_security_group" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/lz-dev-network/providers/Microsoft.Network/networkSecurityGroups/nsg"
    }
  }
}

variables {
  name                 = "lz-dev"
  env                  = "dev"
  region               = "germanywestcentral"
  cidr_block           = "10.1.0.0/16"
  zones                = ["1", "2"]
  flow_log_destination = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/lz-logs/providers/Microsoft.Storage/storageAccounts/lzdevlogarchive"
  tags = {
    owner         = "platform-team"
    env           = "dev"
    "cost-center" = "cc-1042"
  }
}

run "subnets_are_cut_from_the_address_space" {
  command = plan

  assert {
    condition     = azurerm_subnet.private[0].address_prefixes[0] == "10.1.0.0/20" && azurerm_subnet.private[1].address_prefixes[0] == "10.1.16.0/20"
    error_message = "private subnets must be cidrsubnet(cidr_block, 4, i)"
  }

  assert {
    condition     = azurerm_subnet.public[0].address_prefixes[0] == "10.1.128.0/20" && azurerm_subnet.public[1].address_prefixes[0] == "10.1.144.0/20"
    error_message = "public subnets must be cidrsubnet(cidr_block, 4, 8 + i)"
  }

  assert {
    condition     = jsonencode(azurerm_virtual_network.this.address_space) == jsonencode(["10.1.0.0/16"])
    error_message = "the virtual network must use exactly cidr_block"
  }
}

run "only_https_is_open_to_the_internet" {
  command = plan

  assert {
    condition = alltrue([
      for rule in concat(tolist(azurerm_network_security_group.public.security_rule), tolist(azurerm_network_security_group.private.security_rule)) :
      rule.access != "Allow" || rule.direction != "Inbound" || !contains(["*", "0.0.0.0/0", "Internet"], coalesce(rule.source_address_prefix, "none")) || rule.destination_port_range == "443"
    ])
    error_message = "an inbound allow from the internet may only open port 443"
  }

  assert {
    condition     = jsonencode(output.public_ingress_ports) == jsonencode([443])
    error_message = "public_ingress_ports must be [443]"
  }
}

run "flow_logs_cover_the_virtual_network" {
  command = plan

  assert {
    condition     = azurerm_network_watcher_flow_log.this.enabled && azurerm_network_watcher_flow_log.this.storage_account_id == var.flow_log_destination
    error_message = "the flow log must be enabled and write to flow_log_destination"
  }
}

run "rejects_a_single_zone" {
  command = plan

  variables {
    zones = ["1"]
  }

  expect_failures = [var.zones]
}
