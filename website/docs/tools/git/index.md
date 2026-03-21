---
title: "Git"
sidebar_label: "Git Overview"
description: "Comprehensive Git learning resources — fundamentals, advanced workflows, commands, and interview prep"
sidebar_position: 0
---

# Git

The distributed version control system used by virtually every development team.

## Documentation

| Guide | Description |
|:------|:------------|
| [Git Fundamentals](/docs/tools/git/fundamentals) | Repositories, commits, branches, merging, remotes, collaboration workflows |
| [Advanced Git](/docs/tools/git/advanced) | Rebase, cherry-pick, hooks, bisect, internals, Git Flow, trunk-based dev |
| [Command Cheat Sheet](/docs/tools/git/cheatsheet) | 150+ commands organized by category — setup, branching, stash, remote, GitHub CLI |
| [Interview Questions](/docs/tools/git/interview-questions) | 40+ questions from beginner to advanced with detailed answers |

## Learning Path

1. [Start with fundamentals](/docs/tools/git/fundamentals) — repos, commits, branches, merging, collaboration
2. [Move to advanced topics](/docs/tools/git/advanced) — rebase, hooks, internals, workflows at scale
3. [Keep the cheat sheet handy](/docs/tools/git/cheatsheet) — 150+ commands at your fingertips
4. [Practice interview questions](/docs/tools/git/interview-questions) — 40+ Q&A with detailed explanations

## Quick Start

```bash
# Initialize a new repository
git init my-project && cd my-project

# Basic workflow
git add .
git commit -m "Initial commit"

# Branching
git checkout -b feature/new-feature
# ... make changes ...
git add . && git commit -m "Add new feature"
git checkout main && git merge feature/new-feature

# Remote operations
git remote add origin https://github.com/user/repo.git
git push -u origin main
```

## External Resources

| Resource | Description |
|:---------|:------------|
| [Pro Git Book](https://git-scm.com/book/en/v2) | The definitive Git book (free) |
| [Learn Git Branching](https://learngitbranching.js.org/) | Visual interactive Git tutorial |
| [Git Immersion](https://gitimmersion.com) | Hands-on introduction to Git |
| [Oh My Git!](https://ohmygit.org/) | Open source game about learning Git |
| [GitHub Skills](https://skills.github.com) | Official GitHub learning platform |
| [Atlassian Git Tutorial](https://www.atlassian.com/git/tutorials/advanced-overview) | Advanced Git concepts |
| [Oh Shit, Git!?!](https://ohshitgit.com/) | Practical solutions to common Git problems |

## Video Tutorials

| Resource | Description |
|:---------|:------------|
| [Git and GitHub in 5 Hours](https://www.youtube.com/watch?v=KMOmw19ZCGs) | Comprehensive video course |
| [Brian Yu's Git Introduction](https://www.youtube.com/watch?v=eulnSXkhE7I) | Beginner-friendly introduction |
