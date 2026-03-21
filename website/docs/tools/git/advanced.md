---
title: Git Advanced Topics
sidebar_label: Advanced Topics
sidebar_position: 3
---

# Git Advanced Topics

Master sophisticated Git workflows, internals, and strategies used by professional development teams.

## Interactive Rebase

Interactive rebase is the most powerful Git tool for manipulating history. Use it before submitting pull requests to create clean, logical commits.

### Starting Interactive Rebase

```bash
git rebase -i main                 # Rebase and edit commits on main
git rebase -i HEAD~5               # Edit last 5 commits
git rebase -i --root               # Edit all commits in repository
```

### Interactive Commands

When the editor opens, you'll see:

```
pick a1b2c3d First commit
pick b3c4d5e Second commit
pick c5d6e7f Third commit

# Commands:
# p, pick = use commit
# r, reword = use commit, but edit the commit message
# e, edit = use commit, but stop for amending
# s, squash = use commit, but meld into previous commit
# f, fixup = like squash, but discard this commit's log message
# x, exec = run command (the rest of the line) using shell
# b, break = stop here (continue rebase later with 'git rebase --continue')
# d, drop = remove commit
# l, label = label current HEAD with a name
# t, reset = reset HEAD to a label
# m, merge = create a merge commit using the original merge message
```

### Common Rebase Scenarios

**Squashing commits**:
```
pick a1b2c3d First feature commit
squash b3c4d5e Bug fix in feature
squash c5d6e7f Another improvement
```
Result: All three commits merged into one.

**Reordering commits**:
```
pick c5d6e7f Third commit
pick a1b2c3d First commit
pick b3c4d5e Second commit
```
Result: Commits reordered (useful for logical grouping).

**Editing commit messages**:
```
pick a1b2c3d First commit
reword b3c4d5e Fix typo in message
pick c5d6e7f Third commit
```
Editor opens for second commit message only.

**Splitting commits**:
```bash
git rebase -i HEAD~1
# Mark commit with 'edit'
# When rebasing stops at that commit:
git reset HEAD~1                   # Undo the commit
git add <partial-files>            # Stage part of changes
git commit -m "First part"
git add <remaining-files>          # Stage remainder
git commit -m "Second part"
git rebase --continue
```

### Autosquash

Mark commits with fixup/squash prefix, then use `--autosquash`:

```bash
git commit -m "fixup! Previous commit message"
git commit -m "squash! Another commit message"
git rebase -i --autosquash main
# Automatically moves and marks commits
```

## Cherry-Pick

Apply specific commits from one branch to another without merging.

### Basic Cherry-Pick

```bash
git cherry-pick a1b2c3d            # Apply single commit
git cherry-pick a1b2c3d b3c4d5e    # Apply multiple commits
git cherry-pick a1b2c3d..c5d6e7f   # Apply range (exclusive of start)
git cherry-pick a1b2c3d^..c5d6e7f  # Apply range (inclusive of start)
```

### Cherry-Pick with Modifications

```bash
git cherry-pick -x <commit>        # Add cherry-pick reference
git cherry-pick --no-commit <commit>  # Stage changes without committing
git cherry-pick -e <commit>        # Edit commit message
git cherry-pick --signoff <commit> # Add Signed-off-by line
```

### Handling Cherry-Pick Conflicts

```bash
git cherry-pick <commit>
# Conflicts occur...
git status                         # See conflicted files
# Manually resolve conflicts
git add <resolved-files>
git cherry-pick --continue         # Complete cherry-pick
git cherry-pick --abort            # Cancel cherry-pick
```

### Cherry-Pick Use Cases

- **Backporting**: Apply bug fix to release branch
- **Selective merging**: Cherry-pick features without full branch merge
- **Maintenance**: Apply patches to multiple branches

## Bisect: Finding Bugs

Binary search through commits to find where a bug was introduced.

### Basic Bisect Workflow

