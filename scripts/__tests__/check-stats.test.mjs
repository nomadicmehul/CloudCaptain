import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {parseNumber, formatNumber, parseStatsLine, checkReadme, fixReadme, checkRoadmap, checkStats, fixStats, readmeFiles} from '../check-stats.mjs';
import {roadmapTable} from '../lib/emit.mjs';

const stats = {lessons: 113, phasesLive: 17, hours: 196.2, tests: 3350};

function model(phaseLessons = [1, 8, 0]) {
  return {
    stats,
    phases: phaseLessons.map((n, i) => ({number: String(i).padStart(2, '0'), title: `Phase ${i}`, lessons: Array.from({length: n}, () => ({}))})),
  };
}

const en = (n = {}) =>
  `<div>\n  <p>\n    <strong>${n.lessons ?? '113'} lessons</strong> · <strong>${n.phases ?? '17'} phases live</strong> · <strong>${n.hours ?? '196'} hours</strong> · <strong>${n.tests ?? '3,350'} tests</strong> · <strong>223 skills, prompts &amp; MCP servers</strong>\n  </p>\n</div>\n`;
const de = '<p>\n<strong>113 Lektionen</strong> · <strong>17 Phasen live</strong> · <strong>196 Stunden</strong> · <strong>3.350 Tests</strong> · <strong>223 Skills</strong>\n</p>\n';
const ja = '<p>\n<strong>113 レッスン</strong> · <strong>17 フェーズ公開中</strong> · <strong>196 時間</strong> · <strong>3,350 テスト</strong> · <strong>スキル 223 個</strong>\n</p>\n';

test('parseNumber accepts comma and dot thousands separators', () => {
  assert.deepEqual(parseNumber('3,350'), {raw: '3,350', value: 3350, sep: ','});
  assert.deepEqual(parseNumber('3.350'), {raw: '3.350', value: 3350, sep: '.'});
  assert.deepEqual(parseNumber('1,234,567'), {raw: '1,234,567', value: 1234567, sep: ','});
  assert.equal(parseNumber('196').value, 196);
  assert.equal(parseNumber('196.5').value, 196.5);
});

test('parseNumber accepts space, no-break space and apostrophe separators', () => {
  assert.deepEqual(parseNumber('3 350'), {raw: '3 350', value: 3350, sep: ' '});
  assert.deepEqual(parseNumber('3 350'), {raw: '3 350', value: 3350, sep: ' '});
  assert.deepEqual(parseNumber('3 350'), {raw: '3 350', value: 3350, sep: ' '});
  assert.deepEqual(parseNumber("3'350"), {raw: "3'350", value: 3350, sep: "'"});
});

test('formatNumber groups thousands with the given separator', () => {
  assert.equal(formatNumber(3412), '3,412');
  assert.equal(formatNumber(3412, '.'), '3.412');
  assert.equal(formatNumber(113, '.'), '113');
});

test('parseStatsLine finds the four numbers and their line', () => {
  const found = parseStatsLine(en());
  assert.equal(found.line, 3);
  assert.deepEqual(found.numbers.map((n) => n.value), [113, 17, 196, 3350]);
  assert.deepEqual(parseStatsLine(ja).numbers.map((n) => n.value), [113, 17, 196, 3350]);
  assert.equal(parseStatsLine('# no stats here\n'), null);
});

test('checkReadme passes when every language matches', () => {
  assert.deepEqual(checkReadme('README.md', en(), stats), []);
  assert.deepEqual(checkReadme('i18n/README.de.md', de, stats), []);
  assert.deepEqual(checkReadme('i18n/README.ja.md', ja, stats), []);
  assert.deepEqual(checkReadme('README.md', en().replace(/\n/g, '\r\n'), stats), []);
});

test('checkReadme names the file, line and number to change', () => {
  const problems = checkReadme('README.md', en({lessons: '110', tests: '3,300'}), stats);
  assert.deepEqual(problems, [
    'README.md:3: lessons is "110" but the curriculum has 113; change "110" to "113"',
    'README.md:3: tests is "3,300" but the curriculum has 3350; change "3,300" to "3,350"',
  ]);
});

test('checkReadme keeps the file\'s own thousands separator in the suggestion', () => {
  const problems = checkReadme('i18n/README.de.md', de, {...stats, tests: 3412});
  assert.deepEqual(problems, ['i18n/README.de.md:2: tests is "3.350" but the curriculum has 3412; change "3.350" to "3.412"']);
});

test('checkReadme reads space and apostrophe separated numbers whole and keeps the separator', () => {
  for (const sep of [' ', ' ', ' ', "'"]) {
    const text = en({tests: `3${sep}350`});
    assert.deepEqual(parseStatsLine(text).numbers.map((n) => n.value), [113, 17, 196, 3350]);
    assert.deepEqual(checkReadme('i18n/README.fr.md', text, stats), []);
    assert.deepEqual(checkReadme('i18n/README.fr.md', text, {...stats, tests: 3412}), [
      `i18n/README.fr.md:3: tests is "3${sep}350" but the curriculum has 3412; change "3${sep}350" to "3${sep}412"`,
    ]);
  }
});

