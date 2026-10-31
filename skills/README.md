# CloudCaptain tutor skills

Learn the from-scratch curriculum inside your coding agent. The skills work in Claude Code, Cursor, Codex and any agent that reads `SKILL.md` files.

```bash
npx skills add nomadicmehul/CloudCaptain
```

Pick the skills and the agents when prompted (`-g` installs them for every project). Then:

| Skill | Use it for |
|---|---|
| `/captain-start` | Placement quiz, pick a learning path, write `LEARNING.md` with your plan |
| `/captain-learn` | One lesson per session: build it, use it, ship it, quiz, progress saved |
| `/captain-guide` | "Where do I learn X?", "What do I need for role Y?", hours per phase, which certification |
| `/captain-check <phase>` | Socratic review of a phase, readiness score, weak spots |

The skills read `curriculum/manifest.json` and the lesson folders directly when you are inside a clone of this repository, and fetch the same files from GitHub when you are not. Progress lives in `LEARNING.md` in your project; nothing is sent anywhere.

Run a skill without installing: `npx skills use nomadicmehul/CloudCaptain@captain-guide --agent claude-code`.
