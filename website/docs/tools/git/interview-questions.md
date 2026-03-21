---
title: Git Interview Questions
sidebar_label: Interview Questions
sidebar_position: 4
---

# Git Interview Questions & Answers

Comprehensive preparation for Git-focused technical interviews. 40+ questions covering beginner to advanced levels.

## Beginner Level

### 1. What is Git and why is it important?

**Answer:**

Git is a distributed version control system that tracks changes to source code over time. It's important because it:

- **Enables collaboration**: Multiple developers can work simultaneously
- **Maintains history**: Complete record of who changed what and when
- **Allows branching**: Parallel development without interference
- **Provides recovery**: Revert to previous states if needed
- **Supports workflows**: Enable various team collaboration patterns

Git differs from centralized systems (SVN) by giving every developer a complete repository copy, enabling offline work and decentralized workflows.

### 2. What is the difference between git init and git clone?

**Answer:**

```bash
# git init: Create new repository from scratch
git init
# Creates .git directory in current location
# Initializes empty Git database
# No commit history

# git clone: Copy existing repository
git clone https://github.com/user/repo
# Downloads entire repository history
# Sets up remote tracking
# Creates working directory with files
# Automatically sets origin as default remote
```

**When to use**:
- `git init`: Starting new project
- `git clone`: Contributing to existing project

### 3. Explain the three Git areas (working directory, staging, repository)

**Answer:**

```
┌─ Working Directory ─┐
│   (Your files)      │
│                     │
└──────── ↓ ──────────┘
   git add

┌─ Staging Area ─────┐
│   (Index)           │
│                     │
└──────── ↓ ──────────┘
   git commit

┌─ Repository ───────┐
│   (.git directory)  │
│   (Commits)         │
└─────────────────────┘
```

**Working Directory**: Your actual project files. Changes here are untracked until staged.

**Staging Area**: Intermediate layer where you select changes for next commit. Allows atomic commits of specific changes.

**Repository**: `.git` directory storing all commits, branches, and history. The permanent record.

**Example**:
```bash
echo "content" > file.txt     # Working directory
git add file.txt             # Staging area
git commit -m "message"      # Repository
```

### 4. What is a commit hash and why is it important?

**Answer:**

A commit hash is a SHA-1 checksum uniquely identifying a commit:

```
commit a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9t0
Author: Jane Doe <jane@example.com>
Date:   Mon Mar 21 10:30:45 2026 -0700

    Add user authentication
```

The hash (`a1b2c3d...`) is calculated from:
- Commit content (files, changes)
- Author information
- Timestamp
- Parent commit reference

**Why important**:
- **Unique identification**: Every commit is uniquely identifiable
- **Immutability**: Hash changes if content changes, detecting tampering
- **Referencing**: Use hash to access specific commits
- **Verification**: Git can verify commit integrity

**Short hash**:
```bash
git log --oneline              # Shows first 7 characters
a1b2c3d Add user authentication

# Sufficient to uniquely identify (in most repos)
git show a1b2c3d              # Works fine
```

### 5. What does HEAD mean in Git?

**Answer:**

HEAD is a **pointer to your current location** in the repository:

```bash
# Normally, HEAD points to latest commit of current branch
git log HEAD                   # Shows commits from HEAD
# HEAD → main → commit a1b2c3d

# HEAD can also point directly to a commit (detached HEAD)
git checkout a1b2c3d          # HEAD now points directly to this commit
# HEAD → a1b2c3d
```

**Common HEAD references**:
```bash
HEAD                          # Current location
HEAD~1                        # Parent of HEAD
HEAD~2                        # Grandparent of HEAD
HEAD^                         # Parent (alternative syntax)
main                          # Latest commit on main branch
origin/main                   # Latest on remote main
```

**Detached HEAD state**:
When HEAD points directly to a commit instead of a branch. Any commits made here are orphaned unless saved to a branch.

### 6. How do you create and switch between branches?

**Answer:**

```bash
# List branches
git branch                    # Local branches only
git branch -a                 # All (local + remote)

# Create branch
git branch feature-new        # Create without switching
git checkout -b feature-new   # Create and switch (old)
git switch -c feature-new     # Create and switch (new, preferred)

# Switch between branches
git checkout feature-new      # Old syntax
git switch feature-new        # New syntax (preferred)
git switch -                  # Switch to previous branch

# Delete branch
git branch -d feature-new     # Delete if merged
git branch -D feature-new     # Force delete
```

**Modern approach** (Git 2.23+):
```bash
git switch <branch>           # Clearer intent than checkout
git switch -c <branch>        # Create and switch
```

### 7. What is the difference between merge and rebase?

**Answer:**

Both combine commits from one branch into another, but differently:

**Merge**: Creates merge commit, preserves branching history

