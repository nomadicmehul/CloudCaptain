import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import url from 'node:url';
import {run, scaffold} from '../build-curriculum.mjs';
import {parseMarkdown} from '../lib/markdown.mjs';
import {discover, loadManifest, countTests} from '../lib/discover.mjs';
import {validateLesson, validateGlobal} from '../lib/validate.mjs';
import {buildModel} from '../lib/emit.mjs';

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
  fs.cpSync(path.join(realCurriculum, 'phases', SAMPLE_PHASE), path.join(dir, 'phases', SAMPLE_PHASE), {recursive: true});
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

test('draft lessons are excluded from the model and sidebar', () => {
  const dir = tmpCurriculum();
  const lesson = firstLessonDir(dir);
  const jsonFile = path.join(lesson, 'lesson.json');
  const json = JSON.parse(fs.readFileSync(jsonFile, 'utf8'));
  json.status = 'draft';
  fs.writeFileSync(jsonFile, JSON.stringify(json));
  const {phases, manifest} = validateAll(dir);
  const model = buildModel(phases, manifest);
  assert.equal(model.stats.lessons, 0);
  assert.ok(!model.sidebar.some((item) => item.type === 'category' && item.label.includes('Containers')));
});

test('scaffold creates a lesson folder that the validator can read', () => {
  const dir = tmpCurriculum();
  const created = scaffold({phaseDir: SAMPLE_PHASE, lessonSlug: 'image-layers', type: 'Learn', langs: ['python'], title: 'Images Are Just Layers', curriculum: dir});
  assert.ok(fs.existsSync(path.join(created, 'docs', 'en.md')));
  assert.ok(fs.existsSync(path.join(created, 'code', 'tests', 'test_main.py')));
  const json = JSON.parse(fs.readFileSync(path.join(created, 'lesson.json'), 'utf8'));
  assert.equal(json.id, 'containers/image-layers');
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