test('fixReadme replaces only the stale numbers on the stats line', () => {
  const fixed = fixReadme(en({lessons: '110', hours: '190', tests: '3,300'}), stats);
  assert.equal(fixed, en());
  assert.equal(fixReadme(en(), stats), en());
  assert.equal(fixReadme(de, {...stats, lessons: 1000, tests: 3412}), de.replace('113 Lektionen', '1.000 Lektionen').replace('3.350', '3.412'));
  assert.equal(fixReadme(en({tests: '3 350'}), {...stats, tests: 12345}), en({tests: '12 345'}));
  const crlf = en({lessons: '99'}).replace(/\n/g, '\r\n');
  assert.equal(fixReadme(crlf, stats), en().replace(/\n/g, '\r\n'));
  assert.equal(fixReadme('# no stats here\n', stats), '# no stats here\n');
});

test('checkReadme compares hours rounded to a whole number', () => {
  assert.deepEqual(checkReadme('README.md', en(), {...stats, hours: 195.6}), []);
  assert.equal(checkReadme('README.md', en(), {...stats, hours: 197.5}).length, 1);
});

test('checkReadme reports a missing stats line', () => {
  const [problem] = checkReadme('i18n/README.xx.md', '# CloudCaptain\n', stats);
  assert.match(problem, /^i18n\/README\.xx\.md: no stats line found/);
});

const roadmap = (table) => `# Roadmap\n\n<!-- curriculum:start -->\n${table}\n<!-- curriculum:end -->\n\nMore text.\n`;

test('checkRoadmap passes when --roadmap would produce no diff', () => {
  const m = model();
  assert.deepEqual(checkRoadmap('ROADMAP.md', roadmap(roadmapTable(m)), m), []);
  assert.deepEqual(checkRoadmap('ROADMAP.md', roadmap(roadmapTable(m)).replace(/\n/g, '\r\n'), m), []);
});

test('checkRoadmap names the row to change', () => {
  const stale = roadmap(roadmapTable(model([1, 7, 0])));
  const problems = checkRoadmap('ROADMAP.md', stale, model([1, 8, 0]));
  assert.deepEqual(problems, [
    'ROADMAP.md:7: change "| 01 | Phase 1 | 7 | in progress |" to "| 01 | Phase 1 | 8 | in progress |"',
    'ROADMAP.md: or run `node scripts/build-curriculum.mjs --roadmap` to regenerate the table',
  ]);
});

test('checkRoadmap reports missing rows and markers', () => {
  const stale = roadmap(roadmapTable(model([1, 8])));
  assert.match(checkRoadmap('ROADMAP.md', stale, model([1, 8, 0]))[0], /^ROADMAP\.md:8: missing row "\| 02 \| Phase 2 \| 0 \| planned \|"$/);
  assert.match(checkRoadmap('ROADMAP.md', '# Roadmap\n', model())[0], /needs <!-- curriculum:start -->/);
});

test('checkStats checks README.md, every i18n README and ROADMAP.md', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-check-stats-'));
  try {
    const m = model();
    fs.mkdirSync(path.join(root, 'i18n'));
    fs.writeFileSync(path.join(root, 'README.md'), en());
    fs.writeFileSync(path.join(root, 'i18n', 'README.de.md'), de);
    fs.writeFileSync(path.join(root, 'i18n', 'README.ja.md'), ja.replace('3,350', '3,349'));
    fs.writeFileSync(path.join(root, 'i18n', 'notes.md'), 'ignored');
    fs.writeFileSync(path.join(root, 'ROADMAP.md'), roadmap(roadmapTable(m)));
    assert.deepEqual(readmeFiles(root), ['README.md', 'i18n/README.de.md', 'i18n/README.ja.md']);
    assert.deepEqual(checkStats({root, model: m}), ['i18n/README.ja.md:2: tests is "3,349" but the curriculum has 3350; change "3,349" to "3,350"']);
  } finally {
    fs.rmSync(root, {recursive: true, force: true});
  }
});

test('fixStats rewrites the stale READMEs and the ROADMAP table, then checkStats passes', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-fix-stats-'));
  try {
    const m = model();
    fs.mkdirSync(path.join(root, 'i18n'));
    fs.writeFileSync(path.join(root, 'README.md'), en({tests: '3,300'}));
    fs.writeFileSync(path.join(root, 'i18n', 'README.de.md'), de);
    fs.writeFileSync(path.join(root, 'i18n', 'README.ja.md'), ja.replace('113 レッスン', '112 レッスン'));
    fs.writeFileSync(path.join(root, 'ROADMAP.md'), roadmap(roadmapTable(model([1, 7, 0]))));
    assert.deepEqual(fixStats({root, model: m}), ['README.md', 'i18n/README.ja.md', 'ROADMAP.md']);
    assert.deepEqual(checkStats({root, model: m}), []);
    assert.equal(fs.readFileSync(path.join(root, 'README.md'), 'utf8'), en());
    assert.equal(fs.readFileSync(path.join(root, 'i18n', 'README.de.md'), 'utf8'), de);
    assert.equal(fs.readFileSync(path.join(root, 'ROADMAP.md'), 'utf8'), roadmap(roadmapTable(m)));
    assert.deepEqual(fixStats({root, model: m}), []);
  } finally {
    fs.rmSync(root, {recursive: true, force: true});
  }
});