```bash
git merge feature

# Before:
# main ───●───●
#         │
#         └─ feature ───●───●
#
# After:
# main ───●───●───●(merge)
#         │   /
#         └─●───●
```

**Rebase**: Replays commits on top, creates linear history

```bash
git rebase main

# Before:
# main ───●───●
#         │
#         └─ feature ───●───●
#
# After:
# main ───●───●───●───●
#        (original)  (rebased copies)
```

**When to use**:

- **Merge**: Shared branches (main, develop), complex histories you want preserved
- **Rebase**: Local branches before PR, maintaining clean linear history

**Golden rule**: Never rebase commits already pushed to shared branches.

### 8. How do you resolve merge conflicts?

**Answer:**

Merge conflicts occur when Git can't automatically reconcile changes:

```
<<<<<<< HEAD
This is from main branch
=======
This is from feature branch
>>>>>>> feature
```

**Resolution process**:

```bash
# 1. See conflicted files
git status

# 2. Edit files manually
# Choose which version to keep, edit manually, or combine both

# 3. Stage resolved files
git add conflicted-file.txt

# 4. Complete the merge
git commit -m "Resolve: merge conflict"

# Or abort if not ready
git merge --abort
```

**Helpful tools**:
```bash
# Show only conflicted files
git diff --name-only --diff-filter=U

# Use merge tool (GUI)
git mergetool

# See both versions side-by-side
git diff HEAD...feature
```

### 9. What is .gitignore and how do you use it?

**Answer:**

`.gitignore` specifies which files Git should ignore (not track):

```bash
# Create .gitignore file
cat > .gitignore << 'EOF'
# Comments
*.log
*.tmp
node_modules/
dist/
.env
.env.local
*.key
secrets/
.DS_Store
EOF

git add .gitignore
git commit -m "Add: .gitignore"
```

**Common patterns**:
```
# Ignore specific file
secrets.env

# Ignore all .log files
*.log

# Ignore directories
node_modules/
build/

# Ignore files in any subdirectory
**/temp/

# Ignore .env except template
.env
!.env.example

# Negate to unignore
*.log
!important.log
```

**Untracking already-tracked files**:
```bash
git rm --cached file.txt      # Remove from tracking, keep locally
git commit -m "Remove: tracked file"
```

### 10. What is git pull and how does it differ from git fetch?

**Answer:**

Both download from remote, but:

**git fetch**: Download only, no merge

```bash
git fetch origin               # Downloads from remote
git fetch origin main          # Download specific branch
git fetch --all               # Fetch from all remotes

# Doesn't change working directory
# Updates remote-tracking branches (origin/main, etc.)
```

**git pull**: Fetch then merge/rebase

```bash
git pull                      # Fetch + merge (default)
git pull --rebase             # Fetch + rebase (preferred by many)
git pull origin main          # Pull specific branch
```

**Difference visualization**:

```bash
# Before
local main: A ─ B ─ C
remote: A ─ B ─ C ─ D ─ E

# After git fetch
local main: A ─ B ─ C
origin/main: A ─ B ─ C ─ D ─ E

# After git pull (merge)
local main: A ─ B ─ C ─ ─ ─ ─ ─ (merge) ─ D ─ E

# After git pull --rebase
local main: A ─ B ─ C ─ D ─ E
```

**When to use**:
- `fetch`: To see what's on remote without changing local
- `pull --rebase`: Keep clean, linear history (recommended)

## Intermediate Level

### 11. What is git reset and what are the differences between --soft, --mixed, and --hard?

**Answer:**

`git reset` moves HEAD to different commit and changes working directory accordingly:

```bash
git reset --soft HEAD~1       # Move HEAD, keep changes staged
git reset --mixed HEAD~1      # Move HEAD, keep changes unstaged
git reset --hard HEAD~1       # Move HEAD, discard changes completely
```

**Visualization**:

```
Before reset:  W ─ S ─ R  (W=working, S=staging, R=repository)
Commit A: ─ ● ─ ●

After --soft:  W ─ S ─ ●  (Changes back in staging)
After --mixed: W ─ ● ─ ●  (Changes in working directory)
After --hard:  ● ─ ● ─ ●  (All changes discarded)
```

**Example**:
```bash
git add file.txt
git commit -m "Add file"
# Realize commit message was wrong

git reset --soft HEAD~1
# Commit is undone, file.txt still staged
git commit -m "Add file (corrected message)"

# vs

git reset --hard HEAD~1
# Commit is undone, all changes discarded
# file.txt changes lost
```

**Common scenarios**:
```bash
# Undo commit, keep changes to edit
git reset --soft HEAD~1

# Unstage everything
git reset HEAD

# Discard uncommitted changes
git reset --hard HEAD
```

### 12. What is the difference between git revert and git reset?

**Answer:**

Both undo changes, but differently:

