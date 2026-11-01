<div align="center">
  <img src="./website/static/img/cloudcaptain-logo.jpg" width="180" alt="CloudCaptain" />
  <h1>CloudCaptain</h1>
  <h3>Cloud, DevOps &amp; AIOps from scratch.<br/>Build the tool. Use the tool. Ship an artifact.</h3>
  <p>
    <strong>55 lessons</strong> · <strong>8 phases live</strong> · <strong>80 hours</strong> · <strong>1,429 tests</strong> · <strong>107 skills, prompts &amp; MCP servers</strong>
  </p>
  <p>
    <a href="https://cloudcaptain.io/learn"><img src="https://img.shields.io/badge/Start_the_course-cloudcaptain.io%2Flearn-1E9BD7?style=for-the-badge" alt="Start the course" /></a>
  </p>
  <p>
    <a href="https://github.com/nomadicmehul/CloudCaptain/actions/workflows/lesson-smoke.yml"><img src="https://github.com/nomadicmehul/CloudCaptain/actions/workflows/lesson-smoke.yml/badge.svg" alt="Lesson smoke tests" /></a>
    <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT license" /></a>
    <img src="https://img.shields.io/badge/PRs-welcome-blue" alt="PRs welcome" />
    <a href="https://github.com/nomadicmehul/CloudCaptain/stargazers"><img src="https://img.shields.io/github/stars/nomadicmehul/cloudcaptain?style=flat&logo=github" alt="GitHub stars" /></a>
  </p>
  <p>
    <strong>English</strong> ·
    <a href="./i18n/README.hi.md">हिन्दी</a> ·
    <a href="./i18n/README.mr.md">मराठी</a> ·
    <a href="./i18n/README.gu.md">ગુજરાતી</a> ·
    <a href="./i18n/README.de.md">Deutsch</a> ·
    <a href="./i18n/README.es.md">Español</a> ·
    <a href="./i18n/README.pt.md">Português</a> ·
    <a href="./i18n/README.zh.md">中文</a> ·
    <a href="./i18n/README.ja.md">日本語</a>
  </p>
</div>

```bash
npx skills add nomadicmehul/CloudCaptain   # installs the tutor into Claude Code, Cursor, Codex or any agent that reads SKILL.md
```

Then ask your agent to `/captain-start`: a short placement quiz, a learning path, and the first lesson.

---

## What you build

Every lesson starts from a production problem, has you build the mechanism yourself in plain Python or Bash with tests, then do the same job with the real tool, so every flag and every error message maps onto code you wrote.

| Phase | You build | Then you run |
|:--|:--|:--|
| **00 · Setup & Lab** | a lab doctor that probes your machine and prints the install plan | kind, Docker, your coding agent |
| **01 · Linux & Systems** | a PID 1 supervisor, an fd leak detector, an strace analyser, a capability decoder, systemd units, cgroups by hand, a mount table reader, a USE-method dashboard | `strace`, `capsh`, `systemctl`, `findmnt`, PSI |
| **05 · Containers** | a container runtime, an image auditor, a registry client, an image builder without a Dockerfile, a veth/bridge network, Compose, rootless user namespaces, a seccomp generator | Docker, Podman, OCI registries |
| **06 · Orchestration** | a reconciler, a scheduler, a kubelet, kube-proxy rules and cluster DNS, an RBAC authorizer, Helm, an operator, a Gateway API router | Kubernetes on kind, Helm, Envoy Gateway |
| **07 · Infrastructure as Code** | a plan/apply engine, a dependency graph, modules, a locking state server, drift detection, a policy engine, Pulumi and Crossplane front ends, an idempotent config manager | OpenTofu, conftest/OPA, Pulumi, Ansible |
| **09 · Cloud (AWS first)** | an IAM policy evaluator, a VPC reachability analyser, SigV4 signing and an S3 server, the Lambda runtime API, an Auto Scaling group, a FOCUS cost allocator, a security posture scanner | the AWS CLI against moto, the Lambda emulator, Checkov |
| **13 · AI Infrastructure** | a GPU scheduler with DRA, an LLM serving simulator, a token-counting gateway, a queue-based autoscaler, a prefix-cache router, HNSW and IVF indexes, a model artifact inspector, a token cost model | llama.cpp on CPU, LiteLLM, pgvector, llm-d, ORAS |
| **14 · AIOps & Agentic Ops** | an MCP server for kubectl, an incident-triage agent, AI log analysis, OpenTelemetry for LLM calls, agent sandboxes, safe write tools, agent evals | Claude Code, the Claude API, OpenTelemetry, promptfoo |

Nine more phases (networking, Git, scripting, CI/CD, observability, security, platform engineering, MLOps, capstones) are on the [roadmap](./ROADMAP.md).

## How a lesson works

```
The Problem  →  The Concept  →  Build It  →  Use It  →  Ship It  →  Quiz
 a real          one diagram     standard     the real    a skill,    six questions,
 incident                        library +    tool, same  prompt or   before and
                                 tests        job         MCP server  after
```