```bash
git bisect start                   # Start bisect session
git bisect bad                     # Current commit is bad (has bug)
git bisect good v1.0              # Known good commit
# Git checks out middle commit
# Test the commit...
git bisect good                    # Or git bisect bad
# Continue until bug is found
git bisect reset                   # End bisect, return to original branch
```

### Automated Bisect

```bash
git bisect start
git bisect bad HEAD
git bisect good v1.0
git bisect run npm test            # Automatically run test script
# Git bisect runs test on each commit, reports when bug introduced
git bisect reset
```

### Bisect Example

```bash
#!/bin/bash
# test-script.sh - Returns 0 if good, 1 if bad
npm run build || exit 1
npm test | grep "FAILED" && exit 1
exit 0
```

```bash
git bisect start
git bisect bad HEAD
git bisect good v1.0
git bisect run ./test-script.sh
```

## Submodules

Include external repositories as subdirectories.

### Adding Submodules

```bash
git submodule add <url> <path>     # Add submodule
git submodule add -b main <url> <path>  # Add specific branch
git add .gitmodules <path>
git commit -m "Add: submodule"
```

This creates:
- `.gitmodules` file tracking submodule info
- Entry in `.git/config`
- Submodule directory with separate `.git` file

### Cloning with Submodules

```bash
git clone --recurse-submodules <url>  # Clone with submodules
git clone <url>                       # Clone without submodules
git submodule update --init            # Initialize submodules after clone
git submodule update --init --recursive # Initialize recursive submodules
```

### Updating Submodules

```bash
git submodule update                # Update to commit in .gitmodules
git submodule update --remote       # Update to latest from remote
git submodule update --remote --merge # Update and merge
git submodule foreach git pull     # Pull all submodules
```

### Managing Submodules

```bash
git submodule status               # Show submodule status
git submodule deinit <path>        # Deregister submodule
git rm <path>                      # Remove submodule
git config --file=.gitmodules --remove-section submodule.<name>  # Cleanup
```

## Subtrees

Alternative to submodules: merge external repo as subdirectory.

### Adding Subtrees

```bash
git subtree add --prefix=<path> <url> <branch>  # Add subtree
git subtree add --prefix=vendor/lib https://github.com/lib/repo main
```

### Updating Subtrees

```bash
git subtree pull --prefix=<path> <url> <branch>  # Pull updates
git subtree push --prefix=<path> <url> <branch>  # Push changes back
```

### Subtree vs Submodule

| Feature | Subtree | Submodule |
|---------|---------|-----------|
| Learning Curve | Steeper | Gentler |
| Cloning | Automatic | Requires init |
| Sharing Changes | Push back to original | Commit submodule pointer |
| Directory Tracking | Files in main repo | Separate .git file |
| Recommended For | Extracting external code | Using external library |

## Git Hooks

Automate actions at specific Git events.

### Hook Types

**Pre-commit**: Before committing
```bash
#!/bin/bash
# .git/hooks/pre-commit
npm run lint || exit 1
npm run format || exit 1
```

**Pre-push**: Before pushing
```bash
#!/bin/bash
# .git/hooks/pre-push
npm run test || exit 1
```

**Commit-msg**: Validate commit message
```bash
#!/bin/bash
# .git/hooks/commit-msg
if ! head -1 $1 | grep -qE "^(Add|Fix|Update|Remove):"; then
  echo "Commit message must start with: Add, Fix, Update, or Remove"
  exit 1
fi
```

**Post-merge**: After merging
```bash
#!/bin/bash
# .git/hooks/post-merge
npm install || exit 1
```

### Setting Up Hooks

```bash
# Manual setup (one-time)
chmod +x .git/hooks/pre-commit

# Using Husky (NPM projects)
npm install husky --save-dev
npx husky install
npx husky add .husky/pre-commit "npm run lint"
npx husky add .husky/pre-push "npm run test"

# Commit hooks to repo
git add .husky .gitignore
git commit -m "Add: git hooks"
```

### Common Hook Patterns

