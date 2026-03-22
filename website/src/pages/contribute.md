---
title: Contribute to CloudCaptain
description: How to contribute to the CloudCaptain open-source learning platform
---

# Contribute to CloudCaptain

CloudCaptain is **100% community-driven**. Every contribution — from fixing a typo to adding a complete learning path — helps thousands of learners worldwide.

## Ways to Contribute

### Add Resources
Found an amazing article, video, or tool? Add it to the relevant category with a brief description.

### Write Guides
Create tutorials, how-to guides, or concept explanations for any topic.

### Improve Existing Content
Fix errors, update outdated links, improve descriptions, or reorganize content.

### Add Interview Questions
Share interview questions and answers from your experience.

### Create Exercises
Design hands-on exercises, projects, or challenges for learners.

### Update Homepage Content
Homepage sections are powered by JSON files in `website/src/data/`. Edit the JSON to update content — no code changes needed:

| File | What it controls |
|:-----|:-----------------|
| `categories.json` | Technology cards (Docker, K8s, Terraform, etc.) |
| `learningPaths.json` | Learning path cards on homepage |
| `careerPaths.json` | Career paths page (8 roles) |
| `howItWorks.json` | "How It Works" 4-step process |
| `sponsors.json` | Sponsor cards |
| `contributeActions.json` | Contribution type tags |
| `techBadges.json` | Tech badge pills in the hero |
| `testimonials.json` | Community testimonials |

## How to Submit a Contribution

1. **Fork** the [CloudCaptain repository](https://github.com/nomadicmehul/CloudCaptain)
2. **Clone** your fork locally
3. **Create a branch** for your changes: `git checkout -b add/my-resource`
4. **Make your changes** following our content structure
5. **Test locally**: Run `cd website && npm run build` to verify
6. **Commit** with a clear message: `git commit -m "Add: Kubernetes autoscaling guide"`
7. **Push** to your fork: `git push origin add/my-resource`
8. **Open a Pull Request** against the `main` branch

## Content Guidelines

- Use **Markdown** for all documentation content
- Keep descriptions concise (under 80 characters where possible)
- Include links to original sources
- Organize alphabetically within categories
- Test all links before submitting
- Use existing format and structure
- Use Mermaid diagrams for architecture visuals

## Commit Message Format

Use imperative mood in commit messages:
- `Add: Docker security best practices guide`
- `Update: Kubernetes resources with latest links`
- `Fix: Broken link in AWS section`

## Code of Conduct

We are committed to providing a welcoming and inclusive experience for everyone. Please read our [Code of Conduct](https://github.com/nomadicmehul/CloudCaptain/blob/main/CODE_OF_CONDUCT.md).

## Questions?

Open an [issue](https://github.com/nomadicmehul/CloudCaptain/issues) or reach out to [Mehul](https://nomadicmehul.dev/).

---

**Thank you for helping build CloudCaptain!** Every contribution makes the cloud-native community stronger.
