---
title: Git Fundamentals
sidebar_label: Fundamentals
sidebar_position: 1
---

# Git Fundamentals

Master the core concepts and architecture of Git version control. This guide covers the essentials needed to understand how Git works and why it's the industry standard for source control.

## What is Git?

Git is a **distributed version control system (DVCS)** created by Linus Torvalds in 2005. Unlike centralized systems like SVN, every developer has a complete copy of the repository history, enabling offline work and resilient collaboration.

### Key Characteristics

- **Distributed**: Each clone is a full backup of the repository
- **Fast**: All operations are local except push/pull
- **Flexible**: Supports multiple workflows and branching strategies
- **Secure**: SHA-1 hashes ensure data integrity
- **Efficient**: Stores snapshots, not deltas

## Version Control Concepts

### What is Version Control?

Version control tracks changes to files over time, enabling:

- **History**: View what changed, when, and why
- **Collaboration**: Multiple developers working simultaneously
- **Branching**: Parallel development streams
- **Recovery**: Revert to previous states
- **Audit Trail**: Complete blame and attribution

### Centralized vs Distributed

**Centralized (SVN, Perforce)**
- Single source of truth on server
- Developers check out working copies
- Network required for most operations
- Single point of failure

**Distributed (Git, Mercurial)**
- Every clone is a complete repository
- Offline workflow possible
- Multiple sources of truth
- No single point of failure
- Better for open-source collaboration

## Git Architecture

Git operates on three primary storage locations:

```
┌─────────────────────────────────────────────────┐
│            Your Computer                        │
├─────────────────────────────────────────────────┤
│                                                 │
│  ┌──────────────┐                               │
│  │   Working    │                               │
│  │ Directory    │  ← Your actual files          │
│  └──────────────┘                               │
│         │                                       │
│       git add                                   │
│         │                                       │
│  ┌──────────────┐                               │
│  │   Staging    │                               │
│  │     Area     │  ← Files ready to commit      │
│  │   (Index)    │                               │
│  └──────────────┘                               │
│         │                                       │
│      git commit                                 │
│         │                                       │
│  ┌──────────────┐                               │
│  │   Local      │                               │
│  │ Repository   │  ← Commits stored locally     │
│  │   (.git)     │                               │
│  └──────────────┘                               │
│         │                                       │
│      git push                                   │
│         │                                       │
└─────────────────────────────────────────────────┘
         │
         ↓
┌─────────────────────────────────────────────────┐
│  Remote Repository (GitHub, GitLab, etc.)       │
└─────────────────────────────────────────────────┘
```

### 1. Working Directory

The files on your disk that you're currently editing. Changes here are **untracked** until you stage them.

```bash
git status  # See what's changed in working directory
```

### 2. Staging Area (Index)

An intermediate layer where you select which changes to include in the next commit. Allows atomic commits of specific changes.

```bash
git add <file>      # Stage specific file
git add .           # Stage all changes
git status          # See staged vs unstaged
```

### 3. Local Repository

The `.git` directory containing all commits, branches, and history. Commits here are permanent but can be rewritten (before pushing).

```bash
git commit -m "message"  # Save staged changes
git log                  # View commit history
```

## Commits

A commit is an immutable snapshot of your project at a point in time.

### Anatomy of a Commit

```
commit a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0
Author: John Doe <john@example.com>
Date:   Mon Mar 21 10:30:45 2026 -0700

    Add user authentication module

    - Implement JWT token generation
    - Add password hashing with bcrypt
    - Create login endpoint
```

**Hash**: SHA-1 checksum of commit contents (immutable)
**Author**: Creator information and timestamp
**Message**: Description of changes (convention: imperative mood)
**Parent**: Reference to previous commit (creates chain)
**Tree**: Snapshot of all files at this point

### Commit Messages Best Practices

```
# Good
Add user authentication module
Fix: typo in README
Update: dependency versions to latest stable

# Bad
fixed stuff
update
changes
asdf
```

**Guidelines**:
- Use imperative mood ("Add" not "Added")
- First line under 50 characters
- Blank line before body
- Wrap body at 72 characters
- Explain why, not what

