mock_provider "google" {}

variables {
  name                 = "lz-dev"
  env                  = "dev"
  region               = "europe-west3"
  cidr_block           = "10.1.0.0/16"
  zones                = ["europe-west3-a", "europe-west3-b"]
  flow_log_destination = "lz-dev-log-archive"
  tags = {
    owner         = "platform-team"
    env           = "dev"
    "cost-center" = "cc-1042"
  }
}

run "subnets_are_cut_from_the_network_range" {
  command = plan

  assert {
    condition     = google_compute_subnetwork.private[0].ip_cidr_range == "10.1.0.0/20" && google_compute_subnetwork.private[1].ip_cidr_range == "10.1.16.0/20"
    error_message = "private subnets must be cidrsubnet(cidr_block, 4, i)"
  }

  assert {
    condition     = google_compute_subnetwork.public[0].ip_cidr_range == "10.1.128.0/20" && google_compute_subnetwork.public[1].ip_cidr_range == "10.1.144.0/20"
    error_message = "public subnets must be cidrsubnet(cidr_block, 4, 8 + i)"
  }
}

run "every_subnet_logs_flows" {
  command = plan

  assert {
    condition     = alltrue([for subnet in concat(google_compute_subnetwork.private, google_compute_subnetwork.public) : length(subnet.log_config) == 1])
    error_message = "every subnet needs a log_config block so flow logs are on"
  }

  assert {
    condition     = google_logging_project_sink.flow_logs.destination == "storage.googleapis.com/lz-dev-log-archive"
    error_message = "flow logs must land in the flow_log_destination bucket"
  }
}

run "only_https_is_open_to_the_internet" {
  command = plan

  assert {
    condition = alltrue([
      for rule in google_compute_firewall.public : contains(rule.source_ranges, "0.0.0.0/0") ? jsonencode(one(rule.allow).ports) == jsonencode(["443"]) && one(rule.allow).protocol == "tcp" : true
    ])
    error_message = "the only rule open to 0.0.0.0/0 must allow tcp 443"
  }

  assert {
    condition     = jsonencode(output.public_ingress_ports) == jsonencode([443])
    error_message = "public_ingress_ports must be [443]"
  }

  assert {
    condition     = length(coalesce(google_compute_firewall.internal.source_ranges, [])) == 0
    error_message = "the internal rule must not take a source range"
  }
}

run "rejects_ranges_outside_the_allocation" {
  command = plan

  variables {
    cidr_block = "192.168.0.0/16"
  }

  expect_failures = [var.cidr_block]
}
