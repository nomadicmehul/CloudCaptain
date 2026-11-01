# Forking CloudCaptain

CloudCaptain is MIT-licensed so that bootcamps, universities, companies and study groups can teach with it. Fork it, cut it down, add your own lessons, translate it, run it on your own domain. This page explains what to change so a fork works on day one.

## The licence in one paragraph

You can use, copy, modify and redistribute everything, including commercially. Keep the `LICENSE` file and its copyright notice in your fork. Please do not present your fork as the official CloudCaptain or imply endorsement; a line such as "based on [CloudCaptain](https://github.com/nomadicmehul/CloudCaptain)" in your README and site footer is the polite way to credit it.

## What you get

- `curriculum/`: every lesson as plain files (`docs/en.md`, `code/` with tests and `smoke.sh`, `quiz.json`, `outputs/`), phase READMEs, learning paths and `manifest.json`.
- `scripts/build-curriculum.mjs`: the validator and generator that enforce [LESSON_TEMPLATE.md](./LESSON_TEMPLATE.md) and build the `/learn` sidebar; `scripts/run-lesson-tests.mjs` runs tests and smoke scripts.
- `skills/`: the `captain-*` tutor skills that teach the curriculum inside a coding agent.
- `website/`: the Docusaurus site, with the reference docs under `website/docs/`.
- `.github/workflows/`: deploy, pull-request checks and previews, the per-phase lesson smoke run, the nightly validation and the weekly version refresh.

## Make the fork yours

1. **Site identity.** In `website/docusaurus.config.ts` set `url`, `baseUrl`, `organizationName`, `projectName`, the title, the navbar and footer links, and the edit URLs (`editUrl` points at this repository). Replace the logo and social card in `website/static/img/`.
2. **Analytics.** The site loads a GoatCounter script for `cloudcaptain.goatcounter.com` (in `docusaurus.config.ts`). Point it at your own GoatCounter site or remove it. Disable or reconfigure `visitor-stats.yml`, which needs `GOATCOUNTER_SITE_CODE` and `GOATCOUNTER_API_TOKEN` secrets.
3. **Previews.** `pr-preview.yml` deploys every pull request to surge.sh and needs a `SURGE_TOKEN` secret; set one or delete the workflow.
4. **Deployment.** `deploy.yml` publishes to GitHub Pages. The site is built with `trailingSlash: false`, and a small build plugin also serves every page at `path/`, so links work either way on Pages and most static hosts.
5. **The tutor.** The skills fetch lessons from `raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/curriculum/...` when they are not run inside a clone. Replace `nomadicmehul/CloudCaptain` with your repository in `skills/*/SKILL.md` so `npx skills add <you>/<repo>` teaches your version.
6. **Freshness data.** `curriculum/versions.json` (refreshed by `versions-weekly.yml`) and `curriculum/deprecations.json` are yours to edit; the validator warns on retired technology and on lessons not verified for 180 days.

## Change the curriculum

- **Keep a subset.** Delete phase or lesson folders and their ids from the learning paths in `curriculum/manifest.json`; the validator tells you about any prerequisite that now points nowhere.
- **Add a lesson.** `cd website && npm run curriculum:scaffold -- 06-orchestration my-lesson --type Build --langs python --title "..."`, fill it in, then `npm run curriculum:validate` and `npm run curriculum:test -- <phase>/<lesson>`. The lesson-smoke workflow runs its `smoke.sh` on your pull requests, one job per phase.
- **Add a phase.** Phases are declared in `curriculum/manifest.json` (dir, slug, title, tagline); create the folder with a README and the site picks it up.
- **Translate.** Lessons use `docs/en.md`; README translations live in `i18n/`.

## Stay in sync with upstream

```bash
git remote add upstream https://github.com/nomadicmehul/CloudCaptain.git
git fetch upstream
git merge upstream/main        # new lessons, fixes and version bumps
```

Lessons are self-contained folders, so merges rarely conflict outside `manifest.json`, `CHANGELOG.md`, `ROADMAP.md` and the phase READMEs.

## Tell us

If you teach with CloudCaptain, open an issue or a discussion and say where: it helps decide what to build next, and fixes that start in forks are always welcome back upstream.