**git reset**: Moves HEAD backward, rewrites history (destructive)

```bash
git reset --hard HEAD~1
# ●─●─● ─── ●─●
# Commit is erased, history changed
# Don't use on shared branches!
```

**git revert**: Creates new commit that undoes changes (safe)

```bash
git revert HEAD~1
# ●─●─● ─── ●─●─●(revert)
# Original commit stays, new commit created
# Creates new commit that reverses changes
# Safe on shared branches
```

**Key differences**:

| Aspect | reset | revert |
|--------|-------|--------|
| Rewrites history | Yes | No |
| Creates new commit | No | Yes |
| Safe on shared | No | Yes |
| For collaboration | No | Yes |
| Permanent | Yes | Reversible |

**When to use**:

```bash
# Undo on LOCAL branch not yet pushed
git reset --hard HEAD~1

# Undo on SHARED branch (already pushed)
git revert HEAD~1

# Undo multiple commits
git revert HEAD~3..HEAD
```

### 13. What is a detached HEAD state and how do you recover from it?

**Answer:**

Detached HEAD occurs when HEAD points directly to a commit instead of a branch:

```bash
# Normal state
git switch main
# HEAD → main → commit a1b2c3d

# Detached HEAD state
git checkout a1b2c3d
# HEAD → a1b2c3d (not pointing to any branch)
```

**Recovery**:

```bash
# If you made commits in detached state
git reflog                          # Find your commit
commit a1b2c3d HEAD@0: (detached)

# Create branch from current HEAD
git switch -c recovery-branch       # Save your work
git log                             # Verify commits

# Alternatively, if no new commits
git switch main                     # Go back to branch
```

**Use cases for detached HEAD**:
- Inspect historical code
- Test specific commit
- Create hotfix from past version

**Prevention**:
```bash
# Always work on branches
git switch -c feature-name          # Create branch
git checkout -b feature-name        # Alternative
```

### 14. Explain git cherry-pick and when you would use it

**Answer:**

Cherry-pick applies specific commits from one branch to another:

```bash
git cherry-pick a1b2c3d            # Apply single commit
git cherry-pick a1b2c3d b3c4d5e    # Apply multiple
git cherry-pick a1b2c3d..c5d6e7f   # Apply range
```

**How it works**:
1. Takes commit from source branch
2. Calculates the diff (changes)
3. Applies diff to target branch as new commit
4. Creates new commit with same message but different hash

**Use cases**:

```bash
# 1. Backport bug fix
git switch hotfix
# Fix bug on hotfix branch
git switch main
git cherry-pick <bugfix-commit>     # Apply to main

# 2. Selective merging
git switch feature-a
# Implement features A, B, C
git switch main
git cherry-pick <commit-B>          # Only cherry-pick B

# 3. Maintenance patches
git switch release-1.0
git cherry-pick <security-patch>    # Apply security fix to old release
```

**With conflicts**:
```bash
git cherry-pick <commit>
# Conflicts occur...
# Resolve conflicts manually
git add resolved-file
git cherry-pick --continue          # Complete
# or
git cherry-pick --abort             # Cancel
```

### 15. What does git stash do and when would you use it?

**Answer:**

Stash temporarily saves uncommitted changes without committing:

```bash
# Save work in progress
git stash                           # Stash all changes
git stash save "WIP: feature X"     # Stash with description
git stash -u                        # Include untracked files
git stash -a                        # Include ignored files

# List stashes
git stash list                      # Show all stashes
# stash@0: WIP on main: a1b2c3d
# stash@1: feature work
# stash@2: bug fix

# Retrieve stashed work
git stash pop                       # Apply and remove latest
git stash pop stash@1               # Apply specific stash
git stash apply                     # Apply without removing
git stash apply stash@1             # Apply specific

# Other operations
git stash drop stash@0              # Delete stash
git stash clear                     # Delete all stashes
git stash show -p stash@0           # View stash contents
```

**Use cases**:
```bash
# Switch branches without committing
echo "WIP changes" > file.txt
git stash                           # Save
git switch other-branch             # Switch safely
git switch -                        # Return to branch
git stash pop                       # Resume work

# Pull latest without losing work
git stash
git pull
git stash pop

# Create branch from stash
git stash branch new-branch         # Creates branch and applies stash
```

### 16. What is the difference between git push and git push --force?

**Answer:**

**git push**: Normal push, fails if remote has diverged

```bash
git push origin main

# Rejects if remote main has new commits you don't have
# Forces you to pull and reconcile first
# Safe for collaboration
```

**git push --force**: Overwrites remote with your history

```bash
git push --force origin main        # Dangerous!

# Overwrites remote completely
# Discards any work others pushed
# Breaks for teammates
```

**Better alternative**:
```bash
git push --force-with-lease origin main

# Safer than --force
# Only works if remote hasn't changed since your fetch
# Prevents accidental data loss
```

