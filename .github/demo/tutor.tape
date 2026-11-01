# Records the README's tutor demo: the CloudCaptain skills installed into a
# coding agent, then /captain-start running for real.
#
#   brew install vhs            # https://github.com/charmbracelet/vhs
#   vhs .github/demo/tutor.tape # writes website/static/img/tutor-demo.gif
#
# Run it in an empty directory you are happy to leave a LEARNING.md in, with
# Claude Code installed and signed in. Adjust the Sleep values to how long
# your agent takes; keep the result under about 20 seconds of playback.

Output website/static/img/tutor-demo.gif

Set Shell "bash"
Set FontSize 18
Set Width 1280
Set Height 760
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms
Set PlaybackSpeed 1.5

Hide
Type "cd $(mktemp -d) && clear"
Enter
Show

Type "npx skills add nomadicmehul/CloudCaptain"
Sleep 400ms
Enter
Sleep 12s

Type "claude"
Enter
Sleep 4s

Type "/captain-start"
Sleep 400ms
Enter
Sleep 25s

Type "2"
Enter
Sleep 20s
