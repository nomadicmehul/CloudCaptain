# Lesson template

Every lesson in `curriculum/phases/<NN-phase>/<NN-lesson>/` has the same shape. The validator (`npm run curriculum:validate` in `website/`) enforces everything marked **required**. Scaffold a new lesson with:

```bash
cd website
npm run curriculum:scaffold -- 05-containers image-layers --type Learn --langs python --title "Images Are Just Layers"
```

## Folder layout

```
05-containers/02-image-layers/
  lesson.json          required  metadata (see curriculum/schema/lesson.schema.json)
  docs/en.md           required  the lesson, sections in the fixed order below
  code/main.py         required  one entry file per language in lesson.json.langs
  code/tests/          required  at least five tests in total
  quiz.json            required  six questions: 1 pre, 3 check, 2 post
  outputs/             required  every artifact listed in lesson.json.outputs
```

Entry files per language: `main.py` (python), `main.sh` (bash), `main.go` (go), `index.ts` (typescript). Tests count anything matching `def test_`, `@test`, `test(`, `it(` or `func Test` under `code/tests/`.

## `docs/en.md` section order (required)

```markdown
# <Title exactly as lesson.json.title>

*<Motto exactly as lesson.json.motto>*

## Metadata
| Field | Value |
|---|---|
| Type | Build or Learn |
| Languages | Python, Bash |
| Prerequisites | None, or links to earlier lessons |
| Time | 90 minutes |
| Tools | docker |

<Quiz stage="pre" />

## The Problem
## The Concept
## Build It
## Use It
<Quiz stage="check" />
## Ship It
## Exercises
### Easy
### Medium
### Hard
<Quiz stage="post" />
## Key Terms
## Further Reading
```

Rules per section:

| Section | Rule |
|---|---|
| Title + motto | H1 equals `lesson.json.title`; the first non-empty line after it is the motto in italics. |
| Metadata | Table rows `Type` and `Time` must match `lesson.json` (`Time` is `<minutes> minutes`). |
| The Problem | Two or three paragraphs. A concrete scenario of what goes wrong without the concept. No code. |
| The Concept | Intuition first. **Must** contain one `mermaid` diagram (or an image) and **no other code fence**. |
| Build It | The from-scratch implementation, step by step, standard library only. Every code block runs on its own. At least one code fence. Put `<RunTests />` at the end. |
| Use It | The same job with the real tool (Docker, kubectl, Terraform...). Compare it with what you built. |
| Ship It | The reusable artifact the learner walks away with, saved in `outputs/` (prompt, skill, agent definition or MCP server). Put `<ShipIt />` here. |
| Exercises | Exactly three H3s: `### Easy`, `### Medium`, `### Hard`. |
| Key Terms | A table with at least five rows: Term, Common misconception, Precise definition. |
| Further Reading | Links with a reason to read each. At least one link into the Reference layer (`https://cloudcaptain.io/docs/...`). |

Three `<Quiz stage="..." />` tags are required, placed as shown.

## Writing rules

- **Build it, then use it.** The from-scratch version comes first and uses only the standard library. The real tool comes second, doing the same job, so the learner can map one onto the other.
- **Ship something.** Every lesson leaves a file in `outputs/` the learner can drop into their own agent setup: `prompt-<slug>.md`, `skill-<slug>.md`, `agent-<slug>.md`, or an `mcp-<slug>/` folder.
- **Runs today.** Pin versions only when the lesson is about a version. Commands must work on current releases. Put the Linux-only Build It steps and the Use It commands in `code/smoke.sh` and list it as `"smoke": "bash code/smoke.sh"` in `lesson.json`; `lesson-smoke.yml` runs it on an Ubuntu runner with Docker and OpenTofu. Set `last_verified` only after that script has passed (locally on Linux or in CI).
- **Links to the Reference layer are absolute:** `https://cloudcaptain.io/docs/tools/docker/fundamentals`. The site rewrites them to internal links and the build fails if the target does not exist.
- **Markdown is MDX.** Escape `<` and `{` outside code spans, or put them in backticks.
- **Diagrams, not ASCII art.** Mermaid in The Concept; keep it under ~15 nodes.
- **No comments in code.** Names and structure should carry the meaning. Tests are the specification.
- **Linux-only system calls** (namespaces, cgroups, mounts) must be behind a guard so the pure logic still runs and tests pass on macOS and in CI.