**When force-push is acceptable**:
```bash
# On personal branches before PR
git rebase main                     # Rewrite history locally
git push --force-with-lease         # Update your PR

# Never on: main, develop, shared branches
```

**Recovering from force-push**:
```bash
git reflog                          # Find overwritten commit
git reset --hard <hash>             # Recover
git push --force-with-lease         # Push back
```

### 17. How do you squash commits before merging?

**Answer:**

Squashing combines multiple commits into one for cleaner history:

**Method 1: Interactive rebase**
```bash
git rebase -i main                  # Interactive rebase
# In editor:
# pick a1b2c3d First commit
# squash b3c4d5e Second commit
# squash c5d6e7f Third commit

# Result: One commit with combined message
```

**Method 2: Merge with squash**
```bash
git switch main
git merge --squash feature          # Squash feature commits
git commit -m "Add feature"         # Commit squashed changes
# feature branch still exists, just not in history
```

**Method 3: Reset and recommit**
```bash
git reset --soft main               # Undo commits, keep changes staged
git commit -m "Combined message"    # New single commit
```

**Workflow with squashing**:
```bash
git switch -c feature-new main
# Make multiple commits for development
git commit -m "Part 1"
git commit -m "Part 2"
git commit -m "Fix bug from part 1"

# Before PR, squash locally
git rebase -i main
# squash everything to 1-2 logical commits

# Push PR
git push origin feature-new
```

### 18. Explain git bisect and when you would use it

**Answer:**

Bisect uses binary search to find which commit introduced a bug:

```bash
git bisect start                    # Start bisect session
git bisect bad                      # Current is bad
git bisect good v1.0                # v1.0 was good

# Git checks out middle commit
npm test                            # Test it
git bisect good                     # Mark as good (or bad)

# Continue until bug found
git bisect reset                    # End, return to original branch
```

**Automated bisect**:
```bash
#!/bin/bash
# test.sh - Returns 0 if good, 1 if bad
npm run build || exit 1
npm test || exit 1

git bisect start
git bisect bad HEAD
git bisect good v1.0
git bisect run ./test.sh            # Automate testing

# Finds buggy commit automatically
```

**When to use**:
- Bug introduced recently, unclear when
- Need to identify exact commit that broke something
- Regression appeared between known versions

**Result**:
```
Bisect found first bad commit: a1b2c3d
Author: name
Date: date
    Commit message of buggy change
```

### 19. What is git blame and how is it useful?

**Answer:**

Git blame shows who last modified each line:

```bash
git blame <file>                    # Show author per line
git blame -L 10,20 <file>           # Show specific lines
git blame -C <file>                 # Detect copy/move
git blame -C -C <file>              # Detect cross-file moves
```

**Output**:
```
a1b2c3d (Author Name 2026-03-21 10:30:45 -0700 1) int main() {
b3c4d5e (Other Person 2026-03-20 15:22:10 -0700 2)   return 0;
```

**Use cases**:
```bash
# Understand why change was made
git blame <file> | grep "suspicious line"
git show <commit>                   # See full commit

# Find who to ask about code
git blame tricky-function.js
git show <commit>                   # See context

# Identify recent refactoring
git blame --reverse                 # See oldest changes first
```

**With git log**:
```bash
# Find commit that modified line
git log -p -S "specific-line" <file>  # Search for line changes
git log -L 10,20:<file>             # History of lines 10-20
```

### 20. What are remotes in Git and how do you manage them?

**Answer:**

Remotes are references to repositories on servers:

```bash
git remote                          # List remotes
git remote -v                       # List with URLs
git remote show origin              # Details about remote

# Add remote
git remote add origin https://github.com/user/repo
git remote add upstream https://github.com/original/repo

# Modify remote
git remote set-url origin <new-url>
git remote rename origin upstream

# Remove remote
git remote remove origin
```

**Common remotes**:

```
origin      # Your fork or personal repository
upstream    # Original project repository
heroku      # Deployment platform
production  # Production server
```

**Tracking branches**:
```bash
# Local branch tracking remote
git branch -u origin/main           # Set upstream for current
git branch -vv                      # Show tracking info

# Clone with specific remote
git clone -b main --origin upstream <url>
```

**Multi-remote workflow**:
```bash
# Fork on GitHub, clone fork
git clone <your-fork>

# Add upstream
git remote add upstream <original-repo>

# Keep fork synced
git fetch upstream                  # Get updates from original
git rebase upstream/main            # Update local
git push origin main                # Push to fork
```

## Advanced Level

### 21. Explain the difference between fast-forward and three-way merges

**Answer:**

**Fast-forward merge**: When target hasn't changed since branch creation

