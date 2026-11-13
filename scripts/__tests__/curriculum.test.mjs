import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import url from 'node:url';
import {spawnSync} from 'node:child_process';
import {run, scaffold} from '../build-curriculum.mjs';
import {parseMarkdown} from '../lib/markdown.mjs';
import {discover, loadManifest, countTests} from '../lib/discover.mjs';
import {validateLesson, validateGlobal} from '../lib/validate.mjs';
import {buildModel, tutorIndex} from '../lib/emit.mjs';

const here = path.dirname(url.fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..');
const realCurriculum = path.join(repoRoot, 'curriculum');
const websiteDocsRoot = path.join(repoRoot, 'website', 'docs');
const SAMPLE_PHASE = '05-containers';

function tmpCurriculum() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-curriculum-'));
  fs.cpSync(path.join(realCurriculum, 'manifest.json'), path.join(dir, 'manifest.json'));
  fs.cpSync(path.join(realCurriculum, 'README.md'), path.join(dir, 'README.md'));
  fs.cpSync(path.join(realCurriculum, 'paths'), path.join(dir, 'paths'), {recursive: true});
  fs.cpSync(path.join(realCurriculum, 'phases'), path.join(dir, 'phases'), {recursive: true});
  return dir;
}

function validateAll(curriculum) {
  const manifest = loadManifest(curriculum);
  const phases = discover(curriculum, manifest);
  const ctx = {curriculumRoot: curriculum, websiteDocsRoot};
  for (const p of phases) for (const l of p.lessons) validateLesson(l, ctx);
  const global = validateGlobal(phases, manifest, ctx);
  const lessonErrors = phases.flatMap((p) => p.lessons.flatMap((l) => l.errors));
  return {phases, manifest, global, errors: [...lessonErrors, ...global.errors]};
}

function firstLessonDir(curriculum) {
  const phaseDir = path.join(curriculum, 'phases', SAMPLE_PHASE);
  const name = fs.readdirSync(phaseDir).find((n) => /^\d{2}-/.test(n));
  return path.join(phaseDir, name);
}

test('the real curriculum validates without errors', () => {
  const result = run({check: true, quiet: true});
  assert.equal(result.ok, true, JSON.stringify(result.report, null, 2));
  assert.ok(result.model.stats.lessons >= 1);
  assert.ok(result.model.sidebar.length >= 2);
});

test('markdown parser finds title, motto, sections, fences and quiz tags', () => {
  const md = `# Title\n\n*The motto.*\n\n## Metadata\n\n| Field | Value |\n|---|---|\n| Type | Build |\n\n<Quiz stage="pre" />\n\n## The Concept\n\n\`\`\`mermaid\nflowchart LR\n A-->B\n\`\`\`\n\n## Build It\n\n\`\`\`python\nprint("## not a heading")\n\`\`\`\n`;
  const p = parseMarkdown(md);
  assert.equal(p.h1.text, 'Title');
  assert.equal(p.motto, 'The motto.');
  assert.deepEqual(p.sections.map((s) => s.title), ['Metadata', 'The Concept', 'Build It']);
  assert.equal(p.sections[1].fences[0].lang, 'mermaid');
  assert.deepEqual(p.quizTags, ['pre']);
  assert.equal(p.sections[0].tables[0].rows[0][1], 'Build');
});

test('a missing section is reported as an ordering error', () => {
  const dir = tmpCurriculum();
  const lesson = firstLessonDir(dir);
  const md = path.join(lesson, 'docs', 'en.md');
  fs.writeFileSync(md, fs.readFileSync(md, 'utf8').replace('\n## Ship It\n', '\n## Shipping\n'));
  const {errors} = validateAll(dir);
  assert.ok(errors.some((e) => e.includes('H2 sections must be exactly')), errors.join('\n'));
});

test('wrong quiz stage counts are rejected', () => {
  const dir = tmpCurriculum();
  const lesson = firstLessonDir(dir);
  const quizFile = path.join(lesson, 'quiz.json');
  const quiz = JSON.parse(fs.readFileSync(quizFile, 'utf8'));
  quiz.questions[0].stage = 'post';
  fs.writeFileSync(quizFile, JSON.stringify(quiz));
  const {errors} = validateAll(dir);
  assert.ok(errors.some((e) => e.includes('expected 1 "pre"')), errors.join('\n'));
});

test('too few tests and a dangling prerequisite are rejected', () => {
  const dir = tmpCurriculum();
  const lesson = firstLessonDir(dir);
  fs.rmSync(path.join(lesson, 'code', 'tests'), {recursive: true});
  const jsonFile = path.join(lesson, 'lesson.json');
  const json = JSON.parse(fs.readFileSync(jsonFile, 'utf8'));
  json.prereqs = ['linux/does-not-exist'];
  fs.writeFileSync(jsonFile, JSON.stringify(json));
  const {errors} = validateAll(dir);
  assert.ok(errors.some((e) => e.includes('code/tests/ is missing')));
  assert.ok(errors.some((e) => e.includes('prerequisite linux/does-not-exist does not exist')));
});

