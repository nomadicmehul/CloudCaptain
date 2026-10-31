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
- **Runs today.** Pin versions only when the lesson is about a version. Commands must work on current releases; set `last_verified` in `lesson.json` when you run them.
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
