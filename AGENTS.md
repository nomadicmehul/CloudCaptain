# AGENTS.md — working in the CloudCaptain repository

This file is for coding agents (Claude Code, Cursor, Codex, Copilot and friends) and for humans who want the same shortcuts.

## What this repository is

CloudCaptain is a free, open-source learning hub for Cloud, DevOps and AIOps at https://cloudcaptain.io. It has two content layers:

| Layer | Where | What |
|---|---|---|
| **Curriculum** (guided) | `curriculum/` → served at `/learn` | Numbered phases of lessons that follow *Build it → Use it → Ship it*. Each lesson is a folder with docs, runnable code, tests, a quiz and reusable artifacts. |
| **Reference** | `website/docs/` → served at `/docs` | Long-form guides, cheat sheets, interview questions and exam prep per tool or cloud. Lessons link into it. |

The website is Docusaurus 3 in `website/`. The curriculum is read by a second docs-plugin instance; `scripts/build-curriculum.mjs` validates every lesson and generates the sidebar and stats before each build.

## Commands

Run these from `website/` unless noted.

```bash
npm ci                          # install
npm run curriculum:validate     # validate every lesson against LESSON_TEMPLATE.md (fast, no build)
npm run curriculum:scaffold -- <phase-dir> <lesson-slug> --type Build|Learn --langs python,bash --title "..."
npm run curriculum:test -- <phase-slug>/<lesson-slug>   # run that lesson's tests (or --all)
npm run curriculum:unit         # unit tests for the generator itself
npm start                       # dev server (runs the generator first)
npm run build                   # production build; fails on broken links or invalid lessons
npm run typecheck               # tsc
```

## Repository map

```
curriculum/
  manifest.json        phases (stable numbering 00–16), learning paths, certifications
  schema/              JSON Schemas for manifest.json, lesson.json, quiz.json
  phases/NN-slug/      README.md (phase overview) + NN-lesson/ folders
  paths/<id>.md        learning-path pages
scripts/
  build-curriculum.mjs validate + generate website/src/data/generated/curriculum.json (+ --scaffold, --roadmap, --json)
  run-lesson-tests.mjs run lesson tests by id or --all
  lib/, __tests__/     generator internals and its unit tests
website/
  docusaurus.config.ts second docs instance (id: curriculum, routeBasePath: learn), front-matter hook
  plugins/             curriculum-frontmatter.ts (injects slug/title/cc_* from lesson.json), remark-site-links.ts
  src/components/Curriculum/  Quiz, LessonMeta, ShipIt, RunTests, CurriculumHome, LearningPath
  src/components/CaptainsBridge/  the reading layout (progress, chapter rail) shared by /docs and /learn
  docs/                Reference layer (keep every URL working)
LESSON_TEMPLATE.md     the lesson contract the validator enforces
ROADMAP.md             planned phases and lesson counts (table regenerated with --roadmap)
CHANGELOG.md           human-readable changes per release
```

## How to add a lesson

1. `npm run curriculum:scaffold -- 05-containers image-layers --type Learn --langs python --title "Images Are Just Layers"`.
2. Fill `docs/en.md` in the fixed section order, write `code/` and at least five tests, six quiz questions, and the `outputs/` artifacts.
3. `npm run curriculum:validate && npm run curriculum:test -- containers/image-layers && npm run build`.
4. Add the lesson id to a path in `curriculum/manifest.json` if it belongs to one. Never edit `sidebars*.ts` by hand; they are generated.
5. Set `"status": "published"` when it is ready. `draft` lessons are excluded from production builds; `preview` lessons are built but unlisted.

## Conventions

- Lesson ids are `<phase-slug>/<lesson-slug>`; folders are `NN-<lesson-slug>`. Numbers order lessons inside a phase and never appear in URLs.
- Links to the Reference layer are absolute (`https://cloudcaptain.io/docs/...`). The build rewrites them and fails if the target is missing.
- Lesson code: standard library only in *Build It*; no comments; Linux-only syscalls behind guards so tests pass on macOS and CI.
- Commits use imperative subjects (`Add: ...`, `Fix: ...`, `Update: ...`). **Never add `Co-Authored-By` or other AI attribution trailers** (see `CLAUDE.md`).
- Keep `npm run build` green: `onBrokenLinks` is `throw`.
- Do not add "coming soon" pages. A phase without lessons is listed in `ROADMAP.md`, not on the site.

## Learning with an agent

The `skills/` folder ships tutor skills (`captain-start`, `captain-learn`, `captain-guide`, `captain-check`) installable with `npx skills add nomadicmehul/CloudCaptain`. They read `curriculum/manifest.json`, the lesson folders and `LEARNING.md` in the learner's project. Keep them in sync with this file when the curriculum layout changes.