```bash
git merge feature

# Before:
main ───A───B
         │
         └─ feature ───C───D

# After (fast-forward):
main ───A───B───C───D
             └─feature (pointer moved)

# No merge commit created, history is linear
git log --oneline
D (HEAD -> main, feature)
C
B
A
```

**Three-way merge**: When both branches have diverged

```bash
git merge feature

# Before:
main ───A───B───E
         │
         └─ feature ───C───D

# After:
main ───A───B───E───M(merge)
         │       /
         └─C───D

# Merge commit created with two parents
git log --oneline
M (merge commit)
E
D
C
B
A
```

**Forcing merge commit**:
```bash
git merge --no-ff feature          # Always create merge commit even for FF

# Useful for:
# - Keeping branch metadata in log
# - Explicit record of feature branch
# - --graph visualization
```

**Visualization**:
```bash
git log --graph --oneline --all

* a1b2c3d Merge branch 'feature'
|\
| * b3c4d5e Feature commit 2
| * c5d6e7f Feature commit 1
* | d6e7f8g Main commit
|/
* e8f9g0h Initial commit
```

### 22. What is an interactive rebase and what are the common commands?

**Answer:**

Interactive rebase allows editing commits before merging. Opens editor with commit list:

```bash
git rebase -i main
# Or
git rebase -i HEAD~3                # Last 3 commits
```

**Editor commands**:
```
pick a1b2c3d First commit
pick b3c4d5e Second commit
pick c5d6e7f Third commit

# Commands:
# p, pick: use commit unchanged
# r, reword: edit commit message
# e, edit: use commit but stop for amending
# s, squash: use commit, combine with previous
# f, fixup: like squash but discard message
# x, exec: run command
# d, drop: remove commit
```

**Common scenarios**:

**Squash commits**:
```
pick a1b2c3d Add feature
squash b3c4d5e Bug fix
squash c5d6e7f Improvement
# Result: One commit
```

**Reorder commits**:
```
pick c5d6e7f Third commit
pick a1b2c3d First commit
pick b3c4d5e Second commit
# Changes order
```

**Edit message**:
```
pick a1b2c3d First
reword b3c4d5e Second
pick c5d6e7f Third
# Editor opens for second commit only
```

**Autosquash**:
```bash
git commit -m "fixup! Previous commit message"
git rebase -i --autosquash main
# Automatically squashes marked commits
```

### 23. What is a merge conflict and how do you handle complex conflicts?

**Answer:**

Merge conflict occurs when Git can't automatically combine changes:

```
<<<<<<< HEAD
Code from main
=======
Code from feature
>>>>>>> feature
```

**Simple resolution**:
```bash
git status                          # See conflicted files
# Edit file, choose which version
git add resolved-file
git commit                          # Complete merge
```

**Complex conflict strategies**:

**1. Use theirs or ours**:
```bash
# Keep all changes from feature
git checkout --theirs <file>
git add <file>

# Keep all changes from main
git checkout --ours <file>
git add <file>
```

**2. Merge tool GUI**:
```bash
git mergetool                       # Opens visual merge tool
# See both versions side-by-side, choose resolution
```

**3. Three-way view**:
```bash
git diff --diff-filter=U            # Show only conflicts
git diff HEAD..<branch>             # See what changed
```

**4. When to abort and rebase**:
```bash
# If merge is too complex
git merge --abort                   # Cancel merge
git rebase main                     # Try rebase instead
```

**5. Conflict resolution with commits**:
```bash
# For large conflicts, resolve incrementally
git merge --no-commit --no-ff <branch>  # Start but don't commit
# Resolve files one by one
git add <file>
git commit                          # Partial resolution
```

### 24. Explain git submodules and when to use them

**Answer:**

Submodules embed one repository inside another:

```bash
# Add submodule
git submodule add <url> <path>
git add .gitmodules <path>
git commit -m "Add: submodule"

# .gitmodules file created:
[submodule "vendor/lib"]
    path = vendor/lib
    url = https://github.com/user/lib
```

**Cloning with submodules**:
```bash
# With submodules
git clone --recurse-submodules <url>

# Without (then init later)
git clone <url>
git submodule init
git submodule update
```

**Updating submodules**:
```bash
git submodule update                # Update to commit in .gitmodules
git submodule update --remote       # Update to latest from remote
git submodule foreach git pull     # Pull all submodules
```

**When to use**:
- Large external library you don't control
- Shared dependency across projects
- Want to track specific version

**Disadvantages**:
- Cloning is more complex
- Updates require commits
- Steep learning curve

**Alternative (Subtree)**:
```bash
# Subtree merges external repo as directory
git subtree add --prefix=vendor/lib <url> <branch>
git subtree pull --prefix=vendor/lib <url> <branch>
```

**Subtree vs Submodule**:

| Feature | Subtree | Submodule |
|---------|---------|-----------|
| Cloning | Auto | Requires init |
| Simplicity | Simpler | Complex |
| Tracking | Full files | Pointer |
| Pushback | Yes | No |

### 25. What are Git hooks and how do you use them?

**Answer:**

Git hooks are scripts running at specific Git events:

**Hook types**:
- `pre-commit`: Before commit
- `pre-push`: Before push
- `commit-msg`: Validate message
- `post-merge`: After merge
- `post-checkout`: After checkout
- `pre-rebase`: Before rebase

**Setup hook**:
```bash
#!/bin/bash
# .git/hooks/pre-commit
npm run lint || exit 1              # Prevent commit if linting fails
npm run format
git add .                           # Re-add formatted files

chmod +x .git/hooks/pre-commit
```

**Common hooks**:

**Prevent direct commits to main**:
```bash
#!/bin/bash
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" = "main" ]; then
  echo "Cannot commit to main directly"
  exit 1
fi
```

**Prevent large files**:
```bash
#!/bin/bash
MAX_SIZE=5242880  # 5MB
for FILE in $(git diff --cached --name-only); do
  SIZE=$(stat -f%z "$FILE" 2>/dev/null || stat -c%s "$FILE")
  if [ $SIZE -gt $MAX_SIZE ]; then
    echo "File $FILE is too large (max 5MB)"
    exit 1
  fi
done
```

**Using Husky for Node.js projects**:
```bash
npm install husky --save-dev
npx husky install
npx husky add .husky/pre-commit "npm run lint && npm run test"
npx husky add .husky/pre-push "npm run build"

# Hooks installed and active
git commit -m "test"                # Runs lint and test
```

### 26. What is Git LFS and when would you use it?

**Answer:**

Git LFS (Large File Storage) handles large files without bloating repository:

```bash
# Setup
git lfs install                     # Initialize LFS
git lfs track "*.psd"               # Track file type
git add .gitattributes
git commit -m "Add: LFS tracking"

# Usage
git add large-file.zip              # Automatically added to LFS
git push                            # Uploads LFS file
git lfs ls-files                    # List tracked files
git lfs prune                       # Remove local LFS objects
```

**How it works**:
```
git add large-file.zip
├─ Git stores pointer in repository (1KB)
└─ LFS server stores actual file (500MB)

git push
├─ Pushes pointer to GitHub
└─ Uploads file to LFS server
```

**When to use**:
- Binary files: images, videos, datasets
- Compiled artifacts, large archives
- When repo becomes too large

**Limitations**:
- Additional LFS server needed
- Bandwidth limits on free tier
- Not all Git platforms support LFS

### 27. What are Git workflows and how do they differ?

**Answer:**

Git workflows are team conventions for branching and merging:

**Gitflow: Complex with release cycles**
```
main (production)
  ├── hotfix-* (emergency fixes)
  └── release-* (release prep)

develop (staging)
  ├── feature-* (feature development)
  └── bugfix-* (bug fixes)
```

**Trunk-based development: Simple, daily merges**
```
main
  ├── feature-1 (1-2 days)
  ├── feature-2 (1-2 days)
  └── hotfix-1 (hours)
```

**GitHub Flow: Main always deployable**
```
main (always production-ready)
  ├── feature-1
  ├── feature-2
  └── bugfix-1
```

**GitLab Flow: Environment branches**
```
main (staging)
  ├── production (merge main when ready)
  ├── feature-1
  └── feature-2
```

**Choosing workflow**:
- **Scheduled releases**: Gitflow
- **Continuous deployment**: GitHub Flow / Trunk-based
- **Staged environments**: GitLab Flow

### 28. How would you recover a deleted branch?

**Answer:**

Branch deletion just removes pointer; commits still exist:

```bash
# Branch deleted
git branch -D feature

# Find commit
git reflog                          # Shows all recent commits
# a1b2c3d HEAD@1: checkout: moving from feature to main
# b3c4d5e HEAD@2: commit: Add feature

# Recover
git switch -c feature a1b2c3d       # Recreate branch at commit
# or
git checkout -b feature a1b2c3d     # Alternative syntax
```

**For remote deleted branches**:
```bash
git branch -a                       # See branches
# origin/deleted-branch (still visible)

# Recreate locally
git checkout deleted-branch         # Git finds it
# or
git switch deleted-branch
```

**Prevention**:
```bash
# Protect important branches on GitHub
# Settings → Branch protection rules
# Require pull request reviews
# Dismiss stale reviews
```

### 29. What is git reflog and why is it important?

**Answer:**

Reflog (reference log) records all commits you've been at:

```bash
git reflog                          # Show reference log
# a1b2c3d HEAD@0: reset: moving to a1b2c3d
# b3c4d5e HEAD@1: commit: Add feature
# c5d6e7f HEAD@2: checkout: moving to main

git reflog show main                # Reflog for specific branch
```