## Branches

Branches are pointers to commits, enabling parallel development streams.

### Understanding Branches

```
        ┌─ feature-x (local)
        │
main ───●─────●─────●
            │
            └─ develop
```

A branch is simply a reference (pointer) to a commit. Creating a branch doesn't duplicate code; it's just a new label.

### Creating and Switching

```bash
git branch feature-new        # Create branch
git checkout feature-new      # Switch to branch (old way)
git switch feature-new        # Switch to branch (new way)
git checkout -b feature-new   # Create and switch
git branch -d feature-new     # Delete branch
```

### Main vs Master

Modern convention uses **main** instead of **master**. GitHub changed the default in 2020 for inclusive language.

```bash
git branch -m master main     # Rename local branch
```

### Protected Branches

On GitHub/GitLab, protect main/develop to require pull requests and status checks before merging.

## Merging

Combining changes from one branch into another.

### Fast-Forward Merge

When target branch hasn't changed since source branch was created:

```
Before:
main ───●
         │
         └─ feature ───●───●

After (fast-forward):
main ───●───●───●
```

```bash
git switch main
git merge feature
# History is linear; no merge commit created
```

### Three-Way Merge

When both branches have diverged:

```
Before:
main ───●───●
         │
         └─ feature ───●───●

After:
main ───●───●───●(merge)
         │   /
         └─●───●
```

```bash
git switch main
git merge feature
# Creates merge commit with two parents
```

### Merge Conflicts

When Git can't automatically reconcile changes:

```
<<<<<<< HEAD
This is from main branch
=======
This is from feature branch
>>>>>>> feature
```

**Resolution**:
1. Edit file manually to resolve
2. `git add <file>`
3. `git commit` to complete merge

## Rebasing

Reapplying commits from one branch onto another. Creates linear history.

### Rebase vs Merge Visualization

**Merge** (creates merge commit):
```
main ───●───●───●(merge)
         │   /
         └─●───●
```

**Rebase** (linear history):
```
main ───●───●───●───●───●
        (original)   (rebased copies)
```

### Basic Rebase

```bash
git switch feature
git rebase main
# Replays feature commits on top of main
```

### Interactive Rebase

Powerful tool for cleaning up history before submitting PR:

```bash
git rebase -i main
# Opens editor with commit list:
# pick a1b2c3d First feature commit
# pick b3c4d5e Second feature commit
# pick c5d6e7f Third feature commit

# Change to:
# pick a1b2c3d First feature commit
# squash b3c4d5e Second feature commit
# reword c5d6e7f Fix typo in header
```

**Interactive Rebase Commands**:
- `pick`: Use commit
- `reword`: Use commit but edit message
- `squash`: Meld into previous commit
- `fixup`: Meld without keeping message
- `drop`: Remove commit entirely
- `exec`: Run shell command

### Rebase vs Merge Philosophy

- **Rebase**: Clean, linear history (preferred for local branches)
- **Merge**: Preserves branching history (safer for shared branches)

**Golden Rule**: Never rebase commits that are already pushed unless you've coordinated with team.

## HEAD

HEAD is a reference pointing to your current location in the repository.

### Normal HEAD

```bash
git log HEAD      # Shows commits reachable from HEAD
# HEAD points to latest commit of current branch
```

### Detached HEAD State

When HEAD points directly to a commit instead of a branch:

```bash
git checkout a1b2c3d    # Check out specific commit
# HEAD now points directly to this commit
# Any commits made here are orphaned unless saved to a branch
```

**Use cases for detached HEAD**:
- Inspect historical code
- Create hotfix from past version
- Test specific commit

**Recovering from detached HEAD**:

```bash
git reflog                    # Find your commit hash
git switch -c recovery-branch # Create branch from current HEAD
```

## .gitignore

Specifies files and patterns to exclude from version control.

### Common Patterns

```
# Comments start with #

# Ignore specific file
secrets.env

# Ignore all .log files
*.log

# Ignore directory and contents
node_modules/
dist/

# Ignore everything except specific files
*.py
!important.py

# Ignore files in any subdirectory
**/temp/

# Ignore .env files globally
.env*
!.env.example
```

