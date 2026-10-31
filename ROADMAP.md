# Roadmap

CloudCaptain is becoming a **from-scratch curriculum for Cloud, DevOps and AIOps**: build the tool, use the tool, ship an artifact. The existing guides, cheat sheets, interview questions and exam prep stay as the Reference layer at `/docs`; lessons live at `/learn`.

Phases are numbered once and never renumbered. A phase appears on the site as soon as it has one published lesson; until then it is only listed here.

## Phases

<!-- curriculum:start -->
| # | Phase | Lessons live | Status |
|---|---|---|---|
| 00 | Setup & Lab | 0 | planned |
| 01 | Linux & Systems | 0 | planned |
| 02 | Networking | 0 | planned |
| 03 | Git & Collaboration | 0 | planned |
| 04 | Scripting & Automation | 0 | planned |
| 05 | Containers | 1 | in progress |
| 06 | Orchestration | 0 | planned |
| 07 | Infrastructure as Code | 0 | planned |
| 08 | CI/CD & GitOps | 0 | planned |
| 09 | Cloud Providers | 0 | planned |
| 10 | Observability & SRE | 0 | planned |
| 11 | Security & DevSecOps | 0 | planned |
| 12 | Platform Engineering | 0 | planned |
| 13 | AI Infrastructure | 0 | planned |
| 14 | AIOps & Agentic Ops | 0 | planned |
| 15 | MLOps & LLMOps | 0 | planned |
| 16 | Capstone Projects | 0 | planned |
<!-- curriculum:end -->

Regenerate the table with `node scripts/build-curriculum.mjs --roadmap`.

## Release plan

| Milestone | Target | Contents |
|---|---|---|
| Scaffold | Nov 2026 | Lesson template, validator, `/learn` site integration, first proof lessons, agent tutor skills |
| Soft launch | Feb 2027 | Phases 00, 01, 05 and the first half of 14 (AIOps & Agentic Ops), ~55 lessons, shared with the community for feedback |
| Public launch | Apr 2027 | Phases 00, 01, 05, 06, 07, 09, 13, 14, ~115 lessons, plus 21 cheat sheets, 19 interview sets and 5 exam-prep guides |
| After launch | weekly | Remaining phases one lesson at a time; monthly "what changed in Cloud/DevOps/AIOps" post; book edition once 200+ lessons exist |

## What "AIOps & Agentic Ops" will cover

An MCP server for `kubectl` and Terraform built from scratch, an incident-triage agent with human-in-the-loop approval, AI-assisted log analysis, LLM observability with OpenTelemetry GenAI conventions, guardrails and sandboxes for agents that touch production, and evaluations that tell you whether an ops agent is actually safe to run.

## How to help

Open an issue with the lesson you want, or pick a `help wanted` issue. Every lesson follows [`LESSON_TEMPLATE.md`](./LESSON_TEMPLATE.md); the validator tells you exactly what is missing.
