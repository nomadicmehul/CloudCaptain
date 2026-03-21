---
title: Git & GitHub Cheat Sheet
sidebar_label: Cheat Sheet
sidebar_position: 2
---

# Git & GitHub Cheat Sheet

A comprehensive quick reference for 150+ Git commands and GitHub CLI operations. Organized by category for fast lookup.

## Setup & Configuration

### Initial Configuration

```bash
git config --global user.name "Your Name"
git config --global user.email "your@email.com"
git config --global core.editor "nano"
git config --global core.pager "less"
git config --global init.defaultBranch main
git config --list                          # View all settings
git config user.name                       # View specific setting
git config --unset user.name              # Remove setting
git config --global --unset user.email    # Remove global setting
```

### Per-Repository Configuration

```bash
git config user.name "Work Name"           # Set for this repo only
git config core.ignoreCase false          # Case-sensitive filenames
git config pull.rebase true               # Default to rebase on pull
git config push.autoSetupRemote true      # Auto-set upstream on push
git config advice.detachedHead false      # Disable detached HEAD warning
```

### Creating Repositories

```bash
git init                                   # Initialize in current directory
git init <directory>                       # Create new directory and init
git clone <url>                            # Clone remote repository
git clone <url> <directory>                # Clone to specific directory
git clone --depth 1 <url>                  # Shallow clone (latest commit only)
git clone --branch <branch> <url>          # Clone specific branch
git clone --no-checkout <url>              # Clone without working files
```

### Remote Configuration

```bash
git remote add origin <url>                # Add remote
git remote remove origin                   # Remove remote
git remote rename origin upstream          # Rename remote
git remote set-url origin <new-url>        # Change remote URL
git remote -v                              # List remotes with URLs
git remote show origin                     # Show remote details
git remote get-url origin                  # Get remote URL
```

## Branching

### Creating and Listing Branches

```bash
git branch                                 # List local branches
git branch -a                              # List all branches (local + remote)
git branch -r                              # List remote branches only
git branch -v                              # List with last commit
git branch --merged                        # List merged branches
git branch --no-merged                     # List unmerged branches
```

### Creating Branches

```bash
git branch <branch-name>                   # Create new branch
git branch -b <branch-name>                # Create from current commit
git branch <branch-name> <commit-hash>     # Create from specific commit
git branch -c <old-name> <new-name>        # Copy branch
git branch -m <old-name> <new-name>        # Rename branch
git branch -m <new-name>                   # Rename current branch
```

### Switching Branches

```bash
git checkout <branch-name>                 # Switch (older syntax)
git switch <branch-name>                   # Switch (newer syntax)
git checkout -b <branch-name>              # Create and switch (older)
git switch -c <branch-name>                # Create and switch (newer)
git checkout -                             # Switch to previous branch
git switch -                               # Switch to previous branch
git checkout --track origin/<branch>       # Track remote branch
git switch --track origin/<branch>         # Track remote branch
```

### Deleting Branches

```bash
git branch -d <branch-name>                # Delete if merged
git branch -D <branch-name>                # Force delete
git branch -d origin/<branch-name>         # Delete remote tracking branch
git push origin --delete <branch-name>     # Delete remote branch
git push origin :<branch-name>             # Alternative delete syntax
git push origin --all --prune              # Clean up remote branches
```

### Branch Information

```bash
git branch -vv                             # Show tracking info
git rev-parse --abbrev-ref HEAD            # Get current branch name
git symbolic-ref refs/heads/HEAD           # Get HEAD symbolic reference
git branch -u origin/main                  # Set upstream for current branch
git branch --set-upstream-to=origin/main   # Set upstream (long form)
git branch -u origin/main feature          # Set upstream for other branch
git branch --unset-upstream                # Remove upstream tracking
```

## Checkout and Switch

### Working with Commits

```bash
git checkout <commit-hash>                 # Go to specific commit (detached)
git checkout HEAD~1                        # Go back one commit
git checkout HEAD~3                        # Go back three commits
git checkout main~1                        # Go to parent of main
git switch -d <commit-hash>                # Switch to detached HEAD
```

### Restoring Files

```bash
git checkout <file>                        # Discard changes in working directory
git checkout <commit> -- <file>            # Restore file from specific commit
git checkout HEAD -- <file>                # Restore file from HEAD
git switch --detach <commit>               # Detached HEAD
```