### Global .gitignore

```bash
git config --global core.excludesfile ~/.gitignore_global
```

### Untracking Ignored Files

```bash
git rm --cached <file>    # Remove from tracking but keep locally
git rm -r --cached node_modules/  # Remove directory from tracking
```

## Git vs SVN

| Feature | Git | SVN |
|---------|-----|-----|
| Type | Distributed | Centralized |
| Branching | Fast, lightweight | Expensive directory copy |
| Offline | Yes, full history | No |
| Merge | Three-way merge | Expensive re-integration |
| Performance | Fast (local operations) | Slower (network calls) |
| Learning Curve | Steeper | Gentler |
| Rollback | Easy (separate commands) | Same process as merge |
| Staging | Yes (index) | No |
| Repository Size | Smaller (.git is efficient) | Larger (multiple copies) |
| Use Case | Modern teams, open-source | Enterprise, legacy systems |

## Exercises

### Exercise 1: Initialize and Make Your First Commit

```bash
# Create a new directory and initialize Git
mkdir my-project
cd my-project
git init

# Configure Git (one-time)
git config user.name "Your Name"
git config user.email "your@email.com"

# Create a file
echo "# My Project" > README.md

# Stage and commit
git add README.md
git commit -m "Initial commit: add README"

# Verify
git log
```

### Exercise 2: Branching and Switching

```bash
# Create and switch to feature branch
git switch -c add-feature

# Make changes
echo "New feature code" > feature.js
git add feature.js
git commit -m "Add: feature module"

# Switch back to main
git switch main

# Verify feature.js doesn't exist here
ls feature.js  # File not found

# Switch back to feature
git switch add-feature
ls feature.js  # File exists again
```

### Exercise 3: Merge Conflict Resolution

```bash
# Create two branches with conflicting changes
git switch -c branch-a
echo "Content from A" > shared.txt
git add shared.txt
git commit -m "Add: content from A"

git switch -c branch-b main
echo "Content from B" > shared.txt
git add shared.txt
git commit -m "Add: content from B"

# Merge branch-a into branch-b
git merge branch-a

# Conflict! Edit shared.txt to resolve
# Then:
git add shared.txt
git commit -m "Resolve: merge conflict in shared.txt"
```

### Exercise 4: Rebase Practice

```bash
# Create feature branch
git switch -c feature-rebase main
echo "feature 1" > f1.txt
git add f1.txt
git commit -m "Feature 1"

echo "feature 2" > f2.txt
git add f2.txt
git commit -m "Feature 2"

# Meanwhile, main received commits
git switch main
echo "main update" > main.txt
git add main.txt
git commit -m "Main update"

# Rebase feature on main
git switch feature-rebase
git rebase main

# Commits are replayed; history is linear
git log --oneline
```

### Exercise 5: Cherry-Pick

```bash
# Create commits on feature branch
git switch -c source-branch
git commit --allow-empty -m "Commit A"
git commit --allow-empty -m "Commit B"
git commit --allow-empty -m "Commit C"

# Switch to main and cherry-pick specific commit
git switch main
git cherry-pick <hash-of-commit-b>

# Only Commit B is applied to main
git log --oneline
```

### Exercise 6: Stash and Restore

```bash
# Make changes without committing
echo "work in progress" > wip.txt
git add wip.txt
git status  # Shows staged changes

# Stash it (save temporarily)
git stash

# Working directory is clean
git status  # Clean

# Restore stashed changes
git stash pop
git status  # Changes back
```

---

## Summary

You now understand:

- **Git architecture**: Working directory, staging area, local repository
- **Commits**: Immutable snapshots with hashes and messages
- **Branches**: Lightweight pointers enabling parallel development
- **Merging**: Combining branches (fast-forward or three-way)
- **Rebasing**: Linear history through commit reapplication
- **HEAD**: Current location marker (and detached state)
- **.gitignore**: Excluding files from tracking
- **Git vs SVN**: Why Git dominates modern development

These fundamentals form the foundation for all Git workflows. Master these concepts before moving to advanced topics.

