/**
 * /progress — cross-page reading-progress dashboard.
 *
 * Purely client-side. Reads localStorage entries written by Captain's Bridge
 * (website/src/components/CaptainsBridge/useReadProgress.ts) and joins them
 * with the full set of docs exposed by Docusaurus's useAllDocsData().
 *
 * SSR renders an empty skeleton; the real data fills in on the client. This
 * avoids hydration mismatches (localStorage is not available on the server).
 */
import React, {useEffect, useMemo, useState} from 'react';
import Layout from '@theme/Layout';
import BrowserOnly from '@docusaurus/BrowserOnly';
import Link from '@docusaurus/Link';
import {useAllDocsData} from '@docusaurus/plugin-content-docs/client';

import ProgressHero from '@site/src/components/Progress/ProgressHero';
import ProgressByTool, {
  type DocSummary,
} from '@site/src/components/Progress/ProgressByTool';
import ProgressControls from '@site/src/components/Progress/ProgressControls';
import {
  getAllReadEntries,
  type ReadEntry,
} from '@site/src/components/Progress/readProgress';
import styles from '@site/src/components/Progress/styles.module.css';

function flattenDocs(allDocsData: ReturnType<typeof useAllDocsData>): DocSummary[] {
  const out: DocSummary[] = [];
  for (const pluginId of Object.keys(allDocsData)) {
    const plugin = allDocsData[pluginId];
    if (!plugin) continue;
    const versions = plugin.versions ?? [];
    for (const version of versions) {
      for (const doc of version.docs ?? []) {
        // Docusaurus doc shape: {id, path, sidebar}
        if (!doc || typeof doc.path !== 'string') continue;
        out.push({
          pathname: doc.path,
          title:
            // @ts-expect-error - Docusaurus exposes sidebar label via id heuristics;
            // prefer the prettier segment if provided, else derive from path.
            doc.label || humanizePath(doc.id || doc.path),
        });
      }
    }
  }
  return out;
}

function humanizePath(idOrPath: string): string {
  const last = idOrPath.split('/').filter(Boolean).pop() || idOrPath;
  return last
    .replace(/^index$/, 'Overview')
    .replace(/-/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function ProgressPageInner(): JSX.Element {
  const allDocsData = useAllDocsData();
  const allDocs = useMemo(() => flattenDocs(allDocsData), [allDocsData]);

  const [entries, setEntries] = useState<ReadEntry[]>([]);
  const [tick, setTick] = useState(0);

  const refresh = () => setTick((n) => n + 1);

  useEffect(() => {
    setEntries(getAllReadEntries());
  }, [tick]);

  // React to progress changes from Bridge (same tab) and other tabs
  useEffect(() => {
    const handler = () => refresh();
    window.addEventListener('cc-bridge-read-change', handler);
    window.addEventListener('storage', handler);
    return () => {
      window.removeEventListener('cc-bridge-read-change', handler);
      window.removeEventListener('storage', handler);
    };
  }, []);

  const {pagesTouched, pagesComplete, sectionsRead, lastUpdated} = useMemo(() => {
    let touched = 0;
    let complete = 0;
    let sections = 0;
    let last: number | undefined;
    for (const e of entries) {
      if (e.readCount > 0) touched += 1;
      if (e.isComplete) complete += 1;
      sections += e.readCount;
      if (e.lastUpdated && (!last || e.lastUpdated > last)) last = e.lastUpdated;
    }
    return {
      pagesTouched: touched,
      pagesComplete: complete,
      sectionsRead: sections,
      lastUpdated: last,
    };
  }, [entries]);

  const hasEntries = entries.length > 0;

  return (
    <main className={styles.page}>
      <ProgressHero
        pagesTouched={pagesTouched}
        pagesComplete={pagesComplete}
        sectionsRead={sectionsRead}
        lastUpdated={lastUpdated}
      />

      {!hasEntries && (
        <section className={styles.emptyState}>
          <h2>No progress yet — start somewhere 🚀</h2>
          <p>
            CloudCaptain's Captain's Bridge reading mode tracks what you've read on each doc page.
            Open any learning path or tool doc and use the <kbd>m</kbd> key or the{' '}
            <em>"Mark section complete"</em> buttons to see your progress build up here.
          </p>
          <div className={styles.emptyActions}>
            <Link className={`button button--primary button--lg`} to="/docs/learning-paths/welcome">
              📚 Browse Learning Paths
            </Link>
            <Link className={`button button--secondary button--lg`} to="/docs/tools/docker/">
              🐳 Start with Docker
            </Link>
          </div>
          <p className={styles.emptyHint}>
            Want to see what this page looks like populated? Use <strong>Seed demo data</strong>{' '}
            below.
          </p>
        </section>
      )}

      <ProgressByTool allDocs={allDocs} entries={entries} />

      <ProgressControls onChange={refresh} hasEntries={hasEntries} />
    </main>
  );
}

export default function ProgressPage(): JSX.Element {
  return (
    <Layout
      title="Your Progress"
      description="Track your cloud, DevOps, and AI-ops learning progress across all CloudCaptain docs. Privately stored in your browser.">
      <BrowserOnly
        fallback={
          <main className={styles.page}>
            <section className={styles.hero}>
              <h1 className={styles.heroTitle}>📖 Your Progress</h1>
              <p className={styles.heroSubtitle}>Loading…</p>
            </section>
          </main>
        }>
        {() => <ProgressPageInner />}
      </BrowserOnly>
    </Layout>
  );
}
