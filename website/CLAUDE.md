# CLAUDE.md — CloudCaptain Project Intelligence

This file provides context for AI assistants working on the CloudCaptain project.

## Project Overview

CloudCaptain is an open-source, community-driven learning platform for Cloud, DevOps, AI, and Operations. It is built with Docusaurus 3 and deployed to GitHub Pages.

## Architecture

- **Framework**: Docusaurus 3 (TypeScript)
- **Hosting**: GitHub Pages
- **CI/CD**: GitHub Actions (auto-deploy on push to main)
- **Content**: Markdown/MDX docs organized by category
- **Two content layers**: Reference docs in `website/docs` (served at `/docs`, hand-written sidebars in `sidebars.ts`) and the from-scratch **curriculum** in `../curriculum` (served at `/learn` by a second `@docusaurus/plugin-content-docs` instance with `id: 'curriculum'`; sidebar generated into `sidebars.curriculum.ts` from `src/data/generated/curriculum.json`)
- **Curriculum build step**: `scripts/build-curriculum.mjs` runs on `prestart`/`prebuild`/`pretypecheck`; it validates lessons against `LESSON_TEMPLATE.md` and writes `src/data/generated/curriculum.json` (gitignored). `npx docusaurus build` without the npm script will fail until it has run once.
- **Front matter for lessons is injected**, not written: `plugins/curriculum-frontmatter.ts` (wired via `markdown.parseFrontMatter`) reads `lesson.json`/`quiz.json` and sets `slug`, `title`, `draft`/`unlisted` plus custom keys `cc_kind` (landing|phase|lesson|path), `cc_lesson`, `cc_quiz`, `cc_phase`, `cc_path`. Components read them with `useDoc().frontMatter`.
- **Global MDX components** (`src/theme/MDXComponents.tsx`): `Quiz`, `LessonMeta`, `ShipIt`, `RunTests`, `CurriculumHome`, `LearningPath` from `src/components/Curriculum/`. Quiz results are stored at `cloudcaptain.quiz.{pathname}` and fire `cc-bridge-read-change` like Captain's Bridge.
- **Freshness data**: `curriculum/versions.json` and `curriculum/deprecations.json` are copied into `curriculum.json` by the generator; `<ToolVersion name="kubernetes" />` renders the tracked version, `/learn` shows both lists, and `build-curriculum.mjs --deprecations` scans `website/docs` for retired tech without a status note.
- **Links in curriculum markdown** to `https://cloudcaptain.io/...` are rewritten to internal links by `plugins/remark-site-links.ts` so `onBrokenLinks: 'throw'` checks them.

## Directory Structure

```
website/                    # Docusaurus project root
  docs/
    learning-paths/         # Structured learning roadmaps
    interview-prep/         # Interview preparation guides
    tools/                  # Tool-specific documentation
      docker/
      kubernetes/
      terraform/
      ansible/
      ...
    cloud/                  # Cloud provider guides
      aws/
      azure/
      gcp/
      ...
  blog/                     # Blog posts
  src/
    data/                   # JSON data files for homepage sections
    pages/                  # Custom pages (home, contribute)
    css/                    # Custom CSS with CloudCaptain branding
  static/img/               # Static assets, logo
  .github/workflows/        # GitHub Actions for deployment
```

## Brand Colors

- **Navy (primary dark)**: #1B2A4A
- **Blue (primary)**: #1E9BD7
- **Sky Blue (accent)**: #38BDF8
- **Teal**: #06B6D4
- **Dark background**: #0A1628

## Content Guidelines

- All content is in Markdown
- Each tool/topic has an `index.md` as the entry point
- Resources are organized as tables with Name | Description | Link
- Keep descriptions under 80 characters
- Use front matter with title, description, and sidebar_position

## Commands

```bash
cd website
npm install         # Install dependencies
npm start           # Dev server at localhost:3000
npm run build       # Production build
npm run serve       # Serve production build locally
npm run curriculum:validate   # Validate lessons (no build)
npm run curriculum:test -- <phase>/<lesson>   # Run a lesson's tests
```

## Contributing

- Fork → Branch → PR workflow
- Commit messages use imperative mood: "Add: ...", "Update: ...", "Fix: ..."
- **Do NOT include `Co-Authored-By` lines in commit messages**
- Run `npm run build` locally before submitting PR
- All content goes in the appropriate `docs/` subdirectory

## Key Files

- `docusaurus.config.ts` — Main site configuration
- `sidebars.ts` — Navigation sidebar definitions
- `sidebars.curriculum.ts` — Generated sidebar for `/learn` (do not edit; add lesson folders instead)
- `plugins/curriculum-frontmatter.ts`, `plugins/remark-site-links.ts` — Curriculum integration
- `src/components/Curriculum/` — Quiz and lesson components
- `../scripts/build-curriculum.mjs` — Curriculum validator/generator (`npm run curriculum:validate`)
- `src/pages/index.tsx` — Custom homepage
- `src/css/custom.css` — Brand-specific styling
- `.github/workflows/deploy.yml` — Deployment pipeline
