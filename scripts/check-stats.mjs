#!/usr/bin/env node
/**
 * Checks that the hand-typed curriculum numbers match the generator.
 *
 *   node scripts/check-stats.mjs
 *
 * - README.md and every i18n/README.*.md: the lesson, live phase, hour and
 *   test counts in the stats line (`<strong>113 lessons</strong> · ...`) must
 *   equal `node scripts/build-curriculum.mjs --json`. Both `3,350` and `3.350`
 *   are accepted as thousands separators.
 * - ROADMAP.md: the phase table must be what `--roadmap` would write.
 *
 * Exits 1 and names the file, line and number to change on any mismatch.
 * Zero dependencies. Node >= 20.
 */
import fs from 'node:fs';
import path from 'node:path';
import url from 'node:url';
import {run, repoRoot} from './build-curriculum.mjs';
import {roadmapTable} from './lib/emit.mjs';

// The first four <strong> numbers of the stats line, in this order.
export const FIELDS = [
  {key: 'lessons', label: 'lessons', expected: (s) => s.lessons},
  {key: 'phasesLive', label: 'live phases', expected: (s) => s.phasesLive},
  {key: 'hours', label: 'hours', expected: (s) => Math.round(s.hours)},
  {key: 'tests', label: 'tests', expected: (s) => s.tests},
];

const STRONG_RE = /<strong>([^<]*)<\/strong>/g;
const NUMBER_RE = /\d(?:[\d.,]*\d)?/;
const GROUP_RE = /[.,](?=\d{3}(?!\d))/;

// "3.350" -> {value: 3350, sep: '.'}, "196" -> {value: 196, sep: null}, "196.5" -> {value: 196.5, sep: null}
export function parseNumber(raw) {
  const m = GROUP_RE.exec(raw);
  const sep = m ? m[0] : null;
  const value = Number(sep ? raw.split(sep).join('') : raw);
  return {raw, value, sep};
}

export function formatNumber(n, sep = ',') {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, sep);
}

// Finds the first line with at least FIELDS.length numeric <strong> elements.
export function parseStatsLine(text) {
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const numbers = [];
    for (const m of lines[i].matchAll(STRONG_RE)) {
      const n = NUMBER_RE.exec(m[1]);
      if (n) numbers.push(parseNumber(n[0]));
    }
    if (numbers.length >= FIELDS.length) return {line: i + 1, numbers: numbers.slice(0, FIELDS.length)};
  }
  return null;
}

export function checkReadme(relPath, text, stats) {
  const found = parseStatsLine(text);
  if (!found) return [`${relPath}: no stats line found (expected <strong>N lessons</strong> · <strong>N phases</strong> · <strong>N hours</strong> · <strong>N tests</strong>)`];
  const fileSep = found.numbers.find((n) => n.sep)?.sep || ',';
  const problems = [];
  FIELDS.forEach((field, i) => {
    const have = found.numbers[i];
    const want = field.expected(stats);
    if (have.value === want) return;
    const wantText = formatNumber(want, have.sep || fileSep);
    problems.push(`${relPath}:${found.line}: ${field.label} is "${have.raw}" but the curriculum has ${want}; change "${have.raw}" to "${wantText}"`);
  });
  return problems;
}

export function checkRoadmap(relPath, text, model) {
  const start = '<!-- curriculum:start -->';
  const end = '<!-- curriculum:end -->';
  const lines = text.split(/\r?\n/);
  const a = lines.findIndex((l) => l.includes(start));
  const b = lines.findIndex((l) => l.includes(end));
  if (a === -1 || b === -1 || b < a) return [`${relPath}: needs ${start} and ${end} markers`];
  const have = lines.slice(a + 1, b);
  const want = roadmapTable(model).split('\n');
  const problems = [];
  for (let i = 0; i < Math.max(have.length, want.length); i++) {
    if (have[i] === want[i]) continue;
    const lineNo = a + 2 + i;
    if (have[i] === undefined) problems.push(`${relPath}:${lineNo}: missing row "${want[i]}"`);
    else if (want[i] === undefined) problems.push(`${relPath}:${lineNo}: remove row "${have[i]}"`);
    else problems.push(`${relPath}:${lineNo}: change "${have[i]}" to "${want[i]}"`);
  }
  if (problems.length) problems.push(`${relPath}: or run \`node scripts/build-curriculum.mjs --roadmap\` to regenerate the table`);
  return problems;
}

export function readmeFiles(root) {
  const files = ['README.md'];
  const i18n = path.join(root, 'i18n');
  if (fs.existsSync(i18n)) {
    for (const name of fs.readdirSync(i18n).sort()) if (/^README\.[\w-]+\.md$/.test(name)) files.push(`i18n/${name}`);
  }
  return files;
}

export function checkStats({root = repoRoot, model}) {
  const problems = [];
  for (const rel of readmeFiles(root)) {
    problems.push(...checkReadme(rel, fs.readFileSync(path.join(root, rel), 'utf8'), model.stats));
  }
  problems.push(...checkRoadmap('ROADMAP.md', fs.readFileSync(path.join(root, 'ROADMAP.md'), 'utf8'), model));
  return problems;
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === url.fileURLToPath(import.meta.url);
if (isMain) {
  const result = run({check: true, quiet: true});
  if (!result.ok) {
    console.error('check-stats: the curriculum does not validate; run `node scripts/build-curriculum.mjs --check` first');
    process.exit(1);
  }
  const problems = checkStats({model: result.model});
  const s = result.model.stats;
  const summary = `${s.lessons} lessons, ${s.phasesLive} live phases, ${Math.round(s.hours)} hours, ${s.tests} tests`;
  if (problems.length) {
    console.error(`check-stats: ${problems.length} number(s) out of date (generator: ${summary})`);
    for (const p of problems) console.error(`  ${p}`);
    process.exit(1);
  }
  console.log(`check-stats: README (${readmeFiles(repoRoot).length} languages) and ROADMAP.md match the curriculum (${summary})`);
}