test('prerequisite cycles are detected', () => {
  const dir = tmpCurriculum();
  const lesson = firstLessonDir(dir);
  const jsonFile = path.join(lesson, 'lesson.json');
  const json = JSON.parse(fs.readFileSync(jsonFile, 'utf8'));
  json.prereqs = [json.id];
  fs.writeFileSync(jsonFile, JSON.stringify(json));
  const {errors} = validateAll(dir);
  assert.ok(errors.some((e) => e.includes('prerequisite cycle')), errors.join('\n'));
});

test('a path must list every prerequisite, before the lesson that needs it', () => {
  const dir = tmpCurriculum();
  const manifestFile = path.join(dir, 'manifest.json');
  const manifest = JSON.parse(fs.readFileSync(manifestFile, 'utf8'));
  const devops = manifest.paths.find((p) => p.id === 'devops');
  const at = devops.lessons.indexOf('containers/image-layers');
  assert.ok(at > 0);
  devops.lessons.splice(at, 1);
  fs.writeFileSync(manifestFile, JSON.stringify(manifest));
  let {errors} = validateAll(dir);
  assert.ok(errors.some((e) => e.includes('path "devops" lists containers/registry-pull but not its prerequisite containers/image-layers')), errors.join('\n'));

  devops.lessons.push('containers/image-layers');
  fs.writeFileSync(manifestFile, JSON.stringify(manifest));
  ({errors} = validateAll(dir));
  assert.ok(errors.some((e) => e.includes('path "devops" lists containers/registry-pull before its prerequisite containers/image-layers')), errors.join('\n'));
});

test('the tutor index lists every published lesson with its folder and raw file URLs', () => {
  const result = run({check: true, quiet: true});
  const index = tutorIndex(result.model);
  assert.equal(index.lessons.length, result.model.stats.lessons);
  const lesson = index.lessons.find((l) => l.id === 'containers/process-isolation');
  assert.ok(lesson, 'containers/process-isolation is published');
  assert.match(lesson.dir, /^phases\/05-containers\/\d{2}-process-isolation$/);
  assert.equal(lesson.files.quiz, `${index.raw}${lesson.dir}/quiz.json`);
  assert.ok(index.raw.startsWith('https://raw.githubusercontent.com/'));
  const ids = new Set(index.lessons.map((l) => l.id));
  for (const p of index.paths) for (const id of p.lessons) assert.ok(ids.has(id), `${p.id} lists ${id}`);
  for (const l of index.lessons) for (const pre of l.prereqs) assert.ok(ids.has(pre), `${l.id} needs ${pre}`);
});

test('draft lessons are excluded from the model and sidebar', () => {
  const dir = tmpCurriculum();
  const phaseDir = path.join(dir, 'phases', SAMPLE_PHASE);
  const lessonDirs = fs.readdirSync(phaseDir).filter((n) => /^\d{2}-/.test(n));
  for (const name of lessonDirs) {
    const jsonFile = path.join(phaseDir, name, 'lesson.json');
    const json = JSON.parse(fs.readFileSync(jsonFile, 'utf8'));
    json.status = 'draft';
    fs.writeFileSync(jsonFile, JSON.stringify(json));
  }
  const before = run({check: true, quiet: true}).model.stats.lessons;
  const {phases, manifest} = validateAll(dir);
  const model = buildModel(phases, manifest);
  assert.equal(model.stats.lessons, before - lessonDirs.length);
  assert.ok(!model.sidebar.some((item) => item.type === 'category' && item.label.includes('Containers')));
});

test('scaffold creates a lesson folder that the validator can read', () => {
  const dir = tmpCurriculum();
  const created = scaffold({phaseDir: SAMPLE_PHASE, lessonSlug: 'registries', type: 'Learn', langs: ['python'], title: 'Registries and Content Addressing', curriculum: dir});
  assert.ok(fs.existsSync(path.join(created, 'docs', 'en.md')));
  assert.ok(fs.existsSync(path.join(created, 'code', 'tests', 'test_main.py')));
  const json = JSON.parse(fs.readFileSync(path.join(created, 'lesson.json'), 'utf8'));
  assert.equal(json.id, 'containers/registries');
  assert.equal(json.status, 'draft');
  const {errors} = validateAll(dir);
  assert.ok(!errors.some((e) => e.includes('H2 sections must be exactly')), 'scaffold section order is valid');
});

test('countTests recognises python, bats, js and go test styles', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-tests-'));
  fs.writeFileSync(path.join(dir, 'test_a.py'), 'def test_one():\n    pass\n\ndef test_two():\n    pass\n');
  fs.writeFileSync(path.join(dir, 'a.bats'), '@test "x" {\n  true\n}\n');
  fs.writeFileSync(path.join(dir, 'a.test.js'), "test('x', () => {});\nit('y', () => {});\n");
  fs.writeFileSync(path.join(dir, 'a_test.go'), 'func TestX(t *testing.T) {}\n');
  assert.equal(countTests(fs.readdirSync(dir).map((n) => path.join(dir, n))), 6);
});

