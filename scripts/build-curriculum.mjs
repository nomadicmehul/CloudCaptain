#!/usr/bin/env node
/**
 * CloudCaptain curriculum builder.
 *
 *   node scripts/build-curriculum.mjs            validate + write website/src/data/generated/curriculum.json
 *   node scripts/build-curriculum.mjs --check    validate only (CI)
 *   node scripts/build-curriculum.mjs --json     print stats as JSON
 *   node scripts/build-curriculum.mjs --roadmap  refresh the phase table in ROADMAP.md
 *   node scripts/build-curriculum.mjs --deprecations  scan curriculum + website/docs for retired tech without a note
 *   node scripts/build-curriculum.mjs --scaffold <phase-dir> <lesson-slug> [--type Build|Learn] [--langs python,bash] [--title "..."] [--minutes 60]
 *
 * Zero dependencies. Node >= 20.
 */
import fs from 'node:fs';
import path from 'node:path';
import url from 'node:url';
import {discover, loadManifest, exists, readJson, LESSON_DIR_RE, ENTRY_FILES} from './lib/discover.mjs';
import {validateLesson, validateGlobal, formatReport, scanDeprecations} from './lib/validate.mjs';
import {buildModel, writeModel, updateRoadmap} from './lib/emit.mjs';

const here = path.dirname(url.fileURLToPath(import.meta.url));
export const repoRoot = path.resolve(here, '..');
export const curriculumRoot = path.join(repoRoot, 'curriculum');
export const websiteRoot = path.join(repoRoot, 'website');
export const outFile = path.join(websiteRoot, 'src', 'data', 'generated', 'curriculum.json');

export function run({curriculum = curriculumRoot, websiteDocsRoot = path.join(websiteRoot, 'docs'), out = outFile, check = false, quiet = false} = {}) {
  const manifest = loadManifest(curriculum);
  const phases = discover(curriculum, manifest);
  const deprecations = exists(path.join(curriculum, 'deprecations.json')) ? readJson(path.join(curriculum, 'deprecations.json')) : [];
  const versions = exists(path.join(curriculum, 'versions.json')) ? readJson(path.join(curriculum, 'versions.json')) : null;
  const ctx = {curriculumRoot: curriculum, websiteDocsRoot, deprecations};
  for (const phase of phases) for (const lesson of phase.lessons) validateLesson(lesson, ctx);
  const global = validateGlobal(phases, manifest, ctx);
  const report = formatReport(phases, global);
  if (!quiet && report.text.trim()) console.log(report.text.trim());
  if (report.errorCount > 0) {
    if (!quiet) console.error(`\ncurriculum: ${report.errorCount} error(s), ${report.warningCount} warning(s)`);
    return {ok: false, report, model: null, phases, manifest};
  }
  const model = buildModel(phases, manifest);
  model.versions = versions;
  model.deprecations = deprecations;
  if (!check) writeModel(model, out);
  if (!quiet) {
    const s = model.stats;
    console.log(`curriculum: ${s.lessons} lesson(s) in ${s.phasesLive}/${s.phasesTotal} phases, ${s.hours} h, ${s.tests} tests, ${report.warningCount} warning(s)${check ? '' : ` → ${path.relative(repoRoot, out)}`}`);
  }
  return {ok: true, report, model, phases, manifest};
}

function parseArgs(argv) {
  const args = {flags: new Set(), positional: [], options: {}};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--type' || a === '--langs' || a === '--title' || a === '--minutes') args.options[a.slice(2)] = argv[++i];
    else if (a.startsWith('--')) args.flags.add(a.slice(2));
    else args.positional.push(a);
  }
  return args;
}

function slugToTitle(slug) {
  return slug.split('-').map((w) => w[0].toUpperCase() + w.slice(1)).join(' ');
}

