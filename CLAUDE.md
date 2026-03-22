# CLAUDE.md — CloudCaptain

## Project Quick Reference

**CloudCaptain** is an open-source learning platform for Cloud, DevOps, AI, and Operations built with Docusaurus 3 and deployed to GitHub Pages.

```bash
cd website
npm install         # Install dependencies
npm start           # Dev server at localhost:3000
npm run build       # Production build
npm run serve       # Serve production build locally
```

> For full project intelligence (architecture, brand colors, content guidelines), see [`website/CLAUDE.md`](website/CLAUDE.md).

---

## AI Contribution Log

Features built with Claude Code assistance:

### design/homepage-v2-upgrade
- Animated typing terminal with character-by-character command rendering
- Scroll-reveal animations on all sections using IntersectionObserver
- Count-up animation on stats numbers when scrolled into view
- Enhanced glassmorphism cards with gradient borders and glow effects
- Rotating gradient ring on founder photo, animated floating orbs
- Live GitHub stats (stars, contributors, forks) fetched from GitHub API
- Connected steps in How It Works with gradient lines and icon circles
- Overhauled light mode with blue-tinted backgrounds, card shadows, gradient titles
- Button shimmer, badge pulse glow, and timeline hover effects
- Navbar spacing fixes, removed Blog link, fixed title truncation

### feature/json-data-extraction
- Extracted all homepage section data to 8 JSON files in `website/src/data/`
- Hidden Testimonials section (component preserved for future use)
- Updated README, CONTRIBUTING.md, and contribute page with JSON data guide
- Career paths data also extracted to JSON

### Data Files Created
| File | Content |
|------|---------|
| `categories.json` | 12 technology category cards |
| `learningPaths.json` | 3 learning path cards |
| `testimonials.json` | Testimonial entries (hidden) |
| `howItWorks.json` | 4 process steps |
| `sponsors.json` | Sponsor tier configuration |
| `contributeActions.json` | 5 contribution types |
| `techBadges.json` | 11 tech badge names |
| `careerPaths.json` | Career path definitions |

---

## Claude Code Setup Guide

### Prerequisites
- Node.js 18+
- npm
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)

### Getting Started

```bash
# Clone the repo
git clone https://github.com/nomadicmehul/CloudCaptain.git
cd CloudCaptain

# Install and run
cd website
npm install
npm start
```

### Useful Prompts

When working on this project with Claude Code, these prompts work well:

- **Add a new technology category**: "Add a new category for [tool] to `website/src/data/categories.json`"
- **Update homepage content**: "Update the learning paths in `website/src/data/learningPaths.json`"
- **Fix styling issues**: "Fix the [component] styling in `website/src/pages/index.tsx`"
- **Add documentation**: "Create a new doc page for [topic] under `website/docs/tools/`"
- **Build and verify**: "Run `npm run build` in the website directory and fix any errors"

### Commit Guidelines

- **NEVER include `Co-Authored-By` lines in commit messages** — this is strictly prohibited
- Do not add any AI attribution tags or trailers to commits
- Use imperative mood: `Add: ...`, `Update: ...`, `Fix: ...`
- Run `npm run build` before submitting a PR

### Project Context

Claude Code automatically reads `CLAUDE.md` files. This project has two:

- **`/CLAUDE.md`** (this file) — contribution log and setup guide
- **`/website/CLAUDE.md`** — detailed project intelligence (architecture, brand colors, directory structure, content guidelines)

Both files are picked up automatically when Claude Code is run from the repo root or the `website/` directory.
