#!/usr/bin/env node
/**
 * Run the tests of one lesson (by id) or of every lesson (--all).
 *
 *   node scripts/run-lesson-tests.mjs containers/process-isolation
 *   node scripts/run-lesson-tests.mjs --all
 *   node scripts/run-lesson-tests.mjs --all --smoke     # lesson.json "smoke": real tools, Linux CI
 *
 * Commands come from lesson.json "tests" (or "smoke") and run from the lesson folder.
 */
import path from 'node:path';
import url from 'node:url';
import {spawnSync} from 'node:child_process';
import {discover, loadManifest} from './lib/discover.mjs';

const here = path.dirname(url.fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..');
const curriculumRoot = path.join(repoRoot, 'curriculum');

const args = process.argv.slice(2);
const all = args.includes('--all');
const smoke = args.includes('--smoke');
const ids = args.filter((a) => !a.startsWith('--'));
if (!all && ids.length === 0) {
  console.error('usage: run-lesson-tests.mjs <phase-slug>/<lesson-slug> [...] | --all');
  process.exit(2);
}

const manifest = loadManifest(curriculumRoot);
const phases = discover(curriculumRoot, manifest);
const lessons = phases.flatMap((p) => p.lessons).filter((l) => l.json && (all || ids.includes(l.json.id)));

const missing = ids.filter((id) => !lessons.some((l) => l.json.id === id));
if (missing.length) {
  console.error(`unknown lesson id(s): ${missing.join(', ')}`);
  process.exit(2);
}

let failures = 0;
let ran = 0;
for (const lesson of lessons) {
  const tests = smoke ? (lesson.json.smoke ? {smoke: lesson.json.smoke} : {}) : lesson.json.tests || {};
  if (Object.keys(tests).length === 0) {
    console.log(`▸ ${lesson.json.id}: no ${smoke ? 'smoke' : 'test'} commands`);
    continue;
  }
  for (const [lang, cmd] of Object.entries(tests)) {
    console.log(`\n▸ ${lesson.json.id} [${lang}] $ ${cmd}`);
    const res = spawnSync(cmd, {cwd: lesson.dir, shell: true, stdio: 'inherit', env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}});
    ran += 1;
    if (res.status !== 0) {
      failures += 1;
      console.error(`✖ ${lesson.json.id} [${lang}] exited with ${res.status}`);
    }
  }
}

console.log(`\n${ran - failures}/${ran} test command(s) passed across ${lessons.length} lesson(s)`);
process.exit(failures ? 1 : 0);