```bash
#!/bin/bash
# Prevent accidental commits to main
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" = "main" ]; then
  echo "Cannot commit directly to main. Use a feature branch."
  exit 1
fi

# Prevent large files
MAX_SIZE=5242880  # 5MB
STAGED_FILES=$(git diff --cached --name-only)
for FILE in $STAGED_FILES; do
  SIZE=$(stat -f%z "$FILE" 2>/dev/null || stat -c%s "$FILE" 2>/dev/null)
  if [ $SIZE -gt $MAX_SIZE ]; then
    echo "File $FILE is too large"
    exit 1
  fi
done
```

## Git Internals

Understanding the plumbing beneath Git's porcelain.

### Objects

Git stores four object types:

**Blob**: File contents
```bash
git hash-object <file>             # Calculate blob hash
git cat-file -p <hash>             # View blob contents
```

**Tree**: Directory structure
```bash
git cat-file -p <tree-hash>        # View tree
# Shows: [mode] [type] [hash] [name]
```

**Commit**: Snapshot with metadata
```bash
git cat-file -p <commit-hash>      # View commit
# tree: <tree-hash>
# parent: <parent-hash>
# author: name <email> timestamp
# committer: name <email> timestamp
# message
```

**Tag**: Reference to commit
```bash
git cat-file -p <tag-hash>         # View tag
```

### References

Branches and tags are simple references to commits:

```bash
cat .git/HEAD                      # Shows current branch
cat .git/refs/heads/main           # Shows main branch commit
cat .git/refs/tags/v1.0            # Shows tag commit
git symbolic-ref HEAD              # Show symbolic reference
```

### Repository Structure

```
.git/
  HEAD                 # Current branch reference
  refs/
    heads/            # Local branch references
    remotes/          # Remote branch references
    tags/             # Tag references
  objects/
    [hash]/           # Loose objects (blobs, trees, commits)
  pack/                # Packfiles (compressed objects)
  config              # Repository configuration
  hooks/              # Git hooks
  index               # Staging area
  logs/               # Reflog
```

## Signed Commits

Cryptographically sign commits with GPG.

### Setup GPG

```bash
gpg --list-secret-keys --keyid-format long  # List keys
gpg --gen-key                               # Generate key
git config --global user.signingKey <KEY-ID>  # Set default key
git config --global commit.gpgsign true     # Always sign
```

### Signing Commits

```bash
git commit -S                      # Sign commit
git commit -S -m "message"         # Sign with message
git commit --gpg-sign=<key-id>     # Sign with specific key
```

### Verifying Signatures

```bash
git log --show-signature           # Show signatures in log
git verify-commit <commit>         # Verify specific commit
git tag -v <tag>                   # Verify signed tag
```

## Git LFS (Large File Storage)

Handle large files efficiently without bloating repository.

### Setup

```bash
git lfs install                    # Initialize LFS
git lfs track "*.psd"              # Track file type
git add .gitattributes
git commit -m "Add: LFS tracking"
```

### Usage

```bash
git add large-file.zip             # Added to LFS automatically
git push origin main               # LFS file uploaded
git lfs ls-files                   # List tracked files
git lfs prune                      # Remove local LFS objects
```

## Monorepo Strategies

Managing multiple projects in single repository.

### Monorepo Structure

```
my-monorepo/
  packages/
    api/
      package.json
      src/
    web/
      package.json
      src/
    shared/
      src/
  .git
  package.json (root)
```

### Advantages

- Atomic commits across projects
- Shared dependencies
- Simplified tooling
- Easier refactoring

### Challenges

- Larger clone/fetch
- Builds slower
- Complex CI/CD

### Tools for Monorepos

- **npm/yarn workspaces**: Node.js packages
- **Lerna**: Package management
- **Nx**: Build system
- **Bazel**: Google's build system
- **Turborepo**: Fast monorepo build system

## Git Workflows

### Gitflow

Complex workflow with release and hotfix branches:

