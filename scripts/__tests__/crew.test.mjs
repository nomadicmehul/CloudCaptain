import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import url from 'node:url';

const here = path.dirname(url.fileURLToPath(import.meta.url));
const site = path.resolve(here, '..', '..', 'website');
const crew = await import(url.pathToFileURL(path.join(site, 'src/components/Crew/crewCore.mjs')).href);
const config = JSON.parse(fs.readFileSync(path.join(site, 'src/data/crew.json'), 'utf8'));

const pr = (number, login, mergedAt, title = `PR ${number}`, extra = {}) => ({
  number,
  title,
  merged_at: mergedAt,
  user: {login, type: 'User', avatar_url: `https://avatars.example/${login}`},
  labels: [],
  ...extra,
});

const pulls = [
  pr(5, 'bob', '2026-11-03T10:00:00Z'),
  pr(1, 'nomadicmehul', '2026-11-01T10:00:00Z'),
  pr(2, 'alice', '2026-11-02T10:00:00Z', 'Fix: typo in docs'),
  pr(3, 'dependabot[bot]', '2026-11-02T11:00:00Z', 'Bump x', {user: {login: 'dependabot[bot]', type: 'Bot'}}),
  pr(4, 'Alice', '2026-11-04T10:00:00Z', 'Add: shellcheck step to the workflow', {labels: [{name: 'ci'}]}),
  pr(6, 'carol', null),
];

test('the roster numbers crew in order of their first merged PR, skipping the captain, bots and unmerged PRs', () => {
  const roster = crew.buildRoster(pulls, config);
  assert.deepEqual([...roster.keys()], ['nomadicmehul', 'alice', 'bob']);
  assert.equal(roster.get('nomadicmehul').captain, true);
  assert.equal(roster.get('nomadicmehul').number, null);
  assert.equal(roster.get('alice').number, 1);
  assert.equal(roster.get('bob').number, 2);
  assert.deepEqual(roster.get('alice').voyages.map((v) => v.number), [2, 4], 'logins match case-insensitively');
  assert.equal(roster.has('carol'), false);
});

test('ranks follow the ladder in crew.json and say how far the next one is', () => {
  assert.deepEqual(crew.rankFor(1, false, config), {title: 'Deckhand', emoji: '🪢', next: {title: 'Able Seaman', needed: 1}});
  assert.equal(crew.rankFor(3, false, config).title, 'Able Seaman');
  assert.deepEqual(crew.rankFor(3, false, config).next, {title: 'Bosun', needed: 1});
  assert.equal(crew.rankFor(4, false, config).title, 'Bosun');
  assert.equal(crew.rankFor(50, false, config).next, null);
  assert.equal(crew.rankFor(0, false, config).title, 'Stowaway');
  assert.equal(crew.rankFor(1, true, config).title, 'Captain');
});

test('ports come from PR labels and title keywords, in config order', () => {
  const roster = crew.buildRoster(pulls, config);
  const ports = crew.portsFor(roster.get('alice').voyages, config).map((p) => p.id);
  assert.deepEqual(ports, ['docs', 'ci', 'bugfix']);
  assert.deepEqual(crew.portsFor([{title: 'Something else', labels: []}], config), []);
});

test('every post style fits each channel limit, even with a very long PR title', () => {
  const long = 'Add: ' + 'a really long pull request title about refreshing deprecated GitHub Actions examples '.repeat(3);
  const member = {login: 'a-very-long-github-username', captain: false, number: 123, voyages: [{number: 9999, title: long, mergedAt: '2026-11-21T10:00:00Z', labels: ['curriculum', 'dx']}]};
  for (const style of crew.STYLES) {
    const posts = crew.buildPosts(member, config, style);
    assert.ok(crew.xLength(posts.x) <= 280, `${style}: X post is ${crew.xLength(posts.x)}`);
    assert.ok(crew.graphemeLength(posts.bluesky) <= 300, `${style}: Bluesky post is ${crew.graphemeLength(posts.bluesky)}`);
    assert.ok(posts.linkedin.length <= 3000);
    assert.match(posts.x, /@nomadicmehul/);
    assert.match(posts.linkedin, /@Mehul Patel/);
    assert.match(posts.bluesky, /@nomadicmehul\.bsky\.social/, 'Bluesky mentions need the full handle');
    assert.ok(posts.x.includes('https://cloudcaptain.io/crew?u=a-very-long-github-username'));
    assert.doesNotMatch(posts.linkedin, /\ba [AEIOU]/, 'uses "an" before a vowel');
  }
});

test('helpers: titles, ordinals, X length and share links', () => {
  assert.equal(crew.cleanTitle('Fix: curriculum scripts reject unknown flags (#238)'), 'curriculum scripts reject unknown flags');
  assert.equal(crew.cleanTitle('Docs: refresh examples'), 'refresh examples');
  assert.deepEqual([1, 2, 3, 4, 11, 12, 13, 21, 22, 101].map(crew.ordinal), ['1st', '2nd', '3rd', '4th', '11th', '12th', '13th', '21st', '22nd', '101st']);
  assert.equal(crew.withArticle('Able Seaman'), 'an Able Seaman');
  assert.equal(crew.withArticle('Bosun'), 'a Bosun');
  assert.equal(crew.xLength('see https://example.com/a/very/long/path/that/is/long'), 4 + 23);
  assert.equal(crew.xLength('⚓'), 2);
  assert.match(crew.shareUrl('x', 'hi there'), /^https:\/\/x\.com\/intent\/post\?text=hi%20there$/);
  assert.match(crew.shareUrl('bluesky', 'a&b'), /^https:\/\/bsky\.app\/intent\/compose\?text=a%26b$/);
  assert.match(crew.shareUrl('linkedin', 'x'), /^https:\/\/www\.linkedin\.com\/feed\/\?shareActive=true&text=x$/);
});
