---
title: "Observability Cheatsheet"
description: "Quick reference guide for PromQL expressions, common exporters, and alerting rules"
sidebar_label: "Cheatsheet"
sidebar_position: 3
---

# Observability Cheatsheet

Quick reference for PromQL queries, standard metrics, and Alertmanager rules.

## Common PromQL Expressions

| Use Case | Query |
| :--- | :--- |
| **Request Rate** | `sum(rate(http_requests_total[5m])) by (status)` |
| **Error Rate Percentage** | `sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m])) * 100` |
| **Memory Usage %** | `(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100` |
| **Disk Space Usage %** | `(1 - (node_filesystem_free_bytes / node_filesystem_size_bytes)) * 100` |

## Standard Exporters and Ports

* `node_exporter`: Port `9100` (host hardware and OS metrics)
* `blackbox_exporter`: Port `9115` (HTTP/HTTPS, DNS, TCP probing)
* `mysqld_exporter`: Port `9104` (MySQL server metrics)
* `postgres_exporter`: Port `9187` (PostgreSQL server metrics)
