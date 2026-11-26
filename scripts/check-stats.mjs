#!/usr/bin/env node
/**
 * Checks that the hand-typed curriculum numbers match the generator.
 *
 *   node scripts/check-stats.mjs         report mismatches (CI)
 *   node scripts/check-stats.mjs --fix   rewrite the numbers and the ROADMAP table in place
 *   node scripts/check-stats.mjs --help  print this help
 *
 * - README.md and every i18n/README.*.md: the lesson, live phase, hour and
 *   test counts in the stats line (`<strong>113 lessons</strong> · ...`) must
 *   equal `node scripts/build-curriculum.mjs --json`. `3,350`, `3.350`,
 *   `3 350` (also with a no-break or narrow no-break space) and `3'350` are
 *   accepted as thousands separators; a fix keeps the file's own separator.
 * - ROADMAP.md: the phase table must be what `--roadmap` would write.
 *
 * Exits 1 and names the file, line and number to change on any mismatch.
 * Zero dependencies. Node >= 20.
 */
import fs from 'node:fs';
import path from 'node:path';
import url from 'node:url';
import {run, repoRoot} from './build-curriculum.mjs';
import {roadmapTable, renderRoadmap} from './lib/emit.mjs';

// The first four <strong> numbers of the stats line, in this order.
export const FIELDS = [
  {key: 'lessons', label: 'lessons', expected: (s) => s.lessons},
  {key: 'phasesLive', label: 'live phases', expected: (s) => s.phasesLive},
  {key: 'hours', label: 'hours', expected: (s) => Math.round(s.hours)},
  {key: 'tests', label: 'tests', expected: (s) => s.tests},
];

const FIX_HINT = 'run `node scripts/check-stats.mjs --fix` to update every file';
const SEPS = ",.'   ";
const STRONG_RE = /<strong>([^<]*)<\/strong>/g;
// A grouped number ("3,350", "3 350", "1'234'567") or a plain one ("196", "196.5").
const NUMBER_RE = new RegExp(`\\d{1,3}(?:[${SEPS}]\\d{3})+(?!\\d)|\\d+(?:\\.\\d+)?`);
const GROUP_RE = new RegExp(`[${SEPS}](?=\\d{3}(?!\\d))`);

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
// Each number carries its column (`index`) in that line so it can be replaced.
export function parseStatsLine(text) {
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const numbers = [];
    for (const m of lines[i].matchAll(STRONG_RE)) {
      const n = NUMBER_RE.exec(m[1]);
      if (n) numbers.push({...parseNumber(n[0]), index: m.index + '<strong>'.length + n.index});
    }
    if (numbers.length >= FIELDS.length) return {line: i + 1, numbers: numbers.slice(0, FIELDS.length)};
  }
  return null;
}

// The numbers on the stats line that differ from the generator, with the text to put in their place.
export function readmeChanges(text, stats) {
  const found = parseStatsLine(text);
  if (!found) return null;
  const fileSep = found.numbers.find((n) => n.sep)?.sep || ',';
  const changes = [];
  FIELDS.forEach((field, i) => {
    const have = found.numbers[i];
    const want = field.expected(stats);
    if (have.value === want) return;
    changes.push({field, have, want, wantText: formatNumber(want, have.sep || fileSep)});
  });
  return {line: found.line, changes};
}

export function checkReadme(relPath, text, stats) {
  const result = readmeChanges(text, stats);
  if (!result) return [`${relPath}: no stats line found (expected <strong>N lessons</strong> · <strong>N phases</strong> · <strong>N hours</strong> · <strong>N tests</strong>)`];
  return result.changes.map(
    ({field, have, want, wantText}) => `${relPath}:${result.line}: ${field.label} is "${have.raw}" but the curriculum has ${want}; change "${have.raw}" to "${wantText}"`,
  );
}

// README text with the stale numbers on the stats line replaced; line endings are kept.
export function fixReadme(text, stats) {
  const result = readmeChanges(text, stats);
  if (!result || result.changes.length === 0) return text;
  const lines = text.split('\n');
  let line = lines[result.line - 1];
  for (const {have, wantText} of [...result.changes].sort((x, y) => y.have.index - x.have.index)) {
    line = line.slice(0, have.index) + wantText + line.slice(have.index + have.raw.length);
  }
  lines[result.line - 1] = line;
  return lines.join('\n');
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

// Rewrites the stale numbers in every README and the ROADMAP table. Returns the files it changed.
export function fixStats({root = repoRoot, model}) {
  const changed = [];
  const rewrite = (rel, fix) => {
    const file = path.join(root, rel);
    const text = fs.readFileSync(file, 'utf8');
    const next = fix(text);
    if (next === text) return;
    fs.writeFileSync(file, next);
    changed.push(rel);
  };
  for (const rel of readmeFiles(root)) rewrite(rel, (text) => fixReadme(text, model.stats));
  rewrite('ROADMAP.md', (text) => renderRoadmap(text, model));
  return changed;
}

function usage() {
  return [
    'usage: node scripts/check-stats.mjs [--fix]',
    '',
    '  (no flag)  fail if README.md, i18n/README.*.md or the ROADMAP.md table drift from the generator',
    '  --fix      rewrite those numbers and the table in place',
    '  --help     print this help',
  ].join('\n');
}

const isMain = process.argv[1] && path.resolve(process.argv[1]) === url.fileURLToPath(import.meta.url);
if (isMain) {
  const args = process.argv.slice(2);
  if (args.includes('--help')) {
    console.log(usage());
    process.exit(0);
  }
  const unknown = args.filter((a) => a !== '--fix');
  if (unknown.length) {
    console.error(`check-stats: unknown argument(s): ${unknown.join(', ')}\n\n${usage()}`);
    process.exit(2);
  }
  const result = run({check: true, quiet: true});
  if (!result.ok) {
    console.error('check-stats: the curriculum does not validate; run `node scripts/build-curriculum.mjs --check` first');
    process.exit(1);
  }
  const s = result.model.stats;
  const summary = `${s.lessons} lessons, ${s.phasesLive} live phases, ${Math.round(s.hours)} hours, ${s.tests} tests`;
  if (args.includes('--fix')) {
    const changed = fixStats({model: result.model});
    console.log(changed.length ? `check-stats: updated ${changed.join(', ')} (${summary})` : `check-stats: nothing to fix (${summary})`);
  }
  const problems = checkStats({model: result.model});
  if (problems.length) {
    console.error(`check-stats: ${problems.length} number(s) out of date (generator: ${summary})`);
    for (const p of problems) console.error(`  ${p}`);
    console.error(`check-stats: ${FIX_HINT}`);
    process.exit(1);
  }
  console.log(`check-stats: README (${readmeFiles(repoRoot).length} languages) and ROADMAP.md match the curriculum (${summary})`);
}
