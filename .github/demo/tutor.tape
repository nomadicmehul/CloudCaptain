# Records the README's tutor demo: the CloudCaptain skills installed into a
# project, then /captain-start running for real in Claude Code.
#
#   brew install vhs              # https://github.com/charmbracelet/vhs
#   mkdir ~/learn-cloud && cd ~/learn-cloud && git init && claude   # trust the folder once, then /exit
#
# The session must not stop at a permission prompt: record in auto mode, or
# give ~/learn-cloud a .claude/settings.json that allows
# WebFetch(domain:raw.githubusercontent.com), WebFetch(domain:api.github.com)
# and Write(LEARNING.md). Run vhs from a clean environment (env -i HOME=...
# PATH=...) when you start it from inside another Claude Code session.
#   cd <this repo> && vhs .github/demo/tutor.tape   # writes website/static/img/tutor-demo.gif
#
# The recording hides the time the model spends thinking. Each Wait matches
# the line just typed, with its ❯ marker so the question's own words never
# match, followed by Claude Code's "done" line; the GIF cuts from the
# question straight to the finished reply. Wait+Screen only sees the first
# screenful of output, which is why the installer step waits for the shell
# prompt instead, and why the last step also accepts the quiz's "D)" choice
# (the reply can scroll the typed line away). A slash cannot appear inside a
# VHS regex, hence ".captain".

Output website/static/img/tutor-demo.gif

Require claude
Require npx

Set Shell "bash"
Set FontSize 18
Set Width 1280
Set Height 760
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms

Hide
Type "cd ~/learn-cloud && rm -rf .claude/skills skills-lock.json LEARNING.md && clear"
Enter
Show

Type "npx skills add nomadicmehul/CloudCaptain -a claude-code -y"
Sleep 300ms
Enter
Hide
Wait@120s />\s*$/
Show
Sleep 2500ms

Hide
Type "clear"
Enter
Show
Type "claude"
Enter
Wait+Screen@30s /auto mode|shortcuts/
Sleep 1500ms

Type "/captain-start"
Sleep 600ms
Enter
Hide
Wait+Screen@150s /(?s)❯ .captain-start.*done \d/
Show
Sleep 3s

Type "Run Kubernetes and AI workloads in production"
Sleep 400ms
Enter
Hide
Wait+Screen@150s /(?s)❯ Run Kubernetes and AI workloads in production.*done \d/
Show
Sleep 3s

Type "Practitioner: I ship with Docker and CI"
Sleep 400ms
Enter
Hide
Wait+Screen@150s /(?s)❯ Practitioner: I ship with Docker and CI.*done \d/
Show
Sleep 3s

Type "5 hours a week"
Sleep 400ms
Enter
Hide
Wait+Screen@240s /(?s)(❯ 5 hours a week|\bD[).] ).*done \d/
Show
Sleep 4s

Hide
Ctrl+C
Sleep 300ms
Ctrl+C