## Staging & Committing

### Adding Files to Staging

```bash
git add <file>                             # Stage specific file
git add <file1> <file2> <file3>            # Stage multiple files
git add .                                  # Stage all changes
git add *.js                               # Stage by pattern
git add -A                                 # Stage all (new, modified, deleted)
git add -u                                 # Stage modified and deleted only
git add -p                                 # Interactive staging (patch mode)
git add -i                                 # Interactive add menu
git add --intent-to-add <file>             # Track file without staging
```

### Unstaging Files

```bash
git reset HEAD <file>                      # Unstage file (keep changes)
git reset HEAD <file1> <file2>             # Unstage multiple files
git reset HEAD                             # Unstage all
git reset -p HEAD                          # Interactive unstaging
git restore --staged <file>                # Unstage (newer syntax)
```

### Viewing Changes

```bash
git status                                 # Show working directory status
git status -s                              # Short status format
git diff                                   # Show unstaged changes
git diff HEAD                              # Show all changes vs HEAD
git diff --staged                          # Show staged changes
git diff --cached                          # Show staged (alternative)
git diff <branch1> <branch2>               # Compare branches
git diff <commit1> <commit2>               # Compare commits
git diff <commit>^..<commit>               # Changes in specific commit
git diff --word-diff                       # Show word-level diff
git diff --stat                            # Show summary of changes
git show <commit>                          # Show specific commit
git show <commit>:<file>                   # Show file at specific commit
```

### Committing

```bash
git commit -m "message"                    # Commit with message
git commit -m "title" -m "body"            # Commit with title and body
git commit                                 # Open editor for message
git commit -a                              # Stage and commit tracked files
git commit -am "message"                   # Stage tracked and commit
git commit --amend                         # Modify last commit
git commit --amend --no-edit               # Amend without changing message
git commit --allow-empty -m "message"      # Create empty commit
git commit -v                              # Show diff in editor
git commit --signoff                       # Add Signed-off-by line
git commit -S                              # Sign commit with GPG
git commit --date="date string"            # Set custom commit date
```

### Commit History

```bash
git log                                    # Show commit history
git log --oneline                          # Condensed log
git log -n 10                              # Show last 10 commits
git log --all                              # Show all commits (all branches)
git log --graph --oneline --all            # Visual branch history
git log --author="name"                    # Filter by author
git log --grep="pattern"                   # Search commit messages
git log -S "text"                          # Search for code changes
git log -p                                 # Show changes with history
git log --stat                             # Show file statistics
git log --reverse                          # Reverse chronological order
git log --format="%h %s"                   # Custom format
git log --since="2 weeks ago"              # Filter by date
git log --until="3 days ago"               # Until specific date
git log --max-count=5                      # Limit number of commits
git log --name-status                      # Show file status (A/M/D)
git log --follow <file>                    # Show history of file (with renames)
```

## Remote Operations

### Viewing Remote Information

```bash
git remote                                 # List remotes
git remote -v                              # List remotes with URLs
git remote show origin                     # Detailed remote info
git remote get-url origin                  # Get URL
git ls-remote origin                       # List remote branches/tags
git ls-remote origin refs/heads/           # List remote branches only
```

### Managing Remotes

```bash
git remote add origin <url>                # Add remote
git remote add upstream <url>              # Add upstream remote
git remote remove origin                   # Remove remote
git remote rename origin upstream          # Rename remote
git remote set-url origin <new-url>        # Change URL
git remote set-url --add origin <url>      # Add alternate URL
git remote set-url --delete origin <url>   # Remove alternate URL
```

### Fetching from Remote

```bash
git fetch                                  # Fetch from all remotes
git fetch origin                           # Fetch from specific remote
git fetch origin main                      # Fetch specific branch
git fetch --all                            # Fetch all remotes
git fetch --prune                          # Remove deleted remote branches
git fetch --depth=1                        # Shallow fetch
git fetch --dry-run                        # Preview fetch without applying
git fetch -f                               # Force fetch (overwrite local)
```

### Pushing to Remote

