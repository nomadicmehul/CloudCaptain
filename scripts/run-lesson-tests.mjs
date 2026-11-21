#!/usr/bin/env node
/**
 * Run the tests of one lesson (by id) or of every lesson (--all).
 *
 *   node scripts/run-lesson-tests.mjs containers/process-isolation
 *   node scripts/run-lesson-tests.mjs --all
 *   node scripts/run-lesson-tests.mjs --all --smoke     # lesson.json "smoke": real tools, Linux CI
 *   node scripts/run-lesson-tests.mjs --all --smoke --phase=orchestration,iac   # only these phases
 *   node scripts/run-lesson-tests.mjs --list-phases --smoke                    # JSON array of phase slugs (CI matrix)
 *   node scripts/run-lesson-tests.mjs --help                                   # print this help
 *
 * Commands come from lesson.json "tests" (or "smoke") and run from the lesson folder.
 */
import fs from 'node:fs';
import path from 'node:path';
import url from 'node:url';
import {spawnSync} from 'node:child_process';
import {discover, loadManifest} from './lib/discover.mjs';

const here = path.dirname(url.fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..');
const curriculumRoot = path.join(repoRoot, 'curriculum');

function usage() {
  return fs.readFileSync(url.fileURLToPath(import.meta.url), 'utf8').match(/\/\*\*\n([\s\S]*?)\n \*\//)[1].replace(/^ \* ?/gm, '');
}

const args = process.argv.slice(2);
if (args.includes('--help')) {
  console.log(usage());
  process.exit(0);
}
const unknown = args.filter((a) => a.startsWith('--') && !['--all', '--smoke', '--list-phases'].includes(a) && !a.startsWith('--phase='));
if (unknown.length) {
  console.error(`unknown flag(s): ${unknown.join(', ')}\n\n${usage()}`);
  process.exit(2);
}
const all = args.includes('--all');
const smoke = args.includes('--smoke');
const listPhases = args.includes('--list-phases');
const phaseArg = args.find((a) => a.startsWith('--phase='));
const onlyPhases = phaseArg ? phaseArg.slice('--phase='.length).split(',').filter(Boolean) : null;
const ids = args.filter((a) => !a.startsWith('--'));
if (!all && !listPhases && ids.length === 0) {
  console.error('usage: run-lesson-tests.mjs <phase-slug>/<lesson-slug> [...] | --all [--phase=a,b] | --list-phases');
  process.exit(2);
}

const manifest = loadManifest(curriculumRoot);
const phases = discover(curriculumRoot, manifest);
const phaseSlug = (lesson) => lesson.json.id.split('/')[0];

if (listPhases) {
  const slugs = [...new Set(phases.flatMap((p) => p.lessons).filter((l) => l.json && (!smoke || l.json.smoke)).map(phaseSlug))];
  console.log(JSON.stringify(slugs));
  process.exit(0);
}

const lessons = phases
  .flatMap((p) => p.lessons)
  .filter((l) => l.json && (all || ids.includes(l.json.id)))
  .filter((l) => !onlyPhases || onlyPhases.includes(phaseSlug(l)));
if (onlyPhases && lessons.length === 0) {
  console.error(`no lessons in phase(s): ${onlyPhases.join(', ')}`);
  process.exit(2);
}

const missing = ids.filter((id) => !lessons.some((l) => l.json.id === id));
if (missing.length) {
  console.error(`unknown lesson id(s): ${missing.join(', ')}`);
  process.exit(2);
}

const failed = [];
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
      failed.push(`${lesson.json.id} [${lang}]`);
      console.error(`✖ ${lesson.json.id} [${lang}] exited with ${res.status}`);
    }
  }
}

console.log(`\n${ran - failed.length}/${ran} test command(s) passed across ${lessons.length} lesson(s)`);
if (failed.length) console.log(`failed:\n${failed.map((f) => `  ${f}`).join('\n')}`);
process.exit(failed.length ? 1 : 0);
