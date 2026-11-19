import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import url from 'node:url';

const here = path.dirname(url.fileURLToPath(import.meta.url));
const scriptsDir = path.resolve(here, '..');

function runScript(scriptName, args = [], env = {}, cwd) {
  const script = path.join(scriptsDir, scriptName);
  const mock = path.join(here, 'mock-fetch.mjs');
  const result = spawnSync('node', ['--import', mock, script, ...args], {
    cwd: cwd || process.cwd(),
    env: { ...process.env, ...env, MOCK_FETCH: '1' },
    encoding: 'utf8',
    timeout: 15000,
  });
  return result;
}

test('update-versions.mjs writes new versions from recorded GitHub responses', () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-ver-'));
  const versionsFile = path.join(tmp, 'versions.json');
  const initial = {
    updatedAt: '2020-01-01T00:00:00.000Z',
    track: {
      kubernetes: { repo: 'kubernetes/kubernetes', label: 'Kubernetes' },
      helm: { repo: 'helm/helm', label: 'Helm' },
    },
    versions: {
      kubernetes: { version: 'v1.28.0', publishedAt: '2023-08-15' },
      helm: { version: 'v3.13.0', publishedAt: '2023-11-08' },
    },
  };
  fs.writeFileSync(versionsFile, JSON.stringify(initial, null, 2) + '\n');

  const mockResponses = {
    '/repos/kubernetes/kubernetes/releases/latest': {
      ok: true,
      json: { tag_name: 'v1.29.0', published_at: '2023-12-13T10:00:00Z' },
    },
    '/repos/helm/helm/releases/latest': {
      ok: true,
      json: { tag_name: 'v3.14.0', published_at: '2024-01-10T10:00:00Z' },
    },
  };

  const result = runScript('update-versions.mjs', [], {
    TEST_VERSIONS_FILE: versionsFile,
    MOCK_RESPONSES: JSON.stringify(mockResponses),
  }, tmp);

  assert.equal(result.status, 0, result.stderr || result.stdout);
  const updated = JSON.parse(fs.readFileSync(versionsFile, 'utf8'));
  assert.equal(updated.versions.kubernetes.version, 'v1.29.0');
  assert.equal(updated.versions.kubernetes.publishedAt, '2023-12-13');
  assert.equal(updated.versions.helm.version, 'v3.14.0');
  assert.ok(updated.updatedAt > initial.updatedAt);
  assert.match(result.stdout, /Kubernetes: v1\.28\.0 → v1\.29\.0/);
  assert.match(result.stdout, /Helm: v3\.13\.0 → v3\.14\.0/);
});

test('update-versions.mjs leaves file unchanged on bad input (404 and empty tags)', () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-ver-bad-'));
  const versionsFile = path.join(tmp, 'versions.json');
  const initial = {
    updatedAt: '2020-01-01T00:00:00.000Z',
    track: {
      kubernetes: { repo: 'kubernetes/kubernetes', label: 'Kubernetes' },
    },
    versions: {
      kubernetes: { version: 'v1.28.0', publishedAt: '2023-08-15' },
    },
  };
  fs.writeFileSync(versionsFile, JSON.stringify(initial, null, 2) + '\n');
  const before = fs.readFileSync(versionsFile, 'utf8');

  const mockResponses = {
    '/repos/kubernetes/kubernetes/releases/latest': { ok: false, status: 404 },
    '/repos/kubernetes/kubernetes/tags?per_page=1': { ok: true, json: [] },
  };

  const result = runScript('update-versions.mjs', [], {
    TEST_VERSIONS_FILE: versionsFile,
    MOCK_RESPONSES: JSON.stringify(mockResponses),
  }, tmp);

  // Script exits 1 if all fail
  assert.equal(result.status, 1, result.stderr);
  const after = fs.readFileSync(versionsFile, 'utf8');
  assert.equal(after, before, 'file should remain unchanged on failure');
  assert.match(result.stderr, /no releases or tags/);
});

test('update-visitor-stats.mjs writes badges and charts from recorded GoatCounter responses', () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-stats-'));
  const outDir = path.join(tmp, 'stats');
  fs.mkdirSync(outDir, { recursive: true });

  const mockResponses = {
    '/stats/total': {
      ok: true,
      json: {
        total: 12345,
        total_events: 12345,
        total_utms: 0,
        stats: [
          { day: '2026-11-17', daily: 42 },
          { day: '2026-11-16', daily: 30 },
        ],
      },
    },
  };

  const result = runScript(
    'update-visitor-stats.mjs',
    [],
    {
      GOATCOUNTER_SITE_CODE: 'testsite',
      GOATCOUNTER_API_TOKEN: 'fake-token',
      TEST_OUT_DIR: outDir,
      MOCK_RESPONSES: JSON.stringify(mockResponses),
    },
    tmp,
  );

  assert.equal(result.status, 0, result.stderr || result.stdout);
  assert.match(result.stdout, /Updated visitor stats: total=12345/);

  const total = JSON.parse(fs.readFileSync(path.join(outDir, 'visitors-total.json'), 'utf8'));
  assert.equal(total.message, '12,345');
  assert.equal(total.label, 'visits total');

  const week = JSON.parse(fs.readFileSync(path.join(outDir, 'visitors-week.json'), 'utf8'));
  assert.ok(Number(week.message.replace(/,/g, '')) >= 42);

  const chart = fs.readFileSync(path.join(outDir, 'chart-light.svg'), 'utf8');
  assert.match(chart, /<svg/);
  assert.match(chart, /Visitor traffic/);
});

test('update-visitor-stats.mjs skips when env not set', () => {
  const result = runScript('update-visitor-stats.mjs', [], {}, '/tmp');
  assert.equal(result.status, 0);
  assert.match(result.stderr || result.stdout, /not set — skipping/);
});
