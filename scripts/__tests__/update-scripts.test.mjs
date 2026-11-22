import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import url from 'node:url';
import {spawnSync} from 'node:child_process';

const here = path.dirname(url.fileURLToPath(import.meta.url));
const scriptsDir = path.resolve(here, '..');
const versionsScript = path.join(scriptsDir, 'update-versions.mjs');
const visitorStatsScript = path.join(scriptsDir, 'update-visitor-stats.mjs');

function makeTempEnv(responses) {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cc-update-test-'));
  const mockPath = path.join(tmpDir, 'fetch-mock.mjs');
  fs.writeFileSync(
    mockPath,
    `const responses = JSON.parse(process.env.MOCK_RESPONSES || '[]');
globalThis.fetch = async (input) => {
  const target = typeof input === 'string' ? input : input.toString();
  const match = responses.find((r) => target.includes(r.match));
  if (!match) {
    throw new Error('Unexpected fetch (no recorded response): ' + target);
  }
  const bodyStr = typeof match.body === 'string' ? match.body : JSON.stringify(match.body);
  return {
    ok: match.status >= 200 && match.status < 300,
    status: match.status,
    json: async () => JSON.parse(bodyStr),
    text: async () => bodyStr,
  };
};
`,
  );
  return {tmpDir, mockPath};
}

function runWithMock(scriptPath, {cwd, mockPath, responses, env = {}}) {
  return spawnSync(process.execPath, ['--import', url.pathToFileURL(mockPath).href, scriptPath], {
    cwd,
    encoding: 'utf8',
    env: {
      ...process.env,
      ...env,
      MOCK_RESPONSES: JSON.stringify(responses),
    },
  });
}

test('update-versions.mjs updates versions.json from releases and tags fallback', () => {
  const {tmpDir, mockPath} = makeTempEnv();
  try {
    const tmpScripts = path.join(tmpDir, 'scripts');
    const tmpCurriculum = path.join(tmpDir, 'curriculum');
    fs.mkdirSync(tmpScripts, {recursive: true});
    fs.mkdirSync(tmpCurriculum, {recursive: true});
    const copiedScript = path.join(tmpScripts, 'update-versions.mjs');
    fs.copyFileSync(versionsScript, copiedScript);

    const initialVersions = {
      track: {
        toolA: {label: 'Tool A', repo: 'org/tool-a'},
        toolB: {label: 'Tool B', repo: 'org/tool-b'},
      },
      versions: {
        toolA: {version: 'v1.0.0', publishedAt: '2025-01-01'},
      },
    };
    const versionsFile = path.join(tmpCurriculum, 'versions.json');
    fs.writeFileSync(versionsFile, JSON.stringify(initialVersions, null, 2) + '\n');

    const res = runWithMock(copiedScript, {
      cwd: tmpDir,
      mockPath,
      responses: [
        {
          match: '/repos/org/tool-a/releases/latest',
          status: 200,
          body: {tag_name: 'v1.1.0', published_at: '2026-01-15T12:00:00Z'},
        },
        {
          match: '/repos/org/tool-b/releases/latest',
          status: 404,
          body: {message: 'Not Found'},
        },
        {
          match: '/repos/org/tool-b/tags?per_page=1',
          status: 200,
          body: [{name: 'v2.0.0'}],
        },
      ],
    });

    assert.equal(res.status, 0, res.stderr);
    const updated = JSON.parse(fs.readFileSync(versionsFile, 'utf8'));
    assert.deepEqual(updated.versions.toolA, {version: 'v1.1.0', publishedAt: '2026-01-15'});
    assert.deepEqual(updated.versions.toolB, {version: 'v2.0.0', publishedAt: ''});
    assert.ok(updated.updatedAt);
  } finally {
    fs.rmSync(tmpDir, {recursive: true, force: true});
  }
});

test('update-versions.mjs throws on release without tag_name and leaves file unchanged', () => {
  const {tmpDir, mockPath} = makeTempEnv();
  try {
    const tmpScripts = path.join(tmpDir, 'scripts');
    const tmpCurriculum = path.join(tmpDir, 'curriculum');
    fs.mkdirSync(tmpScripts, {recursive: true});
    fs.mkdirSync(tmpCurriculum, {recursive: true});
    const copiedScript = path.join(tmpScripts, 'update-versions.mjs');
    fs.copyFileSync(versionsScript, copiedScript);

    const initialRaw =
      JSON.stringify(
        {
          track: {toolA: {label: 'Tool A', repo: 'org/tool-a'}},
          versions: {toolA: {version: 'v1.0.0', publishedAt: '2025-01-01'}},
        },
        null,
        2,
      ) + '\n';
    const versionsFile = path.join(tmpCurriculum, 'versions.json');
    fs.writeFileSync(versionsFile, initialRaw);

    const res = runWithMock(copiedScript, {
      cwd: tmpDir,
      mockPath,
      responses: [
        {
          match: '/repos/org/tool-a/releases/latest',
          status: 200,
          body: {published_at: '2026-01-15T12:00:00Z'},
        },
      ],
    });

    assert.equal(res.status, 1);
    assert.match(res.stderr, /release missing tag_name/);
    assert.equal(fs.readFileSync(versionsFile, 'utf8'), initialRaw);
  } finally {
    fs.rmSync(tmpDir, {recursive: true, force: true});
  }
});