```
main (production)
  ├── hotfix-* (emergency fixes)
  │   └── merge back to main and develop
  │
develop (staging)
  ├── release-* (preparing release)
  │   └── merge to main and develop
  │
  ├── feature-* (feature development)
  │   └── merge back to develop
  └── bugfix-* (bug fixes)
      └── merge back to develop
```

**Best for**: Teams with scheduled releases

### Trunk-Based Development

Developers commit directly to main (or short-lived branches):

```bash
git checkout -b feature-x
git commit -m "Add feature"
git push origin feature-x
# PR review
git merge feature-x
```

- Branches live days, not weeks
- Frequent merges to main
- CI/CD catches issues quickly

**Best for**: High-performing teams, DevOps

### GitHub Flow

Simple workflow: main always deployable

```
main (always deployable)
  ├── feature-1 (short-lived)
  ├── feature-2 (short-lived)
  └── hotfix-1 (short-lived)
```

```bash
git checkout -b feature-name
# Make changes
git push origin feature-name
# Create PR, review, merge
```

**Best for**: Continuous deployment, web apps

### GitLab Flow

GitHub Flow plus environment branches:

```
main (staging)
  ├── production (merge main when ready)
  ├── feature-1
  └── feature-2
```

**Best for**: Teams needing pre-production testing

## Exercises

### Exercise 1: Interactive Rebase and Squashing

```bash
# Create feature with multiple commits
git switch -c feature-squash
echo "feature 1" > f1.txt
git add f1.txt
git commit -m "Add feature part 1"

echo "feature 2" > f2.txt
git add f2.txt
git commit -m "Add feature part 2"

echo "bug fix" >> f1.txt
git add f1.txt
git commit -m "Fix bug in part 1"

# Interactive rebase to squash
git rebase -i main
# Change last two commits to 'squash'
# Save and edit final message

# Now feature has one clean commit
git log --oneline
```

### Exercise 2: Cherry-Pick Across Branches

```bash
# Create commits on feature branch
git switch -c cherry-source
echo "commit A" > a.txt
git add a.txt
git commit -m "A"

echo "commit B" > b.txt
git add b.txt
git commit -m "B"

echo "commit C" > c.txt
git add c.txt
git commit -m "C"

# Cherry-pick only B to main
git switch main
git cherry-pick <hash-of-B>

# B is now on main, A and C not needed
git log --oneline
```

### Exercise 3: Bisect a Bug

```bash
# Create history with bug
git switch -c buggy-history
for i in {1..10}; do
  echo "version $i" > version.txt
  git add version.txt
  git commit -m "Version $i"
  # Introduce bug in version 6
  if [ $i -eq 6 ]; then
    echo "BUG" >> version.txt
    git add version.txt
    git commit --amend -m "Version $i (buggy)"
  fi
done

# Use bisect to find it
git bisect start
git bisect bad HEAD
git bisect good HEAD~10
# Test each commit, marking good/bad
# Git narrows down to buggy commit
git bisect reset
```

### Exercise 4: Implement Pre-commit Hook

```bash
# Create hook
cat > .git/hooks/pre-commit << 'EOF'
#!/bin/bash
CHANGES=$(git diff --cached --name-only)
for FILE in $CHANGES; do
  if [[ $FILE == *.js ]]; then
    echo "Linting $FILE..."
    # Would run: npx eslint "$FILE" || exit 1
  fi
done
exit 0
EOF

chmod +x .git/hooks/pre-commit

# Test it
echo "console.log('test')" > test.js
git add test.js
git commit -m "Add test"  # Hook runs
```

---

## Summary

Advanced Git mastery includes:

- **Interactive rebase**: Clean, logical commit history
- **Cherry-pick**: Selective commit application
- **Bisect**: Debugging through binary search
- **Submodules/Subtrees**: Managing external code
- **Hooks**: Automating Git workflows
- **Internals**: Understanding objects and references
- **Signed commits**: Cryptographic verification
- **LFS**: Handling large files
- **Workflows**: Team collaboration patterns

These tools enable professional development practices and team workflows at scale.

