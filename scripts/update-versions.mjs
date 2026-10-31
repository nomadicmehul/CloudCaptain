#!/usr/bin/env node
/**
 * Refresh curriculum/versions.json from GitHub releases (falls back to tags).
 *
 *   node scripts/update-versions.mjs            write changes
 *   node scripts/update-versions.mjs --check    print what would change, exit 0
 *
 * Uses GITHUB_TOKEN when present (CI) to avoid the anonymous rate limit.
 */
import fs from 'node:fs';
import path from 'node:path';
import url from 'node:url';

const here = path.dirname(url.fileURLToPath(import.meta.url));
const file = path.resolve(here, '..', 'curriculum', 'versions.json');
const check = process.argv.includes('--check');

const headers = {Accept: 'application/vnd.github+json', 'User-Agent': 'cloudcaptain-versions'};
if (process.env.GITHUB_TOKEN) headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;

async function getJson(apiPath) {
  const res = await fetch(`https://api.github.com${apiPath}`, {headers});
  if (!res.ok) throw new Error(`${res.status} ${apiPath}`);
  return res.json();
}

async function latest(repo) {
  try {
    const rel = await getJson(`/repos/${repo}/releases/latest`);
    return {version: rel.tag_name, publishedAt: String(rel.published_at || '').slice(0, 10)};
  } catch (e) {
    if (!String(e.message).startsWith('404')) throw e;
    const tags = await getJson(`/repos/${repo}/tags?per_page=1`);
    if (!tags.length) throw new Error(`no releases or tags for ${repo}`);
    return {version: tags[0].name, publishedAt: ''};
  }
}

const data = JSON.parse(fs.readFileSync(file, 'utf8'));
data.versions ||= {};
const changes = [];
const failures = [];

for (const [key, meta] of Object.entries(data.track)) {
  try {
    const next = await latest(meta.repo);
    const prev = data.versions[key];
    if (!prev || prev.version !== next.version) {
      changes.push(`${meta.label}: ${prev ? prev.version : '(none)'} → ${next.version}`);
      data.versions[key] = next;
    }
  } catch (e) {
    failures.push(`${meta.label}: ${e.message}`);
  }
}

if (changes.length) console.log(changes.map((c) => `  ${c}`).join('\n'));
else console.log('  no version changes');
if (failures.length) console.error(failures.map((f) => `  ! ${f}`).join('\n'));

if (!check && changes.length) {
  data.updatedAt = new Date().toISOString();
  fs.writeFileSync(file, JSON.stringify(data, null, 2) + '\n');
  console.log(`wrote ${path.relative(process.cwd(), file)}`);
}
process.exit(failures.length === Object.keys(data.track).length ? 1 : 0);