```bash
git push                                   # Push to default remote/branch
git push origin main                       # Push specific branch
git push origin <branch>                   # Push local branch
git push -u origin <branch>                # Push and set upstream
git push --all                             # Push all branches
git push --tags                            # Push all tags
git push origin --delete <branch>          # Delete remote branch
git push origin :<branch>                  # Delete remote (alternative)
git push --force                           # Force push (dangerous!)
git push --force-with-lease                # Safer force push
git push origin HEAD                       # Push current branch
git push origin <commit>:refs/heads/<branch> # Push specific commit as branch
git push origin :refs/tags/<tag>           # Delete remote tag
git push --set-upstream origin <branch>    # Set upstream
```

### Pulling from Remote

```bash
git pull                                   # Fetch and merge
git pull origin main                       # Pull specific branch
git pull --rebase                          # Fetch and rebase
git pull --no-rebase                       # Force merge (not rebase)
git pull --ff-only                         # Only fast-forward
git pull --no-ff                           # Always create merge commit
git pull --dry-run                         # Preview pull
git pull --all                             # Pull all remotes
git pull upstream main                     # Pull from upstream
```

## Merging

### Basic Merging

```bash
git merge <branch>                         # Merge into current branch
git merge --no-ff <branch>                 # Merge with merge commit
git merge --ff-only <branch>               # Only fast-forward
git merge -m "message" <branch>            # Custom merge message
git merge --squash <branch>                # Squash commits before merge
git merge --no-edit <branch>               # Skip merge message editor
git merge --abort                          # Cancel merge in progress
```

### Merge Conflicts

```bash
git merge --abort                          # Cancel merge
git merge --continue                       # Continue after resolving conflicts
git status                                 # Show conflicted files
git diff --name-only --diff-filter=U       # Show conflicted files only
git add <resolved-file>                    # Mark as resolved
git checkout --ours <file>                 # Keep our version
git checkout --theirs <file>               # Keep their version
```

## Rebasing

### Basic Rebasing

```bash
git rebase <branch>                        # Rebase onto branch
git rebase -i HEAD~3                       # Interactive rebase last 3 commits
git rebase -i main                         # Interactive rebase on main
git rebase --continue                      # Continue after resolving conflicts
git rebase --abort                         # Cancel rebase
git rebase --skip                          # Skip current commit
git rebase -x "npm test"                   # Run command after each commit
git rebase -p <branch>                     # Preserve merge commits
```

### Advanced Rebasing

```bash
git rebase --autosquash                    # Auto-squash marked commits
git rebase -i --root                       # Rebase all commits
git rebase --signoff                       # Add Signed-off-by to commits
git rebase --committer-date-is-author-date # Keep original dates
git rebase --update-refs                   # Update dependent branches
```

## Cherry-Pick

### Applying Specific Commits

```bash
git cherry-pick <commit>                   # Apply commit
git cherry-pick <commit1> <commit2>        # Apply multiple commits
git cherry-pick <commit1>..<commit2>       # Apply range
git cherry-pick -x <commit>                # Add cherry-pick reference
git cherry-pick --continue                 # Continue after conflicts
git cherry-pick --abort                    # Cancel cherry-pick
git cherry-pick --signoff <commit>         # Add Signed-off-by
git cherry-pick <branch~2>                 # Apply from branch
```

## Undoing Changes

### Resetting

```bash
git reset                                  # Unstage all
git reset HEAD <file>                      # Unstage file
git reset --soft HEAD~1                    # Undo last commit, keep changes staged
git reset --mixed HEAD~1                   # Undo last commit, keep changes unstaged
git reset --hard HEAD~1                    # Undo last commit, discard changes
git reset --hard HEAD~3                    # Undo last 3 commits
git reset <commit>                         # Reset to commit (default: mixed)
git reset --hard <commit>                  # Reset to commit, discard changes
git reset --hard origin/main               # Reset to remote state
git reset --keep <commit>                  # Reset, preserve uncommitted work
```

### Reverting

```bash
git revert <commit>                        # Create inverse commit
git revert HEAD                            # Revert last commit
git revert <commit1> <commit2>             # Revert multiple commits
git revert --continue                      # Continue after conflicts
git revert --abort                         # Cancel revert
git revert -m 1 <merge-commit>             # Revert merge (pick parent)
git revert --no-commit <commit>            # Revert without committing
git revert -n <commit>                     # Revert without committing (short)
```

### Restoring Files

```bash
git restore <file>                         # Discard changes in file
git restore --source=HEAD <file>           # Restore from HEAD
git restore --source=<commit> <file>       # Restore from specific commit
git restore --staged <file>                # Unstage file
git restore --staged --worktree <file>     # Restore staged and unstaged
git clean -fd                              # Remove untracked files and directories
git clean -fdx                             # Remove untracked including ignored
git clean -n                               # Preview what would be removed
```