test('update-versions.mjs fails on 404 with empty tags and leaves file unchanged', () => {
  const {tmpDir, mockPath} = makeTempEnv();
  try {
    const tmpScripts = path.join(tmpDir, 'scripts');
    const tmpCurriculum = path.join(tmpDir, 'curriculum');
    fs.mkdirSync(tmpScripts, {recursive: true});
    fs.mkdirSync(tmpCurriculum, {recursive: true});
    const copiedScript = path.join(tmpScripts, 'update-versions.mjs');
    fs.copyFileSync(versionsScript, copiedScript);

    const initialRaw =
      JSON.stringify(
        {
          track: {toolA: {label: 'Tool A', repo: 'org/tool-a'}},
          versions: {toolA: {version: 'v1.0.0', publishedAt: '2025-01-01'}},
        },
        null,
        2,
      ) + '\n';
    const versionsFile = path.join(tmpCurriculum, 'versions.json');
    fs.writeFileSync(versionsFile, initialRaw);

    const res = runWithMock(copiedScript, {
      cwd: tmpDir,
      mockPath,
      responses: [
        {match: '/repos/org/tool-a/releases/latest', status: 404, body: {message: 'Not Found'}},
        {match: '/repos/org/tool-a/tags?per_page=1', status: 200, body: []},
      ],
    });

    assert.equal(res.status, 1);
    assert.match(res.stderr, /no releases or tags/);
    assert.equal(fs.readFileSync(versionsFile, 'utf8'), initialRaw);
  } finally {
    fs.rmSync(tmpDir, {recursive: true, force: true});
  }
});

test('update-visitor-stats.mjs writes badges and charts from GoatCounter response', () => {
  const {tmpDir, mockPath} = makeTempEnv();
  try {
    const res = runWithMock(visitorStatsScript, {
      cwd: tmpDir,
      mockPath,
      env: {
        GOATCOUNTER_SITE_CODE: 'testsite',
        GOATCOUNTER_API_TOKEN: 'secret-token',
      },
      responses: [
        {
          match: '/api/v0/stats/total',
          status: 200,
          body: {
            total: 42,
            stats: [
              {day: '2026-01-01', daily: 10},
              {day: '2026-01-02', daily: 32},
            ],
          },
        },
      ],
    });

    assert.equal(res.status, 0, res.stderr);
    const statsDir = path.join(tmpDir, '.github', 'stats');
    const today = JSON.parse(fs.readFileSync(path.join(statsDir, 'visitors-today.json'), 'utf8'));
    const total = JSON.parse(fs.readFileSync(path.join(statsDir, 'visitors-total.json'), 'utf8'));
    assert.equal(today.message, '32');
    assert.equal(total.message, '42');
    assert.match(fs.readFileSync(path.join(statsDir, 'chart-light.svg'), 'utf8'), /<svg /);
    assert.match(fs.readFileSync(path.join(statsDir, 'chart-dark.svg'), 'utf8'), /<svg /);
  } finally {
    fs.rmSync(tmpDir, {recursive: true, force: true});
  }
});

test('update-visitor-stats.mjs leaves existing files unchanged on GoatCounter 500 error', () => {
  const {tmpDir, mockPath} = makeTempEnv();
  try {
    const statsDir = path.join(tmpDir, '.github', 'stats');
    fs.mkdirSync(statsDir, {recursive: true});
    const sentinel = '{"schemaVersion":1,"label":"Total","message":"99","color":"blue"}\n';
    const totalFile = path.join(statsDir, 'visitors-total.json');
    fs.writeFileSync(totalFile, sentinel);

    const res = runWithMock(visitorStatsScript, {
      cwd: tmpDir,
      mockPath,
      env: {
        GOATCOUNTER_SITE_CODE: 'testsite',
        GOATCOUNTER_API_TOKEN: 'secret-token',
      },
      responses: [
        {
          match: '/api/v0/stats/total',
          status: 500,
          body: 'Internal Server Error',
        },
      ],
    });

    assert.notEqual(res.status, 0);
    assert.match(res.stderr, /GoatCounter API error 500/);
    assert.equal(fs.readFileSync(totalFile, 'utf8'), sentinel);
  } finally {
    fs.rmSync(tmpDir, {recursive: true, force: true});
  }
});
