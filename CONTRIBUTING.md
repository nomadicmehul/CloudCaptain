# Contributing to CloudCaptain

Thank you for your interest in contributing to CloudCaptain! Every contribution — from fixing a typo to adding a complete learning path — helps thousands of learners worldwide.

## Ways to Contribute

- **Add Resources** — Found an amazing article, video, or tool? Add it to the relevant category.
- **Write Guides** — Create tutorials, how-to guides, or concept explanations.
- **Improve Existing Content** — Fix errors, update outdated links, improve descriptions.
- **Add Interview Questions** — Share interview questions and answers from your experience.
- **Create Exercises** — Design hands-on labs, projects, or challenges.
- **Update Homepage Content** — Edit JSON data files to update what appears on the site.
- **Improve Design** — Enhance styling, animations, or responsiveness.
- **Translate** — Help make content accessible in other languages.

## Quick Start

```bash
# 1. Fork and clone
git clone https://github.com/<your-username>/CloudCaptain.git
cd CloudCaptain/website

# 2. Install dependencies
npm install

# 3. Start dev server
npm start         # Opens at localhost:3000

# 4. Build for production (always do this before submitting a PR)
npm run build
```

## How to Submit a Contribution

1. **Fork** the [CloudCaptain repository](https://github.com/nomadicmehul/CloudCaptain)
2. **Clone** your fork locally
3. **Create a branch**: `git checkout -b add/my-resource`
4. **Make your changes** following the guidelines below
5. **Test locally**: Run `npm run build` to verify no errors
6. **Commit** with a clear message: `git commit -m "Add: Kubernetes autoscaling guide"`
7. **Push** to your fork: `git push origin add/my-resource`
8. **Open a Pull Request** against the `main` branch

## Content Guidelines

### Documentation (Markdown)

- All docs go in `website/docs/` under the appropriate subdirectory
- Use front matter: `title`, `description`, `sidebar_position`
- Keep descriptions concise (under 80 characters where possible)
- Include links to original sources
- Organize alphabetically within categories
- Test all links before submitting
- Use Mermaid diagrams for architecture visuals

### Homepage Content (JSON Data)

Homepage sections are driven by JSON files in `website/src/data/`. To update content, edit the relevant JSON file — no React code changes needed.

| File | What it controls |
|:-----|:-----------------|
| `categories.json` | Technology cards (Docker, K8s, Terraform, etc.) |
| `learningPaths.json` | 3 learning path cards on homepage |
| `careerPaths.json` | 8 career paths on the Career Paths page |
| `howItWorks.json` | "How It Works" 4-step section |
| `sponsors.json` | Sponsor cards (Buy Me a Coffee, GitHub Sponsors) |
| `contributeActions.json` | Contribution type tags in "Join the Crew" section |
| `techBadges.json` | Tech badge pills in the hero section |
| `testimonials.json` | Community testimonials (hidden until we have real ones) |

**Example — adding a new technology card:**

Edit `website/src/data/categories.json` and add:
```json
{
  "icon": "🔥",
  "title": "My Tool",
  "desc": "Short description of the tool.",
  "link": "/docs/tools/my-tool/",
  "count": "5+ resources",
  "accent": "#FF6600"
}
```

**Example — adding a testimonial:**

Edit `website/src/data/testimonials.json` and add:
```json
{
  "text": "Your testimonial text here.",
  "name": "Your Name",
  "role": "Your Role",
  "initial": "Y"
}
```

## Commit Message Format

Use imperative mood:
- `Add: Docker security best practices guide`
- `Update: Kubernetes resources with latest links`
- `Fix: Broken link in AWS section`
- `Refactor: Extract data to JSON files`

## Project Structure

```
website/
  docs/                     # All documentation content (Markdown)
    learning-paths/         # Structured learning roadmaps
    interview-prep/         # Interview preparation guides
    tools/                  # Tool-specific docs (Docker, K8s, Terraform, etc.)
    cloud/                  # Cloud provider guides (AWS, Azure, GCP)
  src/
    data/                   # JSON data files for homepage sections
    pages/                  # Custom React pages (index.tsx, career-paths.tsx)
    css/custom.css          # All custom styling
    theme/                  # Docusaurus theme overrides
  static/img/               # Images and assets
  docusaurus.config.ts      # Site configuration
  sidebars.ts               # Sidebar navigation structure
```

## Pull Requests

- Use imperative mood in PR titles (e.g. "Add" not "Added")
- Include what you changed and why in the description
- One logical change per PR
- Make sure `npm run build` passes before submitting

## Issues

- Discussion, questions, and bug reports only
- Check spelling and grammar
- If you're sure your contribution meets guidelines, go ahead and create a PR

## Code of Conduct

We are committed to providing a welcoming and inclusive experience for everyone. Please read our [Code of Conduct](https://github.com/nomadicmehul/CloudCaptain/blob/main/CODE_OF_CONDUCT.md).

## Questions?

Open an [issue](https://github.com/nomadicmehul/CloudCaptain/issues) or reach out to [Mehul](https://nomadicmehul.dev/).

---

**Thank you for helping build CloudCaptain!** Every contribution makes the cloud-native community stronger.