### Reflog (Recovery)

```bash
git reflog                                 # Show reference logs
git reflog show                            # Show reflog for HEAD
git reflog <branch>                        # Show reflog for branch
git reflog expire --expire=now --all       # Clear reflog
git reset --hard <reflog-entry>            # Recover using reflog
```

## Stashing

### Saving Work Temporarily

```bash
git stash                                  # Stash all changes
git stash save "description"               # Stash with message
git stash push <file>                      # Stash specific file
git stash push -m "message" <file>         # Stash file with message
git stash -u                               # Stash including untracked
git stash -a                               # Stash including ignored files
git stash --keep-index                     # Stash, keep staged
```

### Retrieving Stashed Work

```bash
git stash list                             # List all stashes
git stash show                             # Show latest stash changes
git stash show stash@0-2                   # Show specific stash
git stash show -p                          # Show stash diff
git stash pop                              # Apply and remove stash
git stash pop stash@0-2                    # Pop specific stash
git stash apply                            # Apply without removing
git stash apply stash@0-2                  # Apply specific stash
```

### Managing Stashes

```bash
git stash drop                             # Delete latest stash
git stash drop stash@0-2                   # Delete specific stash
git stash clear                            # Delete all stashes
git stash branch <branch-name>             # Create branch from stash
git stash branch <branch-name> stash@0-1   # Create branch from specific stash
```

## Tagging

### Creating Tags

```bash
git tag <tag-name>                         # Create lightweight tag
git tag -a <tag-name> -m "message"        # Create annotated tag
git tag -a v1.0.0 -m "Release 1.0.0"      # Version tag
git tag <tag-name> <commit>                # Tag specific commit
git tag -d <tag-name>                      # Delete local tag
git push origin <tag-name>                 # Push specific tag
git push origin --tags                     # Push all tags
git push origin --delete <tag-name>        # Delete remote tag
git show <tag-name>                        # Show tag info
```

### Managing Tags

```bash
git tag                                    # List tags
git tag -l "v1.*"                          # List matching pattern
git describe                               # Show nearest tag
git describe --tags                        # Show nearest tag with tags
git describe --always                      # Show short hash if no tag
git describe <commit>                      # Describe specific commit
```

## Advanced Operations

### Cherry-pick and Bisect

```bash
git bisect start                           # Start binary search for bug
git bisect bad                             # Mark current as bad
git bisect good <commit>                   # Mark commit as good
git bisect run <script>                    # Automate bisect
git bisect reset                           # End bisect
git bisect visualize                       # See bisect progress
```

### Blame and Log

```bash
git blame <file>                           # Show author of each line
git blame -L <start>,<end> <file>          # Blame specific lines
git blame -C <file>                        # Detect line movement
git blame -C -C <file>                     # Detect copy/move across files
git log <file>                             # History of file
git log -p <file>                          # History with changes
git log --follow <file>                    # History including renames
```

### Graph and Visualization

```bash
git log --graph --oneline --all            # ASCII graph
git log --graph --pretty=oneline --all     # Detailed graph
git log --graph --decorate --all           # With branch names
git log --all --graph --oneline --decorate # Complete visualization
```

### Cleaning and Maintenance

```bash
git gc                                     # Garbage collection
git fsck                                   # Check repository integrity
git verify-pack -v                         # Verify pack files
git count-objects                          # Count objects
git prune                                  # Remove unreachable objects
git prune --dry-run                        # Preview prune
```

### Submodules

```bash
git submodule add <url> <path>             # Add submodule
git submodule init                         # Initialize submodules
git submodule update                       # Update submodules
git submodule update --remote              # Update to remote version
git submodule foreach git pull             # Pull all submodules
git clone --recurse-submodules <url>       # Clone with submodules
git rm <submodule-path>                    # Remove submodule
```

### Worktrees

```bash
git worktree list                          # List worktrees
git worktree add <path> <branch>           # Create new worktree
git worktree add -b <branch> <path>        # Create branch and worktree
git worktree remove <path>                 # Delete worktree
git worktree repair                        # Repair broken worktrees
```

## GitHub CLI (gh)

### Repository Operations

