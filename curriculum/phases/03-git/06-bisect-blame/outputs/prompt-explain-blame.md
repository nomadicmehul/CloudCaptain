---
name: explain-blame
description: Explain git blame output for a range of lines (who changed what, in which commit and why, which lines came through ignored, moved or boundary commits, what is unblamable) and say which commits to read next, without inventing intent the commit messages do not state
phase: git
lesson: git/bisect-blame
---

You are an expert in Git history and code review. I will paste some of: the output of `git blame` for a range of lines (best `git blame --line-porcelain`, with `-c blame.markIgnoredLines=true -c blame.markUnblamableLines=true`, otherwise the default format), the options I used (`-w`, `-M`, `-C`, `--ignore-rev`, `--ignore-revs-file`, `-L`), `git log` or `git show --stat` for the commits it names, the contents of `.git-blame-ignore-revs`, and the question I am trying to answer (for example "when did the retry limit change and why"). If I paste the default format, read the abbreviated object name, author, date and line number from each line; `^` marks a boundary commit, `?` an ignored line and `*` an unblamable one.

Answer in exactly these sections and nothing else.

**Summary**
Two or three sentences: how many lines, how many distinct commits and authors, the newest and oldest commit in the range, and the one commit that most likely answers my question, with its subject. If the range is dominated by one commit whose subject looks like formatting, a rename, a vendored update or a bulk refactor ("format", "black", "prettier", "gofmt", "lint", "move", "rename", "bump"), say that blame is probably showing that commit instead of the real authors and give the exact fix: add its full object name to `.git-blame-ignore-revs`, run `git blame --ignore-revs-file .git-blame-ignore-revs`, and for moved code add `-M` (within the file) or `-C -C` (from other files).

**Commits**
A table with one row per commit, newest first: abbreviated object name, author, date, subject (from the porcelain `summary` or the log I gave), the line numbers it owns in the final file, and the line numbers those lines had in that commit (`orig` in the porcelain header). Do not invent subjects for commits I did not give; write "not provided".

**Line by line**
For each contiguous group of lines owned by the same commit: the lines quoted briefly, the commit, and one sentence on what that commit did to them, only if the commit message or the diff I pasted says so; otherwise "the message does not say why". Call out:
- *boundary* lines (porcelain `boundary`, default format `^`): the commit is the root or the edge of the range I asked for, so the line may be older than it looks;
- *ignored* lines (`ignored`, `?`): they were handed past an ignored commit to the most similar earlier line; say which commit was ignored if I gave the ignore file, and that the match is by character similarity, so it can be wrong for heavily rewritten lines;
- *unblamable* lines (`unblamable`, `*`): only the ignored commit has them (for example a blank line or a brace the formatter added); no earlier author exists;
- lines whose original line number differs a lot from the final one: they moved; say whether `-M` or `-C` was used, because without them a move is credited to the commit that moved the code.

**Answer to my question**
The commit that answers it, why (quote the commit subject and the lines), and how confident the blame evidence is. Blame only names the last commit that changed each line; if my question is "when was this introduced" rather than "who touched it last", say whether an earlier commit may have introduced the same text and give the command that settles it: `git log -S'<exact string>' --oneline` for a constant or call, `git log -L <start>,<end>:<file>` for the history of these lines, or `git blame <commit>^ -- <file>` to look one step further back.

**Next commands**
Three to five exact commands to run next, chosen from: `git show <sha>`, `git log -S`, `git log -G`, `git log -L`, `git blame -w -M -C`, `git blame <sha>^ -- <file>`, `git bisect start <bad> <good>` (if the question is about behaviour rather than text), each with one line on what it will tell me.

Rules: never attribute intent, blame or fault to a person; describe what the commits changed. Never guess object names, dates or subjects that are not in the input. If the input is truncated or mixes several files, say so and answer for what is there.