**Recovery uses**:
```bash
# Recover lost commits
git reset --hard a1b2c3d

# Recover deleted branch
git checkout -b recovery a1b2c3d

# See what happened before force-push
git reflog
```

**Why important**:
- Safety net for destructive operations
- See entire history of HEAD
- Recover from mistakes

**Cleanup**:
```bash
git reflog expire --expire=now --all  # Clear reflog
git gc                                # Garbage collection
```

### 30. How do you handle secrets and sensitive data in Git?

**Answer:**

Never commit secrets; use environment files:

**Setup .gitignore**:
```bash
cat > .gitignore << 'EOF'
.env
.env.local
.env.*.local
*.key
secrets/
credentials.json
private/
EOF

git add .gitignore
git commit -m "Add: .gitignore for secrets"
```

**Use .env files**:
```bash
# .env (ignored)
DATABASE_PASSWORD=secret123
API_KEY=sk-1234567890

# .env.example (committed)
DATABASE_PASSWORD=change_me
API_KEY=change_me
```

**If secret already committed**:
```bash
# Remove from history
git filter-branch --tree-filter 'rm -f secrets.env' HEAD

# Or use tool
git secret                          # Encrypts .gitignore files

# Force push (careful!)
git push --force-with-lease
```

**Better approaches**:
- Use environment variables at deployment
- Use secrets management (GitHub Secrets, AWS Secrets Manager)
- Use git-crypt for encryption
- Use git-secret for GPG-encrypted files

### 31. What is the difference between fetch and pull, and when would you use each?

**Answer:**

Already covered in Q#10, but expanded:

**git fetch**: Safe, updates remote-tracking branches only

```bash
git fetch origin                    # Get updates
git fetch --all                     # Fetch all remotes
git fetch --prune                   # Remove deleted branches
```

**Typical workflow**:
```bash
git fetch origin
git log origin/main..main           # What I have that remote doesn't
git log main..origin/main           # What remote has I don't
git rebase origin/main              # Update without merge
```

**git pull**: Fetch then merge/rebase

```bash
git pull                            # Fetch + merge
git pull --rebase                   # Fetch + rebase (preferred)
git pull --ff-only                  # Only if fast-forward possible
```

**When to use**:

```bash
# When reviewing changes first
git fetch origin
git diff main origin/main
git merge origin/main              # Only if looks good

# Quick update of local branch
git pull --rebase                   # Fewer merge commits
```

**Team recommendation**:
```bash
git config pull.rebase true         # Prefer rebase to merge
git config --global pull.rebase true  # Set globally
```

### 32. How do you synchronize a fork with upstream?

**Answer:**

Keep fork updated with original repository:

```bash
# Setup (one-time)
git remote add upstream https://github.com/original/repo
git remote -v
# origin: your fork
# upstream: original repo

# Synchronize
git fetch upstream                  # Get latest from original
git switch main
git rebase upstream/main            # Update local with original
git push origin main                # Push to fork

# Or merge instead of rebase
git merge upstream/main
git push origin main
```

**Workflow**:
```bash
# Clone your fork
git clone <your-fork>
cd repo

# Add upstream
git remote add upstream <original>

# Before creating PR
git fetch upstream
git rebase upstream/main            # Catch up with original
git push origin feature-branch      # Push to fork

# Create PR from fork to original
# GitHub shows: your-fork:feature → original:main
```

**Keeping in sync regularly**:
```bash
# Add to shell aliases
alias sync='git fetch upstream && git rebase upstream/main && git push origin'

# Use git workflow
git config --global alias.sync 'pull --rebase upstream'
git sync main
```

### 33. What is a squash merge and when would you use it?

**Answer:**

Squash merge combines all commits from branch into one:

```bash
# Normal merge
git merge feature
# Creates merge commit, preserves feature history

# Squash merge
git merge --squash feature
git commit -m "Add: feature X"
# All feature commits combined into one
```

**Before and after**:
```
Before:
main ───A───B
         │
         └─ feature ───C───D───E

Normal merge:
main ───A───B───M(merge)
                /
            C─D─E

Squash merge:
main ───A───B───F(squashed C+D+E)
```

**When to use**:
- Feature branch with many development commits
- Clean up noisy commit history on main
- Small features that don't need detailed history

**When NOT to use**:
- Complex features where history matters
- Shared branches where history is important
- When audit trail is critical

**Workflow**:
```bash
# Develop feature with multiple commits
git switch -c feature-x
git commit -m "WIP: part 1"
git commit -m "WIP: part 2"
git commit -m "Fix: bug in part 1"

# Before PR, squash
git rebase -i main
# Squash all to one commit

# Or merge with squash
git switch main
git merge --squash feature-x
git commit -m "Add: feature X"
```

