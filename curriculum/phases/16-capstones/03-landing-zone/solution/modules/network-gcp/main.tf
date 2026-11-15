locals {
  private_subnet_cidrs = [for index, zone in var.zones : cidrsubnet(var.cidr_block, 4, index)]
  public_subnet_cidrs  = [for index, zone in var.zones : cidrsubnet(var.cidr_block, 4, 8 + index)]
  tag_summary          = join(", ", [for key in sort(keys(var.tags)) : "${key}=${var.tags[key]}"])

  public_ingress = [
    { name = "lb-https", port = 443, protocol = "tcp", cidr = "0.0.0.0/0" },
  ]
}

resource "google_compute_network" "this" {
  name                    = var.name
  description             = "Landing zone network (${local.tag_summary})"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

resource "google_compute_subnetwork" "private" {
  count                    = length(var.zones)
  name                     = "${var.name}-private-${count.index}"
  description              = "Private subnet for ${var.zones[count.index]}"
  network                  = google_compute_network.this.id
  region                   = var.region
  ip_cidr_range            = local.private_subnet_cidrs[count.index]
  private_ip_google_access = true

  log_config {
    aggregation_interval = "INTERVAL_1_MIN"
    flow_sampling        = 1.0
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_subnetwork" "public" {
  count         = length(var.zones)
  name          = "${var.name}-public-${count.index}"
  description   = "Load balancer subnet for ${var.zones[count.index]}"
  network       = google_compute_network.this.id
  region        = var.region
  ip_cidr_range = local.public_subnet_cidrs[count.index]

  log_config {
    aggregation_interval = "INTERVAL_1_MIN"
    flow_sampling        = 1.0
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_firewall" "public" {
  for_each      = { for rule in local.public_ingress : rule.name => rule }
  name          = "${var.name}-${each.key}"
  network       = google_compute_network.this.id
  direction     = "INGRESS"
  priority      = 1000
  source_ranges = [each.value.cidr]
  target_tags   = ["load-balancer"]

  allow {
    protocol = each.value.protocol
    ports    = [tostring(each.value.port)]
  }

  log_config {
    metadata = "INCLUDE_ALL_METADATA"
  }
}

resource "google_compute_firewall" "internal" {
  name        = "${var.name}-from-load-balancers"
  network     = google_compute_network.this.id
  direction   = "INGRESS"
  priority    = 1000
  source_tags = ["load-balancer"]
  target_tags = ["app"]

  allow {
    protocol = "tcp"
    ports    = ["8443"]
  }
}

resource "google_compute_firewall" "deny_ingress" {
  name          = "${var.name}-deny-ingress"
  network       = google_compute_network.this.id
  direction     = "INGRESS"
  priority      = 65000
  source_ranges = ["0.0.0.0/0"]

  deny {
    protocol = "all"
  }
}

resource "google_logging_project_sink" "flow_logs" {
  name                   = "${var.name}-flow-logs"
  destination            = "storage.googleapis.com/${var.flow_log_destination}"
  filter                 = "resource.type=\"gce_subnetwork\" AND log_id(\"compute.googleapis.com/vpc_flows\")"
  unique_writer_identity = true
}
