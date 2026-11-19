// Preload mock for fetch and fs to allow testing update scripts without network or real files.
import fs from 'node:fs';
import path from 'node:path';

if (process.env.MOCK_FETCH === '1') {
  const responses = JSON.parse(process.env.MOCK_RESPONSES || '{}');
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts) => {
    const u = typeof url === 'string' ? url : url.toString();
    const key = Object.keys(responses).find((k) => u.includes(k) || u.endsWith(k));
    if (key && responses[key]) {
      const r = responses[key];
      return {
        ok: r.ok !== false,
        status: r.status || (r.ok === false ? 404 : 200),
        json: async () => r.json || {},
        text: async () => r.text || JSON.stringify(r.json || {}),
      };
    }
    // Fallback to real if not mocked (shouldn't happen in tests)
    return originalFetch(url, opts);
  };

  // Mock fs for the versions file if TEST_VERSIONS_FILE is set
  if (process.env.TEST_VERSIONS_FILE) {
    const realFile = process.env.TEST_VERSIONS_FILE;
    const originalRead = fs.readFileSync;
    const originalWrite = fs.writeFileSync;
    fs.readFileSync = (p, ...args) => {
      if (String(p).endsWith('versions.json')) return originalRead(realFile, ...args);
      return originalRead(p, ...args);
    };
    fs.writeFileSync = (p, data, ...args) => {
      if (String(p).endsWith('versions.json')) return originalWrite(realFile, data, ...args);
      return originalWrite(p, data, ...args);
    };
  }

  // For visitor stats, mock writeFile and mkdir if TEST_OUT_DIR
  if (process.env.TEST_OUT_DIR) {
    const outDir = process.env.TEST_OUT_DIR;
    const originalMkdir = fs.promises.mkdir;
    const originalWriteFile = fs.promises.writeFile;
    fs.promises.mkdir = async (p, opts) => {
      if (String(p).includes('.github/stats') || String(p).endsWith('stats')) {
        return originalMkdir(outDir, opts);
      }
      return originalMkdir(p, opts);
    };
    fs.promises.writeFile = async (p, data, opts) => {
      const base = path.basename(String(p));
      const target = path.join(outDir, base);
      return originalWriteFile(target, data, opts);
    };
  }
}
