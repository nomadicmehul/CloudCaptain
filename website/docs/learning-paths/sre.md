---
sidebar_position: 7
title: "SRE Learning Path"
description: "Site Reliability Engineering roadmap with Google SRE Book chapter guide"
---

# SRE Learning Path

Build and operate reliable, scalable, and efficient systems.

## Core SRE Principles

- **SLOs, SLIs, SLAs** — Define and measure reliability
- **Error Budgets** — Balance reliability with velocity
- **Toil Reduction** — Automate operational work
- **Incident Management** — Respond effectively to outages
- **Blameless Postmortems** — Learn from failures

## Stage 1: Foundations

- Linux system administration
- Networking and distributed systems
- Programming (Python/Go)
- [Observability stack](/docs/tools/observability/)

## Stage 2: Monitoring & Alerting

- [Prometheus](/docs/tools/observability/) metrics and queries
- Grafana dashboards
- Alert design and routing
- On-call best practices

## Stage 3: Reliability Practices

- Capacity planning
- Load testing and performance
- Chaos Engineering
- Disaster recovery and DR drills
- Change management

---

## Google SRE Book — Free Chapter Guide

The **Site Reliability Engineering** book by Google is available for free online. Below is the complete table of contents with direct links to every chapter.

> All chapters link to [sre.google/sre-book](https://sre.google/sre-book/table-of-contents/) — read the full book for free.

### Part I — Introduction

| Ch | Title | Key Topics |
|:---|:------|:-----------|
| 1 | [Introduction](https://sre.google/sre-book/introduction/) | What SRE is, how Google does it, tenets of SRE |
| 2 | [The Production Environment at Google](https://sre.google/sre-book/production-environment/) | Hardware, software infrastructure, networking, Borg |

### Part II — Principles

| Ch | Title | Key Topics |
|:---|:------|:-----------|
| 3 | [Embracing Risk](https://sre.google/sre-book/embracing-risk/) | Risk tolerance, error budgets, balancing reliability vs. velocity |
| 4 | [Service Level Objectives](https://sre.google/sre-book/service-level-objectives/) | SLIs, SLOs, SLAs, choosing targets, control loops |
| 5 | [Eliminating Toil](https://sre.google/sre-book/eliminating-toil/) | Defining toil, measuring toil, automation strategies |
| 6 | [Monitoring Distributed Systems](https://sre.google/sre-book/monitoring-distributed-systems/) | Symptoms vs. causes, black-box/white-box monitoring, alerting |
| 7 | [The Evolution of Automation at Google](https://sre.google/sre-book/automation-at-google/) | Automation hierarchy, platform-based automation |
| 8 | [Release Engineering](https://sre.google/sre-book/release-engineering/) | Build/release pipeline, hermetic builds, release philosophy |
| 9 | [Simplicity](https://sre.google/sre-book/simplicity/) | Boring is good, minimal APIs, modularity, release simplicity |

### Part III — Practices

| Ch | Title | Key Topics |
|:---|:------|:-----------|
| 10 | [Practical Alerting](https://sre.google/sre-book/practical-alerting/) | Borgmon, time-series monitoring, alerting rules, dashboards |
| 11 | [Being On-Call](https://sre.google/sre-book/being-on-call/) | On-call balance, compensation, operational load, feeling safe |
| 12 | [Effective Troubleshooting](https://sre.google/sre-book/effective-troubleshooting/) | Problem reports, triage, examine, diagnose, test/treat |
| 13 | [Emergency Response](https://sre.google/sre-book/emergency-response/) | Real-world emergencies, learning from incidents, drills |
| 14 | [Managing Incidents](https://sre.google/sre-book/managing-incidents/) | Incident command, roles, communication, declared incidents |
| 15 | [Postmortem Culture: Learning from Failure](https://sre.google/sre-book/postmortem-culture/) | Blameless postmortems, collaboration, continuous improvement |
| 16 | [Tracking Outages](https://sre.google/sre-book/tracking-outages/) | Escalator, Outalator, aggregation, tagging, reporting |
| 17 | [Testing for Reliability](https://sre.google/sre-book/testing-reliability/) | Unit/integration/system testing, production tests, canary |
| 18 | [Software Engineering in SRE](https://sre.google/sre-book/software-engineering-in-sre/) | SRE as software engineers, Auxon case study |
| 19 | [Load Balancing at the Frontend](https://sre.google/sre-book/load-balancing-frontend/) | DNS load balancing, virtual IPs, anycast |
| 20 | [Load Balancing in the Datacenter](https://sre.google/sre-book/load-balancing-datacenter/) | Subset selection, backend health, weighted round robin |
| 21 | [Handling Overload](https://sre.google/sre-book/handling-overload/) | Client-side throttling, criticality, graceful degradation |
| 22 | [Addressing Cascading Failures](https://sre.google/sre-book/addressing-cascading-failures/) | Causes, prevention, mitigation, resource exhaustion |
| 23 | [Managing Critical State](https://sre.google/sre-book/managing-critical-state/) | Distributed consensus, Paxos, Raft, leader election |
| 24 | [Distributed Periodic Scheduling](https://sre.google/sre-book/distributed-periodic-scheduling/) | Cron at scale, idempotency, large-scale scheduling |
| 25 | [Data Processing Pipelines](https://sre.google/sre-book/data-processing-pipelines/) | Pipeline design patterns, Workflow, periodic pipelines |
| 26 | [Data Integrity](https://sre.google/sre-book/data-integrity/) | Backups, recovery, replication, ACID, soft deletes |
| 27 | [Reliable Product Launches at Scale](https://sre.google/sre-book/reliable-product-launches/) | Launch coordination, checklists, progressive rollouts |

### Part IV — Management

| Ch | Title | Key Topics |
|:---|:------|:-----------|
| 28 | [Accelerating SREs to On-Call](https://sre.google/sre-book/accelerating-sre-on-call/) | Training, reverse engineering, shadowing, onboarding |
| 29 | [Dealing with Interrupts](https://sre.google/sre-book/dealing-with-interrupts/) | Interrupt management, flow state, operational vs. project work |
| 30 | [Embedding an SRE to Recover from Operational Overload](https://sre.google/sre-book/operational-overload/) | Team health, SRE embedding, operational recovery |
| 31 | [Communication and Collaboration in SRE](https://sre.google/sre-book/communication-and-collaboration/) | Production meetings, collaboration models, knowledge sharing |
| 32 | [The Evolving SRE Engagement Model](https://sre.google/sre-book/evolving-sre-engagement-model/) | PRR model, early engagement, frameworks, SRE teams |

### Part V — Conclusions

| Ch | Title | Key Topics |
|:---|:------|:-----------|
| 33 | [Lessons Learned from Other Industries](https://sre.google/sre-book/lessons-learned/) | Aviation, healthcare, nuclear — parallels to SRE |
| 34 | [Conclusion](https://sre.google/sre-book/conclusion/) | SRE future, key takeaways |

### Appendices

| ID | Title | Description |
|:---|:------|:-----------|
| A | [Availability Table](https://sre.google/sre-book/availability-table/) | Nines of availability with downtime calculations |
| B | [Best Practices for Production Services](https://sre.google/sre-book/service-best-practices/) | Collected best practices checklist |
| C | [Example Incident State Document](https://sre.google/sre-book/incident-document/) | Template for tracking incidents |
| D | [Example Postmortem](https://sre.google/sre-book/example-postmortem/) | Full postmortem example from Shakespeare service |
| E | [Launch Coordination Checklist](https://sre.google/sre-book/launch-checklist/) | Pre-launch and launch-day checklist |
| F | [Example Production Meeting Minutes](https://sre.google/sre-book/production-meeting/) | How Google runs production review meetings |

---

## Recommended Reading

| Book | Author |
|:-----|:-------|
| [Site Reliability Engineering](https://sre.google/sre-book/table-of-contents/) | Google (Betsy Beyer et al.) — Free online |
| [The Site Reliability Workbook](https://sre.google/workbook/table-of-contents/) | Google (Betsy Beyer et al.) — Free online |
| [Building Secure and Reliable Systems](https://sre.google/books/building-secure-reliable-systems/) | Google (Heather Adkins et al.) — Free online |
| Implementing Service Level Objectives | Alex Hidalgo |
| Observability Engineering | Charity Majors et al. |

All SRE content from these books has been integrated into the [SRE Practices](/docs/learning-paths/sre-practices) guide.
