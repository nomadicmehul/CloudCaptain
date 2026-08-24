---
title: "Observability Fundamentals"
description: "Core concepts of metrics, logs, traces, Prometheus architecture, and Grafana visualization"
sidebar_label: "Fundamentals"
sidebar_position: 2
---

# Observability Fundamentals

Observability is the practice of understanding the internal state of complex systems based on their external outputs (telemetry).

## The Three Pillars of Observability

1. **Metrics**: Aggregable numerical measurements recorded over time intervals (counters, gauges, histograms).
2. **Logs**: Timestamped structured or unstructured event records emitted during execution.
3. **Traces**: End-to-end request journeys tracked across distributed microservice boundaries using span identifiers.

## Prometheus Architecture

Prometheus is a time-series monitoring system utilizing a pull-based scraping model:

* **Prometheus Server**: Scrapes metrics over HTTP endpoints (typically `/metrics`) and stores them in a local time-series database.
* **Exporters**: Lightweight adapters translating third-party metrics into Prometheus format (e.g., `node_exporter`, `blackbox_exporter`).
* **Pushgateway**: Intermediary component for ephemeral or batch jobs.
* **Alertmanager**: Handles alerts sent by Prometheus, deduplicating, grouping, and routing notifications to receivers (Slack, PagerDuty, email).

## PromQL Basics

Prometheus Query Language (PromQL) provides real-time metric selection and aggregation:

```promql
# Instant rate of HTTP requests per second over 5 minutes
rate(http_requests_total[5m])

# 99th percentile request latency
histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))

# CPU utilization percentage per instance
100 - (avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)
```

## Grafana Visualization

Grafana provides visualization dashboards connected to Prometheus, Elasticsearch, and Loki data sources, supporting dynamic variables, panel alerts, and unified dashboard templating.
