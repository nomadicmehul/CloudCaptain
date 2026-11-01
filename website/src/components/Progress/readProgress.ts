/**
 * readProgress — localStorage reader for the /progress dashboard.
 *
 * Mirrors the contract written by the Captain's Bridge reader
 * (website/src/components/CaptainsBridge/useReadProgress.ts). Duplicated
 * here so the /progress page can ship independently of the Bridge branch.
 * When both are merged they share the same key schema:
 *
 *   key   = `cloudcaptain.bridge.read.{pathname}`
 *   value = JSON: { readSections: string[], totalSections?: number,
 *                   lastUpdated?: number }
 */
const STORAGE_PREFIX = 'cloudcaptain.bridge.read.';
const QUIZ_PREFIX = 'cloudcaptain.quiz.';

export type ReadEntry = {
  pathname: string;
  readSections: string[];
  readCount: number;
  totalSections?: number;
  lastUpdated?: number;
  isComplete: boolean;
};

export function getAllReadEntries(): ReadEntry[] {
  if (typeof localStorage === 'undefined') return [];
  const out: ReadEntry[] = [];
  for (let i = 0; i < localStorage.length; i += 1) {
    const k = localStorage.key(i);
    if (!k || !k.startsWith(STORAGE_PREFIX)) continue;
    const pathname = k.slice(STORAGE_PREFIX.length);
    const raw = localStorage.getItem(k);
    if (!raw) continue;
    try {
      const parsed = JSON.parse(raw);
      const readSections: string[] = Array.isArray(parsed.readSections)
        ? parsed.readSections
        : [];
      const readCount = readSections.length;
      if (readCount === 0 && !parsed.totalSections) continue;
      const totalSections: number | undefined = parsed.totalSections;
      const isComplete =
        typeof totalSections === 'number' && totalSections > 0 && readCount >= totalSections;
      out.push({
        pathname,
        readSections,
        readCount,
        totalSections,
        lastUpdated: parsed.lastUpdated,
        isComplete,
      });
    } catch {}
  }
  return out;
}

export function clearAllReadEntries(): number {
  if (typeof localStorage === 'undefined') return 0;
  const toRemove: string[] = [];
  for (let i = 0; i < localStorage.length; i += 1) {
    const k = localStorage.key(i);
    if (k && (k.startsWith(STORAGE_PREFIX) || k.startsWith(QUIZ_PREFIX))) toRemove.push(k);
  }
  toRemove.forEach((k) => localStorage.removeItem(k));
  window.dispatchEvent(new CustomEvent('cc-bridge-read-change'));
  return toRemove.length;
}

export type ExportPayload = {
  version: 1 | 2;
  exportedAt: string;
  appliedFrom: string;
  entries: Record<string, {
    readSections: string[];
    totalSections?: number;
    lastUpdated?: number;
  }>;
  /** version 2: quiz records keyed by pathname (cloudcaptain.quiz.*) */
  quizzes?: Record<string, unknown>;
};

function readQuizzes(): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (typeof localStorage === 'undefined') return out;
  for (let i = 0; i < localStorage.length; i += 1) {
    const k = localStorage.key(i);
    if (!k || !k.startsWith(QUIZ_PREFIX)) continue;
    const raw = localStorage.getItem(k);
    if (!raw) continue;
    try {
      out[k.slice(QUIZ_PREFIX.length)] = JSON.parse(raw);
    } catch {}
  }
  return out;
}

export function exportReadEntries(): ExportPayload {
  const entries = getAllReadEntries();
  const payload: ExportPayload = {
    version: 2,
    exportedAt: new Date().toISOString(),
    appliedFrom: typeof window !== 'undefined' ? window.location.origin : 'unknown',
    entries: {},
    quizzes: readQuizzes(),
  };
  for (const e of entries) {
    payload.entries[e.pathname] = {
      readSections: e.readSections,
      totalSections: e.totalSections,
      lastUpdated: e.lastUpdated,
    };
  }
  return payload;
}

export function importReadEntries(
  payload: unknown,
  mode: 'merge' | 'replace' = 'merge'
): {imported: number; skipped: number; errors: string[]} {
  const errors: string[] = [];
  if (!payload || typeof payload !== 'object') {
    return {imported: 0, skipped: 0, errors: ['Invalid payload: not an object.']};
  }
  const p = payload as ExportPayload;
  if ((p.version !== 1 && p.version !== 2) || !p.entries || typeof p.entries !== 'object') {
    return {
      imported: 0,
      skipped: 0,
      errors: ['Invalid payload: missing version (1 or 2) or entries map.'],
    };
  }
  if (mode === 'replace') clearAllReadEntries();
  let imported = 0;
  let skipped = 0;
  for (const [pathname, entry] of Object.entries(p.entries)) {
    if (!pathname.startsWith('/')) {
      skipped += 1;
      continue;
    }
    try {
      const toStore = {
        readSections: Array.isArray(entry.readSections) ? entry.readSections : [],
        totalSections: entry.totalSections,
        lastUpdated: entry.lastUpdated ?? Date.now(),
      };
      localStorage.setItem(STORAGE_PREFIX + pathname, JSON.stringify(toStore));
      imported += 1;
    } catch (e) {
      errors.push(`Failed to import ${pathname}: ${(e as Error).message}`);
      skipped += 1;
    }
  }
  if (p.quizzes && typeof p.quizzes === 'object') {
    for (const [pathname, record] of Object.entries(p.quizzes)) {
      if (!pathname.startsWith('/')) continue;
      try {
        localStorage.setItem(QUIZ_PREFIX + pathname, JSON.stringify(record));
      } catch {}
    }
  }
  window.dispatchEvent(new CustomEvent('cc-bridge-read-change'));
  return {imported, skipped, errors};
}
