# Learn Cloud, DevOps & AIOps from scratch

**Build the tool. Use the tool. Ship an artifact.**

Every lesson in this curriculum follows the same arc. You start with a real production problem, build the mechanism yourself from the standard library (a container runtime, a scheduler, a state file with plan and apply, a metrics store, an incident-triage agent), then do the same job with the tool the industry actually runs (Docker, Kubernetes, Terraform, Prometheus, an MCP server) so you can map one onto the other. You leave with a reusable prompt, skill or agent definition in your own toolbox, a quiz score, and tests that prove your build works.

Lessons live in this folder as plain files you can read on GitHub, on [cloudcaptain.io/learn](https://cloudcaptain.io/learn), or inside your coding agent. The long-form guides, cheat sheets, interview questions and exam prep that CloudCaptain is known for are the **Reference layer** at [cloudcaptain.io/docs](https://cloudcaptain.io/docs); lessons link into it rather than repeating it.

<CurriculumHome />

## Learn in your terminal

The tutor skills install into Claude Code, Cursor, Codex and any agent that reads `SKILL.md` files:

```bash
npx skills add nomadicmehul/CloudCaptain
```

Then `/captain-start` for a placement quiz and a personal plan, `/captain-learn` for one lesson per session, `/captain-guide` to ask where a topic lives, and `/captain-check <phase>` for a Socratic review. Progress is written to `LEARNING.md` in your project.

## Run it locally

```bash
git clone https://github.com/nomadicmehul/CloudCaptain.git
cd CloudCaptain/curriculum/phases/05-containers/01-process-isolation
python3 -m unittest discover -s code/tests -v
```

Every lesson folder has `docs/en.md`, `code/` with tests, `quiz.json` and `outputs/`. The contract is in [LESSON_TEMPLATE.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/LESSON_TEMPLATE.md); the validator in `scripts/build-curriculum.mjs` enforces it, so contributing a lesson is a matter of filling in the template until the validator is happy.
