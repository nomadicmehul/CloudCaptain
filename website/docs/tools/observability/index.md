---
title: "Observability & SRE"
description: "Monitoring, observability, and SRE practices for reliable systems"
---

# Observability & SRE

Prometheus, Grafana, and SRE practices for reliable systems.

## Core Concepts

| Concept | Description |
|:--------|:-----------|
| **Monitoring** | Collecting, processing, and aggregating metrics to understand system health |
| **Observability** | Ability to understand internal state from external outputs (metrics, logs, traces) |
| **Alerting** | Notifying humans when systems need attention |
| **SLOs/SLIs** | Measuring and targeting reliability with Service Level Objectives and Indicators |
| **Incident Management** | Detecting, responding to, and learning from outages |

## Key Tools

| Tool | Purpose | Link |
|:-----|:--------|:-----|
| Prometheus | Metrics collection and alerting | [prometheus.io](https://prometheus.io/) |
| Grafana | Dashboards and visualization | [grafana.com](https://grafana.com/) |
| Alertmanager | Alert routing, grouping, and silencing | [prometheus.io/docs/alerting](https://prometheus.io/docs/alerting/latest/alertmanager/) |
| Jaeger | Distributed tracing | [jaegertracing.io](https://www.jaegertracing.io/) |
| OpenTelemetry | Unified observability framework | [opentelemetry.io](https://opentelemetry.io/) |
| PagerDuty | Incident response and on-call | [pagerduty.com](https://www.pagerduty.com/) |

## Google SRE Book — Observability Chapters

These chapters from the free [Google SRE Book](https://sre.google/sre-book/table-of-contents/) are directly relevant to observability and monitoring:

| Ch | Title | Why It Matters |
|:---|:------|:---------------|
| 4 | [Service Level Objectives](https://sre.google/sre-book/service-level-objectives/) | Foundation for what to monitor and alert on |
| 6 | [Monitoring Distributed Systems](https://sre.google/sre-book/monitoring-distributed-systems/) | Core monitoring philosophy and approach |
| 10 | [Practical Alerting](https://sre.google/sre-book/practical-alerting/) | How to build effective alerting systems |
| 12 | [Effective Troubleshooting](https://sre.google/sre-book/effective-troubleshooting/) | Using observability data to diagnose issues |
| 15 | [Postmortem Culture](https://sre.google/sre-book/postmortem-culture/) | Learning from failures to improve monitoring |
| 16 | [Tracking Outages](https://sre.google/sre-book/tracking-outages/) | Aggregating and analyzing incident data |
| 17 | [Testing for Reliability](https://sre.google/sre-book/testing-reliability/) | Validating monitoring and alerting in production |

> See the full [SRE Learning Path](/docs/learning-paths/sre) for all 34 chapters.

## Further Reading

- [SRE Learning Path](/docs/learning-paths/sre) — Complete roadmap with all Google SRE Book chapters
- [SRE Practices Guide](/docs/learning-paths/sre-practices) — Deep dive into SLOs, incident management, postmortems

## Contributing

Know great Observability & SRE resources? Submit a PR to help the community learn!
