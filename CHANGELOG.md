# Changelog

All notable changes to CloudCaptain. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- `curriculum/` with the first from-scratch lesson (Containers → *Process Isolation: A Container Is Just a Process*), the phase manifest (phases 00–16), JSON Schemas, `LESSON_TEMPLATE.md`, `AGENTS.md`, `ROADMAP.md`.
- `scripts/build-curriculum.mjs`: validates every lesson against the template and generates the `/learn` sidebar, stats and backlinks; `--scaffold`, `--check`, `--roadmap`, `--json`.
- `scripts/run-lesson-tests.mjs`: runs a lesson's tests by id or `--all`.
- Website: second docs instance at `/learn`, front-matter injection from `lesson.json`, `<Quiz />`, `<ShipIt />`, `<RunTests />`, `<CurriculumHome />`, `<LearningPath />` MDX components, "Learn" navbar entry.

### Added (second batch)
- Lessons: Containers → *Images Are Just Layers* (Learn; a `docker save` layer auditor that finds deleted and overwritten bytes) and Infrastructure as Code → *Plan and Apply: A State File Is Just a Diff* (Build; a plan/apply engine with refresh, drift, replace semantics and a state file). 17 more tests, two more artifacts.
- `/progress`: a Curriculum section with per-phase completion bars, quiz scores per lesson and a "continue" card; lesson titles everywhere progress is listed.
- Captain's Bridge on lesson pages: a lesson cell in the telemetry bar, a **Lesson** rail tab (quiz status, prerequisites, tools, test command, artifacts, reference links), and a completion toast when all sections are read and the post quiz is passed. Sidebar progress dots on `/learn` links.
- Reference docs that a lesson builds on show a "Build this from scratch" card.
- Progress export is now version 2 and includes quiz records; import accepts v1 and v2; reset clears both.
- Issue templates for new lessons and outdated content; nightly workflow that validates lessons, runs every lesson's tests and checks links.
- `code/smoke.sh` per lesson plus `lesson-smoke.yml`: the Linux-only Build It steps and the Use It commands run for real on an Ubuntu runner with Docker and OpenTofu. `last_verified` is set only after that passes.

- Lessons: Setup → *The Lab: One Command That Proves Your Machine Is Ready* (Build; a lab doctor that probes tools, versions and Linux features and prints the install plan per OS) and Linux → *PID 1: Why Your Container Ignores Ctrl-C* (Build; a supervisor that reaps, forwards signals and restarts with backoff; `docker stop` timing with and without an init).
- Lesson: Containers → *Pull an Image With Plain HTTP: Registries Are Content-Addressed* (Build; an OCI registry client with the Bearer token flow, index and platform selection, digest verification, a tamper test and traversal-safe rootfs extraction; verified against Docker Hub).
- Lesson: AIOps & Agentic Ops → *An MCP Server for kubectl: Tools Are Functions With a Schema* (Build; a read-only Model Context Protocol server over JSON-RPC/stdio with typed tools, verb and flag allow-lists, a namespace allow-list, pinned context and bounded output; 11 tests; ships the packaged server plus an MCP tool-review skill). New **AIOps Engineer** learning path.
- Freshness system: `curriculum/versions.json` (16 tools, refreshed weekly from GitHub releases by `versions-weekly.yml`), `curriculum/deprecations.json` (retired/removed/relicensed tech with replacements), a `<ToolVersion name="…" />` tag for lessons, "Current tool versions" and "Deprecation watch" on `/learn`, a validator warning when `last_verified` is older than 180 days or a lesson mentions retired tech without saying so, and `build-curriculum.mjs --deprecations` to scan the Reference docs (run nightly).
- Homepage: a "Learn from scratch" section with the Build/Use/Ship pillars, live curriculum stats, phase chips and the `npx skills add` command.
- Reference docs: 18 remaining mentions of CodeCommit, Docker Swarm, PodSecurityPolicy and ingress-nginx now carry a status note where they appear.

### Fixed
- Captain's Bridge **Commands** tab and `.sh` download: multi-line code blocks were read as one over-long line (Prism emits no newlines), so most pages showed "Commands 0". Lines are now taken from the rendered `token-line` spans.

### Removed
- 15 stub and pointer pages (Puppet, Vagrant, Packer, CloudFormation, DigitalOcean, CircleCI, the Docker/Terraform `commands`/`notes`/`exercises` placeholders and five interview-prep pointer pages). Every old URL redirects to the page that has the content; CircleCI is summarised on the CI/CD landing page; the Interview Prep sidebar now links straight to the question sets.

### Changed
- Homepage hero button goes to `/learn`; category cards show real guide counts instead of "N+ resources"; footer Roadmap points at ROADMAP.md.
- Navbar: the three Reference sidebars (Learning Paths, Tools & Tech, Cloud Providers) plus Career Paths and Interview Prep now live under one **Reference** dropdown next to **Learn**, so the row fits at 1280px and above.

## [2026-10-31]

### Fixed
- Commands that no longer worked: GitHub Actions pins (`upload/download-artifact@v3` and friends), `apt-key` installs (Jenkins, HashiCorp, Falco), the dead Helm `stable` repo, `kubectl --record`, `k8s.gcr.io`, `gcr.io` registries, Bitnami images and charts, the kubeadm upgrade walkthrough.
- Retired technology taught as current: ingress-nginx (retired March 2026), PodSecurityPolicy, AWS CodeCommit, Docker Swarm positioning, Terraform licence wording (BUSL / OpenTofu).
- Exam metadata: CKA/CKAD/CKS $445, KCNA $250, Terraform Associate 004, AWS CloudOps Engineer SOA-C03.
- Overstated numbers in the README and homepage (185K words of prose, 3,300 code blocks, 52 diagrams, 20–60 questions per set); "hands-on labs" → "exercises"; non-existent certifications in career paths; the Journey page rating.
- `npm run typecheck` (React 19 `JSX` namespace imports).

### Changed
- Nine orphan stub/redirect pages and the two 2024 blog posts are now `unlisted`. Docusaurus boilerplate page and images removed.