export function scaffold({phaseDir, lessonSlug, type = 'Build', langs = ['python'], title, minutes = 60, curriculum = curriculumRoot}) {
  const manifest = loadManifest(curriculum);
  const phase = manifest.phases.find((p) => p.dir === phaseDir);
  if (!phase) throw new Error(`unknown phase dir "${phaseDir}" (see curriculum/manifest.json)`);
  if (!/^[a-z0-9]+(-[a-z0-9]+)*$/.test(lessonSlug)) throw new Error('lesson slug must be lowercase words separated by hyphens');
  if (!['Build', 'Learn'].includes(type)) throw new Error('--type must be Build or Learn');
  for (const l of langs) if (!ENTRY_FILES[l]) throw new Error(`unknown language "${l}"`);

  const phasePath = path.join(curriculum, 'phases', phaseDir);
  fs.mkdirSync(phasePath, {recursive: true});
  const existing = fs.readdirSync(phasePath).filter((n) => LESSON_DIR_RE.test(n));
  if (existing.some((n) => n.endsWith(`-${lessonSlug}`))) throw new Error(`lesson "${lessonSlug}" already exists in ${phaseDir}`);
  const number = String(existing.length + 1).padStart(2, '0');
  const dir = path.join(phasePath, `${number}-${lessonSlug}`);
  const id = `${phase.slug}/${lessonSlug}`;
  title = title || slugToTitle(lessonSlug);
  const motto = 'TODO: one line that states the core idea.';

  fs.mkdirSync(path.join(dir, 'docs'), {recursive: true});
  fs.mkdirSync(path.join(dir, 'code', 'tests'), {recursive: true});
  fs.mkdirSync(path.join(dir, 'outputs'), {recursive: true});

  const tests = {};
  for (const l of langs) {
    if (l === 'python') tests.python = 'python3 -m unittest discover -s code/tests -v';
    if (l === 'bash') tests.bash = 'bats code/tests';
    if (l === 'go') tests.go = 'cd code && go test ./...';
    if (l === 'typescript') tests.typescript = 'cd code && npm test';
  }

  fs.writeFileSync(
    path.join(dir, 'lesson.json'),
    JSON.stringify(
      {
        $schema: '../../../schema/lesson.schema.json',
        id,
        title,
        motto,
        type,
        status: 'draft',
        minutes,
        langs,
        prereqs: [],
        tools: [],
        reference: [],
        outputs: [`prompt-${lessonSlug}.md`],
        tests,
      },
      null,
      2,
    ) + '\n',
  );

  const langLabel = langs.map((l) => l[0].toUpperCase() + l.slice(1)).join(', ');
  fs.writeFileSync(
    path.join(dir, 'docs', 'en.md'),
    `# ${title}

*${motto}*

## Metadata

| Field | Value |
|---|---|
| Type | ${type} |
| Languages | ${langLabel} |
| Prerequisites | None |
| Time | ${minutes} minutes |
| Tools | TODO |

<Quiz stage="pre" />

## The Problem

TODO: two or three paragraphs. A concrete scenario of what goes wrong without this concept.

## The Concept

TODO: the intuition, then a diagram. No code here.

\`\`\`mermaid
flowchart LR
  A[Input] --> B[Concept] --> C[Outcome]
\`\`\`

## Build It

TODO: the from-scratch implementation, step by step, standard library only.

\`\`\`bash
python3 code/main.py --help
\`\`\`

<RunTests />

## Use It

TODO: the same job with the real tool. Compare it with what you built.

<Quiz stage="check" />

## Ship It

TODO: what the learner takes away, saved in outputs/.

<ShipIt />

## Exercises

### Easy

TODO

### Medium

TODO

### Hard

TODO

<Quiz stage="post" />

## Key Terms

| Term | Common misconception | Precise definition |
|---|---|---|
| TODO | TODO | TODO |
| TODO | TODO | TODO |
| TODO | TODO | TODO |
| TODO | TODO | TODO |
| TODO | TODO | TODO |

## Further Reading

- [TODO Reference page](https://cloudcaptain.io/docs/tools/docker/fundamentals) — why to read it.
`,
  );

  for (const l of langs) {
    const entry = path.join(dir, 'code', ENTRY_FILES[l]);
    if (l === 'python') fs.writeFileSync(entry, `from __future__ import annotations\n\nimport sys\n\n\ndef main(argv: list[str] | None = None) -> int:\n    argv = sys.argv[1:] if argv is None else argv\n    print("TODO", argv)\n    return 0\n\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n`);
    else if (l === 'bash') fs.writeFileSync(entry, `#!/usr/bin/env bash\nset -euo pipefail\n\necho "TODO $*"\n`);
    else fs.writeFileSync(entry, '');
  }
  if (langs.includes('python')) {
    fs.writeFileSync(
      path.join(dir, 'code', 'tests', 'test_main.py'),
      `import os\nimport sys\nimport unittest\n\nsys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))\n\nimport main  # noqa: E402\n\n\nclass MainTests(unittest.TestCase):\n    def test_main_returns_zero(self):\n        self.assertEqual(main.main([]), 0)\n\n    def test_todo_1(self):\n        self.fail("write me")\n\n    def test_todo_2(self):\n        self.fail("write me")\n\n    def test_todo_3(self):\n        self.fail("write me")\n\n    def test_todo_4(self):\n        self.fail("write me")\n\n\nif __name__ == "__main__":\n    unittest.main()\n`,
    );
  } else {
    fs.writeFileSync(path.join(dir, 'code', 'tests', '.gitkeep'), '');
  }

  const q = (stage, n) => ({id: `${stage}-${n}`, stage, prompt: 'TODO: question text?', choices: ['TODO A', 'TODO B', 'TODO C'], answer: 0, explanation: 'TODO: why the answer is right.'});
  fs.writeFileSync(
    path.join(dir, 'quiz.json'),
    JSON.stringify({$schema: '../../schema/quiz.schema.json', lesson: id, questions: [q('pre', 1), q('check', 1), q('check', 2), q('check', 3), q('post', 1), q('post', 2)]}, null, 2) + '\n',
  );
  fs.writeFileSync(
    path.join(dir, 'outputs', `prompt-${lessonSlug}.md`),
    `---\nname: ${lessonSlug}\ndescription: TODO one-line purpose\nphase: ${phase.slug}\nlesson: ${id}\n---\n\nTODO: the reusable prompt this lesson produces.\n`,
  );
  return dir;
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === url.fileURLToPath(import.meta.url);
if (isMain) {
  const args = parseArgs(process.argv.slice(2));
  try {
    if (args.flags.has('scaffold')) {
      const [phaseDir, lessonSlug] = args.positional;
      if (!phaseDir || !lessonSlug) throw new Error('usage: --scaffold <phase-dir> <lesson-slug> [--type Build|Learn] [--langs python,bash] [--title "..."] [--minutes 60]');
      const dir = scaffold({
        phaseDir,
        lessonSlug,
        type: args.options.type || 'Build',
        langs: (args.options.langs || 'python').split(',').map((s) => s.trim()).filter(Boolean),
        title: args.options.title,
        minutes: args.options.minutes ? Number(args.options.minutes) : 60,
      });
      console.log(`scaffolded ${path.relative(repoRoot, dir)}\nnext: fill docs/en.md, code/, quiz.json and outputs/, then run: npm run curriculum:validate`);
      process.exit(0);
    }
    if (args.flags.has('deprecations')) {
      const deprecations = readJson(path.join(curriculumRoot, 'deprecations.json'));
      const hits = scanDeprecations([path.join(curriculumRoot, 'phases'), path.join(websiteRoot, 'docs')], deprecations, repoRoot);
      if (hits.length === 0) console.log('deprecations: no unannotated mentions');
      else {
        console.log(`deprecations: ${hits.length} mention(s) without a nearby status note`);
        for (const h of hits) console.log(`  ${h.file}:${h.line}  ${h.term}  → ${h.replacement}`);
      }
      process.exit(0);
    }
    const result = run({check: args.flags.has('check') || args.flags.has('json') || args.flags.has('roadmap'), quiet: args.flags.has('json')});
    if (!result.ok) process.exit(1);
    if (args.flags.has('json')) console.log(JSON.stringify(result.model.stats, null, 2));
    if (args.flags.has('roadmap')) {
      const changed = updateRoadmap(path.join(repoRoot, 'ROADMAP.md'), result.model);
      console.log(changed ? 'ROADMAP.md updated' : 'ROADMAP.md already current');
    }
    if (!args.flags.has('check') && !args.flags.has('json') && !args.flags.has('roadmap') && !exists(outFile)) process.exit(1);
  } catch (e) {
    console.error(`curriculum: ${e.message}`);
    process.exit(1);
  }
}