function scaffoldCapstoneIn(dir) {
  return scaffold({phaseDir: '16-capstones', lessonSlug: 'build-a-paas', type: 'Capstone', langs: ['python', 'bash'], title: 'Build a Tiny PaaS', curriculum: dir});
}

function capstoneErrors(dir) {
  const {phases} = validateAll(dir);
  return phases.find((p) => p.dir === '16-capstones').lessons[0].errors;
}

test('scaffold --type Capstone creates a capstone the validator accepts', () => {
  const dir = tmpCurriculum();
  const created = scaffoldCapstoneIn(dir);
  for (const rel of ['starter/README.md', 'solution/README.md', 'grader/grade.sh', 'grader/grade.py', 'grader/checks.py', 'grader/smoke.sh', 'grader/workflow.yml', 'grader/tests/test_grade.py']) {
    assert.ok(fs.existsSync(path.join(created, rel)), rel);
  }
  assert.ok(!fs.existsSync(path.join(created, 'code')), 'capstones have no code/ folder');
  const json = JSON.parse(fs.readFileSync(path.join(created, 'lesson.json'), 'utf8'));
  assert.equal(json.type, 'Capstone');
  assert.equal(json.minutes, 600);
  assert.match(fs.readFileSync(path.join(created, 'grader', 'workflow.yml'), 'utf8'), /16-capstones\/01-build-a-paas\/grader\/grade\.sh project/);
  assert.deepEqual(capstoneErrors(dir), []);
  const harness = spawnSync('python3', ['-m', 'unittest', 'discover', '-s', 'grader/tests'], {cwd: created, encoding: 'utf8', env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}});
  assert.equal(harness.status, 0, harness.stderr);
  const graded = spawnSync('bash', ['grader/grade.sh', 'starter', '--json'], {cwd: created, encoding: 'utf8'});
  assert.equal(graded.status, 1, 'the scaffold starter must not pass');
  assert.equal(JSON.parse(graded.stdout).score, 0);
});

test('capstone rules: milestones need checks and headings, a post-only quiz, hours and a reachable pass score', () => {
  const dir = tmpCurriculum();
  const created = scaffoldCapstoneIn(dir);
  const jsonFile = path.join(created, 'lesson.json');
  const json = JSON.parse(fs.readFileSync(jsonFile, 'utf8'));
  json.milestones.push({id: 'fourth-milestone', title: 'Fourth milestone', points: 2});
  json.pass_score = 99;
  json.minutes = 90;
  fs.writeFileSync(jsonFile, JSON.stringify(json));
  const quizFile = path.join(created, 'quiz.json');
  const quiz = JSON.parse(fs.readFileSync(quizFile, 'utf8'));
  quiz.questions[0].stage = 'pre';
  fs.writeFileSync(quizFile, JSON.stringify(quiz));
  const errors = capstoneErrors(dir).join('\n');
  assert.match(errors, /milestone "fourth-milestone" needs def check_fourth_milestone\(project\)/);
  assert.match(errors, /Milestones: needs one ### per lesson\.json milestone/);
  assert.match(errors, /pass_score must be an integer between 1 and the milestone total \(12\)/);
  assert.match(errors, /minutes must be whole hours between 240 and 1200/);
  assert.match(errors, /expected 0 "pre" question/);
});

test('milestones are rejected on a Build lesson', () => {
  const dir = tmpCurriculum();
  const jsonFile = path.join(firstLessonDir(dir), 'lesson.json');
  const json = JSON.parse(fs.readFileSync(jsonFile, 'utf8'));
  json.pass_score = 3;
  fs.writeFileSync(jsonFile, JSON.stringify(json));
  assert.ok(validateAll(dir).errors.some((e) => e.includes('milestones and pass_score are only for type Capstone')));
});

test('the model counts capstones separately and carries their milestones', () => {
  const dir = tmpCurriculum();
  const created = scaffoldCapstoneIn(dir);
  const jsonFile = path.join(created, 'lesson.json');
  const json = JSON.parse(fs.readFileSync(jsonFile, 'utf8'));
  json.status = 'published';
  fs.writeFileSync(jsonFile, JSON.stringify(json));
  const {phases, manifest} = validateAll(dir);
  const model = buildModel(phases, manifest);
  const lesson = model.lessonsById['capstones/build-a-paas'];
  assert.equal(model.stats.capstones, 1);
  assert.equal(lesson.passScore, 7);
  assert.equal(lesson.milestones.length, 3);
  const entry = tutorIndex(model).lessons.find((l) => l.id === 'capstones/build-a-paas');
  assert.match(entry.grader, /16-capstones\/01-build-a-paas\/grader\/grade\.sh$/);
});
