/**
 * ResumeReadingWidget — homepage banner that surfaces the user's top 3
 * in-progress docs. Renders nothing when there's no read-progress data,
 * so first-time visitors see a clean hero.
 *
 * Client-only: gated by BrowserOnly in the parent to avoid SSR flash
 * (localStorage is unavailable on the server).
 */
import React, {useEffect, useState} from 'react';
import Link from '@docusaurus/Link';
import {useAllDocsData} from '@docusaurus/plugin-content-docs/client';
import {getAllReadEntries, type ReadEntry} from './readProgress';
import styles from './resumeWidget.module.css';

type EnrichedEntry = ReadEntry & {
  title: string;
  percent: number;
};

function relativeShort(ts: number | undefined): string {
  if (!ts) return '';
  const diff = Date.now() - ts;
  if (diff < 60_000) return 'just now';
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)}d ago`;
  return `${Math.floor(diff / (7 * 86_400_000))}w ago`;
}

function humanizePath(path: string): string {
  const last = path.split('/').filter(Boolean).pop() ?? path;
  return last
    .replace(/^index$/, 'Overview')
    .replace(/-/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export default function ResumeReadingWidget(): JSX.Element | null {
  const allDocsData = useAllDocsData();
  const [entries, setEntries] = useState<ReadEntry[]>([]);

  useEffect(() => {
    const refresh = () => setEntries(getAllReadEntries());
    refresh();
    window.addEventListener('cc-bridge-read-change', refresh);
    window.addEventListener('storage', refresh);
    return () => {
      window.removeEventListener('cc-bridge-read-change', refresh);
      window.removeEventListener('storage', refresh);
    };
  }, []);

  const topInProgress = React.useMemo<EnrichedEntry[]>(() => {
    // Build title index from all indexed docs
    const titleByPath = new Map<string, string>();
    for (const pluginId of Object.keys(allDocsData)) {
      const plugin = allDocsData[pluginId];
      for (const version of plugin?.versions ?? []) {
        for (const doc of version.docs ?? []) {
          if (!doc || typeof doc.path !== 'string') continue;
          const label =
            // @ts-expect-error - Docusaurus may expose a label on doc; fall through otherwise
            doc.label || humanizePath(doc.id || doc.path);
          titleByPath.set(doc.path, label);
          titleByPath.set(doc.path.replace(/\/$/, ''), label);
          titleByPath.set(doc.path.replace(/\/$/, '') + '/', label);
        }
      }
    }

    return entries
      .filter((e) => e.readCount > 0 && !e.isComplete)
      .map((e) => {
        const total = e.totalSections && e.totalSections > 0 ? e.totalSections : e.readCount;
        const percent = Math.min(100, Math.round((e.readCount / total) * 100));
        const title =
          titleByPath.get(e.pathname) ??
          titleByPath.get(e.pathname.replace(/\/$/, '')) ??
          humanizePath(e.pathname);
        return {...e, title, percent};
      })
      .sort((a, b) => (b.lastUpdated ?? 0) - (a.lastUpdated ?? 0))
      .slice(0, 3);
  }, [entries, allDocsData]);

  if (topInProgress.length === 0) return null;

  return (
    <section className={styles.widget} aria-label="Resume reading">
      <div className={styles.widgetInner}>
        <header className={styles.widgetHeader}>
          <div>
            <span className={styles.widgetKicker}>⚓ CAPTAIN'S LOG</span>
            <h2 className={styles.widgetTitle}>Resume where you left off</h2>
          </div>
          <Link to="/progress" className={styles.widgetAll}>
            View all progress →
          </Link>
        </header>

        <ul className={styles.resumeList}>
          {topInProgress.map((e) => (
            <li key={e.pathname} className={styles.resumeItem}>
              <Link to={e.pathname} className={styles.resumeLink}>
                <div className={styles.resumeTopRow}>
                  <span className={styles.resumeTitle}>{e.title}</span>
                  <span className={styles.resumeMeta}>
                    {e.readCount}
                    {e.totalSections ? ` / ${e.totalSections}` : ''} · {relativeShort(e.lastUpdated)}
                  </span>
                </div>
                <div className={styles.resumeBar} aria-hidden="true">
                  <div className={styles.resumeBarFill} style={{width: `${e.percent}%`}} />
                </div>
                <div className={styles.resumeBottomRow}>
                  <span className={styles.resumePath}>{e.pathname}</span>
                  <span className={styles.resumeContinue}>Continue →</span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
