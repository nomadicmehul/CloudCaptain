# CLAUDE.md — CloudCaptain Project Intelligence

This file provides context for AI assistants working on the CloudCaptain project.

## Project Overview

CloudCaptain is an open-source, community-driven learning platform for Cloud, DevOps, AI, and Operations. It is built with Docusaurus 3 and deployed to GitHub Pages.

## Architecture

- **Framework**: Docusaurus 3 (TypeScript)
- **Hosting**: GitHub Pages
- **CI/CD**: GitHub Actions (auto-deploy on push to main)
- **Content**: Markdown/MDX docs organized by category

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
- `src/pages/index.tsx` — Custom homepage
- `src/css/custom.css` — Brand-specific styling
- `.github/workflows/deploy.yml` — Deployment pipeline