### 34. How do you handle rebasing with conflicts?

**Answer:**

Conflicts during rebase are handled similarly to merge, but different mindset:

```bash
git rebase main
# Conflict during rebase!
# Rebase is paused at conflicted commit

git status                          # See conflicts
# Resolve manually
git add resolved-file
git rebase --continue               # Move to next commit
# May have more conflicts
# Repeat until rebase complete
```

**Visualization**:
```
Before rebase:
feature ───C───D───E
        /
main ───A───B

During rebase at C (conflict):
HEAD → C (detached, conflict)
main ───A───B
feature ───C───D───E (unchanged until rebase done)

After resolving and continuing:
HEAD → C' (rebased, conflict resolved)
main ───A───B

After rebase complete:
feature ───A───B───C'───D'───E'
```

**Handling workflow**:
```bash
# Start rebase
git rebase main

# Conflict occurs
git status

# Option 1: Resolve and continue
git add <resolved-file>
git rebase --continue

# Option 2: Skip this commit
git rebase --skip

# Option 3: Abort rebase
git rebase --abort                  # Restore original state
```

**Complex conflict?**:
```bash
# Use merge instead
git rebase --abort
git merge main                      # Easier to manage conflicts
```

### 35. What is the difference between git checkout and git switch, and why was switch introduced?

**Answer:**

Both switch branches, but `checkout` does more:

**git checkout**: Multiple purposes (overloaded)
```bash
git checkout main               # Switch branch
git checkout -b feature         # Create and switch
git checkout -- file            # Discard file changes
git checkout a1b2c3d            # Detached HEAD
git checkout -- .               # Discard all changes
```

**git switch**: Focused on branch switching (newer, clearer)
```bash
git switch main                 # Switch branch
git switch -c feature           # Create and switch
git switch -d a1b2c3d           # Detached HEAD (explicit)

# File operations use git restore instead
git restore file                # Discard file changes
git restore -- .                # Discard all changes
```

**Why switch was introduced**:
```
checkout is overloaded
├── Switch branches (intended use)
├── Discard changes (unrelated)
├── Create branches (partially)
└── Enter detached HEAD

switch is focused
├── Switch branches
├── Create branches
└── Explicit detached mode

restore is focused
├── Discard changes
├── Restore file versions
└── Unstage files
```

**Modern recommendation**:
```bash
# Use switch for branching
git switch <branch>
git switch -c <branch>
git switch -d <commit>

# Use restore for file operations
git restore <file>              # Discard changes
git restore --staged <file>     # Unstage
git restore --source=<commit> <file>  # Restore version

# Still use checkout for complex operations
# But new users should learn switch
```

## More Advanced Scenarios

### 36. How would you undo a public commit that others have pulled?

**Answer:**

Never use reset on public/shared commits. Use revert:

```bash
# Bad (don't do this on shared branches)
git reset --hard HEAD~1
git push --force                    # Breaks others' clones

# Good
git revert HEAD                     # Create new commit undoing changes
git push                            # Safe to push

# Or communicate and coordinate force-push
# 1. Notify team
# 2. Everyone stashes work
# 3. Force-push
# 4. Team re-pulls
```

### 37. What is the correct way to rename a branch?

**Answer:**

```bash
# Rename locally
git branch -m old-name new-name

# Rename current branch
git branch -m new-name

# Push new branch
git push origin new-name

# Delete old on remote
git push origin --delete old-name

# Or team members:
git fetch origin
git switch new-name
```

### 38. How do you handle large binary files in Git?

**Answer:**

Use Git LFS:

```bash
git lfs install
git lfs track "*.psd"
git add .gitattributes
git add large-file.psd
git push
```

### 39. Explain commit graph visualization

**Answer:**

```bash
git log --graph --oneline --all --decorate

*   a1b2c3d (HEAD -> main) Merge pull request #123
|\
| * b3c4d5e (develop) Add feature
* | c5d6e7f Update documentation
|/
* d6e7f8g (tag: v1.0.0) Release 1.0.0
```

### 40. What is the best practice for commit messages?

**Answer:**

```
Add: feature name

Longer description explaining why this change.
What problem does it solve?
Any breaking changes?

Fixes #123
Relates to #456
```

**Format**:
- First line: Subject (under 50 characters)
- Blank line
- Body: Explanation (wrapped at 72 characters)
- References: Issue numbers, related commits

---

## Summary

Master these interview questions to demonstrate:
- Solid Git fundamentals
- Understanding of branching and merging
- Knowledge of history manipulation
- Best practices for collaboration
- Recovery techniques
- Advanced workflows

The most important concepts:
1. Understand the three areas (working, staging, repository)
2. Know merge vs rebase and when to use each
3. Understand reset vs revert
4. Comfortable with conflict resolution
5. Know recovery techniques (reflog, bisect)