- **Runnable and tested.** Every lesson has code with unit tests that run anywhere (no root, no Docker, no cloud account) and a `smoke.sh` that runs the Linux-only steps and the real tools on a CI runner with Docker, kind and OpenTofu. A lesson shows "last verified" only after that passes.
- **Current.** Tool versions are tracked weekly, retired technology (ingress-nginx, PodSecurityPolicy, Swarm, Bitnami images) is flagged wherever it appears, and AI lessons use today's models and APIs.
- **Yours to keep.** Each lesson ships an agent skill, a prompt or an MCP server into `outputs/`, so you finish with a toolbox, not just notes.

## Learning paths

| Path | For | Lessons |
|:--|:--|:--|
| [DevOps Engineer](https://cloudcaptain.io/learn/paths/devops) | from a single Linux process to Kubernetes, IaC, cloud and AIOps | 47 |
| [AI Platform Engineer](https://cloudcaptain.io/learn/paths/ai-platform) | engineers asked to run models: GPUs, serving, gateways, routing, cost | 17 |
| [AIOps Engineer](https://cloudcaptain.io/learn/paths/aiops) | putting agents to work on infrastructure safely | 9 |

## The tutor in your agent

`npx skills add nomadicmehul/CloudCaptain` installs four skills: `captain-start` (placement quiz and a plan in `LEARNING.md`), `captain-learn` (one lesson per session, Build It → Use It → Ship It, with the quiz and tests), `captain-guide` (where do I learn X, what does role Y need) and `captain-check` (a Socratic review of a phase). They work in Claude Code, Cursor, Codex and any agent that reads `SKILL.md`.

## The reference library

Alongside the curriculum, [cloudcaptain.io/docs](https://cloudcaptain.io/docs) keeps the long-form guides CloudCaptain started with: 150+ pages of original writing, 21 command cheat sheets, 19 interview question sets, 5 certification exam-prep guides (CKA/CKAD/CKS, Terraform, AWS, Azure, GCP), 7 learning paths and 8 career paths. Lessons link into it rather than repeating it, and the reference pages that a lesson builds on link back with a "Build this from scratch" card.

The site's **Captain's Bridge** reading mode tracks progress per section, and [/progress](https://cloudcaptain.io/progress) remembers lessons, quiz scores and reading in your browser. No account, no tracking.

<div align="center">
  <img src="./website/static/img/captains-bridge-demo.gif" width="760" alt="Captain's Bridge: sections tick off as you read, and /progress remembers what you have read" />
</div>

## Why CloudCaptain

| | CloudCaptain | roadmap.sh | devops-exercises | awesome-* lists |
|:--|:--|:--|:--|:--|
| Build each tool from scratch, with tests | ✅ 55 lessons | ❌ | ❌ | ❌ |
| Verified against the real tools in CI | ✅ every lesson | ❌ | ❌ | ❌ |
| AI infrastructure and AIOps | ✅ 15 lessons | partial | ❌ | links |
| An agent tutor you install | ✅ `npx skills add` | ❌ | ❌ | ❌ |
| Long-form guides, cheat sheets, interview Q&A, exam prep | ✅ | roadmap nodes link out | Q&A | links |
| Reading and quiz progress | ✅ in your browser | ✅ | ❌ | ❌ |

## Run it locally

```bash
git clone https://github.com/nomadicmehul/CloudCaptain.git
cd CloudCaptain/website
npm install
npm start                                  # http://localhost:3000, lessons at /learn
npm run curriculum:test -- linux/pid1-supervisor   # one lesson's tests
```

Each lesson is a folder: `curriculum/phases/NN-phase/NN-lesson/` with `docs/en.md`, `code/` (with tests and `smoke.sh`), `quiz.json` and `outputs/`. The contract is [LESSON_TEMPLATE.md](./LESSON_TEMPLATE.md); the repo map is [AGENTS.md](./AGENTS.md).

## Contributing

Lessons, fixes and translations are all welcome; see [CONTRIBUTING.md](./CONTRIBUTING.md). A new lesson starts with `npm run curriculum:scaffold`, must pass the validator and its tests, and gets verified by the smoke run on its pull request. Outdated content has its own issue template, and every pull request gets a preview deployment.

Teaching with CloudCaptain at a bootcamp, university or company? [FORKING.md](./FORKING.md) explains how to fork and adapt it. To support the work, see [SPONSORS.md](./SPONSORS.md).

## Site traffic

<div align="center">
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-total.json" alt="Total visits" />
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-week.json" alt="Visits this week" />
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-today.json" alt="Visits today" />

  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./.github/stats/chart-dark.svg">
    <img src="./.github/stats/chart-light.svg" width="800" alt="Visitor traffic over the last 90 days" />
  </picture>
  <p><em>Updated daily via <a href="https://www.goatcounter.com">GoatCounter</a>: privacy-friendly, no cookies, no tracking of individuals.</em></p>
</div>

## License

MIT, see [LICENSE](./LICENSE). Use it, fork it, teach with it.

---

<div align="center">
  <p>Built by <a href="https://nomadicmehul.dev/">Mehul Patel</a> and the open-source community · <a href="https://cloudcaptain.io">cloudcaptain.io</a></p>
  <p>If CloudCaptain helped you, <a href="https://github.com/nomadicmehul/CloudCaptain">star the repo</a>: it helps other engineers find a free alternative to paid courses.</p>
</div>