## Output file front matter

```markdown
---
name: explain-container-isolation
description: One-line purpose
phase: containers
lesson: containers/process-isolation
---
```

Skills additionally carry `version: 1.0.0` and `tags: [...]`.

## Capstones (phase 16)

A capstone is a graded project, not a lesson: the learner builds something real across several milestones and a grader scores it. It uses `"type": "Capstone"` and lives in `curriculum/phases/16-capstones/NN-slug/`. Scaffold one with:

```bash
cd website
npm run curriculum:scaffold -- 16-capstones build-a-paas --type Capstone --langs python,bash --title "Build a Tiny PaaS" --minutes 720
```

### Folder layout

```
16-capstones/01-build-a-paas/
  lesson.json           required  type Capstone, minutes (whole hours, 240–1200), milestones, pass_score, smoke
  docs/en.md            required  the capstone sections below
  starter/              required  what the learner copies into a new repository
  solution/             required  the reference solution; CI grades it on every change
  grader/grade.sh       required  bash grader/grade.sh <project> [--milestone ID] [--json]
  grader/grade.py       required  the shared harness (identical in every capstone; do not edit)
  grader/checks.py      required  one check_<milestone_id>(project) -> (passed, detail) per milestone
  grader/smoke.sh       required  CI: solution/ must pass, starter/ must not
  grader/workflow.yml   required  GitHub Actions workflow the learner copies into their repository
  grader/tests/         required  at least five tests (the harness tests plus tests for checks.py)
  quiz.json             required  three post questions, asked after the grader passes
  outputs/              required  every artifact listed in lesson.json.outputs
```

`lesson.json` adds:

```json
"milestones": [
  {"id": "deploy-on-push", "title": "Deploy on git push", "points": 3}
],
"pass_score": 7,
"tests": {"python": "python3 -m unittest discover -s grader/tests -v"},
"smoke": "bash grader/smoke.sh"
```

There are 3–8 milestones. `pass_score` is at most the milestone total. Milestone `deploy-on-push` is checked by `check_deploy_on_push(project)` in `grader/checks.py`, and the validator fails if that function is missing.

### `docs/en.md` section order (required)

```markdown
# <Title>
*<Motto>*
## Metadata          Type Capstone, Time "<hours> hours", plus a Pass score row
## The Brief         the job as a team lead would hand it over; who uses it, what is out of scope
## The Architecture  target design; a mermaid diagram or image (code fences allowed)
## Milestones        one ### per milestone, titles and order exactly as in lesson.json
## Grading           how to run grader/grade.sh and copy grader/workflow.yml
## Ship It           <ShipIt />
## Stretch Goals
<Quiz stage="post" />
## Further Reading   at least one Reference-layer link
```

### Grader rules

- **Offline.** The grader needs no cloud account and no API key. Use kind, LocalStack, moto, OpenTofu `plan` with policy checks, or recorded LLM responses. Learners may use real clouds and the real Claude API, but grading must not depend on them.
- **One milestone, one check.** A check returns `(passed, detail)`. The detail tells a failing learner what is missing, not just that something is wrong. A crashing check fails only its own milestone.
- **Proven both ways.** `grader/smoke.sh` starts whatever the checks need, grades `solution/` (exit 0) and `starter/` (exit 1). `lesson-smoke.yml` runs it like any lesson smoke, and `last_verified` is set only after it passes.
- **Fetched, not copied.** `grader/workflow.yml` checks out the grader from this repository, so learners get grader fixes and cannot edit their own grader.
- **Python 3.9+, standard library only** in `grader/`. Real tools are called with `subprocess`.
