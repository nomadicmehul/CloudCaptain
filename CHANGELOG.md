# Changelog

All notable changes to CloudCaptain. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- `curriculum/` with the first from-scratch lesson (Containers → *Process Isolation: A Container Is Just a Process*), the phase manifest (phases 00–16), JSON Schemas, `LESSON_TEMPLATE.md`, `AGENTS.md`, `ROADMAP.md`.
- `scripts/build-curriculum.mjs`: validates every lesson against the template and generates the `/learn` sidebar, stats and backlinks; `--scaffold`, `--check`, `--roadmap`, `--json`.
- `scripts/run-lesson-tests.mjs`: runs a lesson's tests by id or `--all`.
- Website: second docs instance at `/learn`, front-matter injection from `lesson.json`, `<Quiz />`, `<ShipIt />`, `<RunTests />`, `<CurriculumHome />`, `<LearningPath />` MDX components, "Learn" navbar entry.

## [2026-10-31]

### Fixed
- Commands that no longer worked: GitHub Actions pins (`upload/download-artifact@v3` and friends), `apt-key` installs (Jenkins, HashiCorp, Falco), the dead Helm `stable` repo, `kubectl --record`, `k8s.gcr.io`, `gcr.io` registries, Bitnami images and charts, the kubeadm upgrade walkthrough.
- Retired technology taught as current: ingress-nginx (retired March 2026), PodSecurityPolicy, AWS CodeCommit, Docker Swarm positioning, Terraform licence wording (BUSL / OpenTofu).
- Exam metadata: CKA/CKAD/CKS $445, KCNA $250, Terraform Associate 004, AWS CloudOps Engineer SOA-C03.
- Overstated numbers in the README and homepage (185K words of prose, 3,300 code blocks, 52 diagrams, 20–60 questions per set); "hands-on labs" → "exercises"; non-existent certifications in career paths; the Journey page rating.
- `npm run typecheck` (React 19 `JSX` namespace imports).

### Changed
- Nine orphan stub/redirect pages and the two 2024 blog posts are now `unlisted`. Docusaurus boilerplate page and images removed.
