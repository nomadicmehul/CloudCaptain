---
name: captain-check
description: Socratic review of one CloudCaptain phase (e.g. "captain-check containers"). Asks the phase's post-lesson questions open-ended first, scores readiness, lists weak lessons, and appends the result to LEARNING.md. Use after finishing a phase or before an interview or exam.
metadata:
  project: cloudcaptain
  version: 1.0.0
---

# captain-check

Argument: a phase slug from `curriculum/manifest.json` (`containers`, `orchestration`, `iac`, ...). If none is given, ask which phase, listing the ones with published lessons.

## Build the question set

For every published lesson in the phase (files as described in `captain-start`), take the two `post` questions from `quiz.json`. If the phase has fewer than three lessons, add the `check` questions too. Shuffle. Cap at twelve.

## Run the review

One question per turn, Socratic:

1. Ask the question **without** the choices. Let the learner answer in their own words.
2. If the answer is right, say so in one line and move on.
3. If it is incomplete or wrong, ask one follow-up that points at the gap (use the question's `explanation` and the lesson's Key Terms table from `docs/en.md`). Then show the choices and let them pick.
4. Record right / right-after-hint / wrong per question.

Do not lecture between questions. Keep the pace brisk.

## Score and report

- Readiness = right ÷ total, with right-after-hint counting half. Report it as a percentage and a word: under 60% "revisit", 60–85% "solid", above 85% "ready".
- List weak lessons: any lesson with a wrong or hinted answer, with its id and URL (`https://cloudcaptain.io/learn/<phase>/<lesson>`), and the Key Term to reread.
- If the phase maps to a certification in `manifest.certifications`, name it and link its `referenceDoc`.

Append to the `## Assessments` section of `LEARNING.md` (create the section if missing):

```markdown
- 2026-12-01 containers: 83% (solid). Weak: containers/process-isolation (mount propagation). Next: reread Key Terms, then captain-check again in a week.
```

Do not change any Progress rows. End with the one next action you recommend.
