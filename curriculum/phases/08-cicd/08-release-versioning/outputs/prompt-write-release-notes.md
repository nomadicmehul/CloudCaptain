---
name: write-release-notes
description: Turn the commits since the last tag into the next SemVer version with its reason, a Keep a Changelog section, consumer-facing upgrade notes and a list of commits that could not be classified
phase: cicd
lesson: cicd/release-versioning
---

You are a release engineer. I will paste the previous version (for example `v0.2.0`), optionally a pre-release channel, the bump settings if the project has non-default ones, and the commits since the last tag, either as `git log --format='%h %s%n%b%n---' vX..HEAD` or as git-cliff's `--context` JSON. Treat the commits as Conventional Commits 1.0.0.

Answer in exactly these sections and nothing else.

**Next version**
One line: the next tag, then the rule that decided it in one sentence (for example "a breaking change in 0.y.z bumps the minor version (the 0.x rule)"). Rules: the impact of the release is the worst impact of any commit that appears in the changelog; a `!` after the type or a `BREAKING CHANGE:` or `BREAKING-CHANGE:` footer (upper case only) is breaking; `feat` is a feature; `fix`, `perf`, `refactor` and `revert` are patches; `build`, `chore`, `ci`, `docs`, `style` and `test` are hidden unless breaking. At 1.0 and later: breaking is major, feature is minor, otherwise patch. In 0.y.z with y above 0: breaking is minor and feature is patch, unless I say the project leaves 0.x on a breaking change or bumps the minor for features. In 0.0.z: everything is a patch unless I say otherwise. With a channel, give `X.Y.Z-CHANNEL.N` where N is one more than the highest existing pre-release on that channel for the same X.Y.Z that I listed. If nothing would appear in the changelog, say "No release" and stop after the last section.

**Changelog**
A Keep a Changelog section: `## [X.Y.Z] - YYYY-MM-DD` with today's date if I did not give one, then only the non-empty subsections in this order: Added, Changed, Deprecated, Removed, Fixed, Security. Placement: scope `security` goes to Security; a description starting with "deprecate" to Deprecated; one starting with "remove" or "drop" to Removed; then `feat` to Added, `fix` to Fixed, `perf`, `refactor` and `revert` to Changed. One line per commit: `- **BREAKING** ` when breaking, `**scope:** ` when there is a scope, then the description, rewritten only to fix grammar, never to change meaning.

**Upgrade notes**
One bullet per breaking change, written for the person upgrading: what they must change in their code or configuration, with the old and new names in backticks. Use the `BREAKING CHANGE:` footer text when there is one. If a commit is breaking but says nothing about how to migrate, write "Migration not described" and name the commit hash.

**Could not classify**
A table with columns: commit hash, subject, why (not conventional, hidden type, or suspicious), and what you assumed. Flag as suspicious any non-breaking commit whose body mentions removing or renaming a public field, flag, endpoint, option or default; say which version it would imply if it were breaking.

Rules: do not invent commits, scopes, issue numbers or dates. Do not merge two commits into one entry. If my input mixes several packages or tag prefixes, ask which one to release instead of answering. Under 400 words excluding the changelog lines.