```bash
gh repo create <name>                      # Create repository
gh repo clone <owner/repo>                 # Clone repository
gh repo fork <owner/repo>                  # Fork repository
gh repo view                               # View repo info
gh repo list <owner>                       # List repos
gh repo delete <owner/repo>                # Delete repository
gh repo set-default <owner/repo>           # Set default repo
gh repo view --web                         # Open in browser
```

### Pull Requests

```bash
gh pr create                               # Create PR (interactive)
gh pr create -t "Title" -b "Body"          # Create PR with title/body
gh pr list                                 # List open PRs
gh pr view <number>                        # View PR details
gh pr view <number> --web                  # Open PR in browser
gh pr checkout <number>                    # Check out PR branch
gh pr merge <number>                       # Merge PR
gh pr review <number>                      # Review PR
gh pr comment <number> -b "message"        # Comment on PR
gh pr edit <number> -t "new title"         # Edit PR
gh pr close <number>                       # Close PR
gh pr reopen <number>                      # Reopen PR
```

### Issues

```bash
gh issue create                            # Create issue
gh issue create -t "Title" -b "Body"       # Create with details
gh issue list                              # List open issues
gh issue view <number>                     # View issue
gh issue comment <number> -b "message"     # Comment on issue
gh issue close <number>                    # Close issue
gh issue reopen <number>                   # Reopen issue
gh issue lock <number>                     # Lock issue
gh issue unlock <number>                   # Unlock issue
```

### Releases

```bash
gh release create <tag>                    # Create release
gh release create <tag> -t "Title"         # Create with title
gh release list                            # List releases
gh release view <tag>                      # View release
gh release delete <tag>                    # Delete release
gh release download <tag>                  # Download release assets
```

### Other Operations

```bash
gh gist create <file>                      # Create gist
gh gist list                               # List gists
gh workflow list                           # List workflows
gh workflow run <workflow>                 # Run workflow
gh auth login                              # Authenticate
gh config get user                         # Get config
gh run list                                # List workflow runs
gh run view <run-id>                       # View run details
```

## Useful Aliases and One-Liners

### Setup Aliases

```bash
git config --global alias.st status
git config --global alias.co checkout
git config --global alias.br branch
git config --global alias.ci commit
git config --global alias.unstage 'reset HEAD --'
git config --global alias.last 'log -1 HEAD'
git config --global alias.visual 'log --graph --oneline --all'
git config --global alias.amend 'commit --amend --no-edit'
git config --global alias.sync 'pull --rebase --autostash'
git config --global alias.cleanup 'branch --merged | grep -v main | xargs git branch -d'
```

### One-Liners

```bash
# Count commits by author
git shortlog -sn

# Show lines changed by author
git log --format='%an' | sort | uniq -c

# Find who deleted a file
git log -p -- <file> | grep -B 10 "^-"

# Create backup before dangerous operation
git tag backup-$(date +%Y%m%d-%H%M%S)

# List contributors
git log --format='%an' | sort -u

# Show files changed in commit
git show --name-status <commit>

# Find branches containing commit
git branch -a --contains <commit>

# Get largest files
git ls-files -z | xargs -0 du | sort -rn | head

# Export repository as archive
git archive --format zip HEAD > repo.zip

# See what would be lost
git reflog
```

## Tips and Tricks

- Use `git add -p` for selective staging
- Always `git pull --rebase` to maintain linear history
- Use `git stash` before switching branches with uncommitted work
- Delete merged branches regularly: `git branch --merged | xargs git branch -d`
- Use `git reflog` to recover lost commits
- Sign commits with GPG: `git config --global user.signingKey <key-id>` and `git commit -S`
- Enable `push.default = current` to avoid pushing all branches

---

## Quick Reference Table

| Task | Command |
|------|---------|
| Initialize repo | `git init` |
| Clone repo | `git clone <url>` |
| Check status | `git status` |
| Stage changes | `git add <file>` |
| Commit | `git commit -m "msg"` |
| View history | `git log` |
| Create branch | `git branch <name>` |
| Switch branch | `git switch <name>` |
| Merge branch | `git merge <name>` |
| Push to remote | `git push` |
| Pull from remote | `git pull` |
| Stash changes | `git stash` |
| Undo commit | `git reset --soft HEAD~1` |
| Revert commit | `git revert <commit>` |
| Create tag | `git tag <name>` |

