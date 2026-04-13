/**
 * seedDemo — writes a small set of synthetic read-progress entries into
 * localStorage so /progress can be demoed before users actually read docs.
 *
 * Safety:
 *   - Refuses to overwrite keys that already have non-zero readCount (we
 *     never clobber real progress).
 *   - Marks seeded entries with lastUpdated = recent timestamps spread
 *     across the last 10 days so the dashboard looks realistic.
 *   - Reversible via a dedicated unseed() that removes ONLY the keys we
 *     wrote in this seed pass (tracked in a dedicated meta key).
 */
const STORAGE_PREFIX = 'cloudcaptain.bridge.read.';
const SEED_META_KEY = 'cloudcaptain.bridge.demo-seed';

type SeedEntry = {
  pathname: string;
  readSections: string[];
  totalSections: number;
  daysAgo: number;
};

const DEMO_ENTRIES: SeedEntry[] = [
  {
    pathname: '/docs/tools/docker/fundamentals',
    readSections: ['what-is-docker', 'containers-vs-virtual-machines', 'docker-architecture'],
    totalSections: 8,
    daysAgo: 0,
  },
  {
    pathname: '/docs/tools/docker/',
    readSections: ['overview'],
    totalSections: 1,
    daysAgo: 0,
  },
  {
    pathname: '/docs/tools/docker/cheatsheet',
    readSections: ['basics', 'images', 'containers', 'networking', 'volumes'],
    totalSections: 5,
    daysAgo: 2,
  },
  {
    pathname: '/docs/tools/kubernetes/fundamentals',
    readSections: ['what-is-kubernetes', 'architecture'],
    totalSections: 10,
    daysAgo: 3,
  },
  {
    pathname: '/docs/learning-paths/devops',
    readSections: ['intro', 'linux-fundamentals', 'version-control'],
    totalSections: 8,
    daysAgo: 4,
  },
  {
    pathname: '/docs/tools/terraform/fundamentals',
    readSections: ['what-is-terraform'],
    totalSections: 6,
    daysAgo: 6,
  },
  {
    pathname: '/docs/tools/linux/fundamentals',
    readSections: ['filesystem', 'permissions', 'processes', 'shell-basics'],
    totalSections: 4,
    daysAgo: 7,
  },
  {
    pathname: '/docs/tools/git/fundamentals',
    readSections: ['intro', 'basic-commands'],
    totalSections: 5,
    daysAgo: 9,
  },
];

export function seedDemoProgress(): {created: number; skipped: number} {
  if (typeof localStorage === 'undefined') return {created: 0, skipped: 0};
  let created = 0;
  let skipped = 0;
  const seededKeys: string[] = [];
  const now = Date.now();
  for (const seed of DEMO_ENTRIES) {
    const key = STORAGE_PREFIX + seed.pathname;
    try {
      const existing = localStorage.getItem(key);
      if (existing) {
        try {
          const parsed = JSON.parse(existing);
          if (Array.isArray(parsed.readSections) && parsed.readSections.length > 0) {
            skipped += 1;
            continue;
          }
        } catch {}
      }
      const lastUpdated = now - seed.daysAgo * 86_400_000 - Math.floor(Math.random() * 3_600_000);
      localStorage.setItem(
        key,
        JSON.stringify({
          readSections: seed.readSections,
          totalSections: seed.totalSections,
          lastUpdated,
        })
      );
      seededKeys.push(key);
      created += 1;
    } catch {
      skipped += 1;
    }
  }
  try {
    localStorage.setItem(SEED_META_KEY, JSON.stringify({keys: seededKeys, at: now}));
  } catch {}
  window.dispatchEvent(new CustomEvent('cc-bridge-read-change'));
  return {created, skipped};
}

export function unseedDemoProgress(): number {
  if (typeof localStorage === 'undefined') return 0;
  try {
    const raw = localStorage.getItem(SEED_META_KEY);
    if (!raw) return 0;
    const meta = JSON.parse(raw) as {keys: string[]};
    const keys = Array.isArray(meta.keys) ? meta.keys : [];
    keys.forEach((k) => localStorage.removeItem(k));
    localStorage.removeItem(SEED_META_KEY);
    window.dispatchEvent(new CustomEvent('cc-bridge-read-change'));
    return keys.length;
  } catch {
    return 0;
  }
}

export function hasDemoSeed(): boolean {
  if (typeof localStorage === 'undefined') return false;
  return localStorage.getItem(SEED_META_KEY) !== null;
}
