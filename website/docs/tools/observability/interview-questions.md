---
title: "Observability Interview Questions"
description: "Common DevOps and SRE interview questions covering monitoring, Prometheus, and SLOs"
sidebar_label: "Interview Questions"
sidebar_position: 4
---

# Observability Interview Questions

Essential questions and concepts for site reliability engineering (SRE) and DevOps interviews.

## 1. What is the difference between monitoring and observability?
* **Monitoring** tells you *when* a system is failing based on predefined thresholds and known failure modes.
* **Observability** provides sufficient contextual telemetry to determine *why* a system is failing, including novel, unexpected failure modes.

## 2. What are SLIs, SLOs, and SLAs?
* **SLI (Service Level Indicator)**: A measurable metric of service performance (e.g., latency under 200ms).
* **SLO (Service Level Objective)**: Target reliability goal agreed internally by engineering teams (e.g., 99.9% of requests meet the SLI).
* **SLA (Service Level Agreement)**: A formal commitment with external clients defining penalties if SLO targets are breached.

## 3. Explain push vs. pull monitoring architectures.
* **Pull (Prometheus)**: Central server initiates requests to target endpoints. Facilitates target discovery, health checks, and simplifies scraping control.
* **Push (StatsD / Graphite)**: Targets emit metrics to a central collector. Well suited for short-lived batch jobs, though vulnerable to overwhelming collectors during traffic spikes.
