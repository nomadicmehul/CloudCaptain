import React, {useMemo, useState} from 'react';
import Link from '@docusaurus/Link';
import styles from './styles.module.css';
import {CATEGORIES, classifyDoc} from './groupDocs';
import type {ReadEntry} from './readProgress';

export type DocSummary = {
  pathname: string;
  title: string;
};

type Props = {
  allDocs: DocSummary[];
  entries: ReadEntry[];
};

type Group = {
  id: string;
  label: string;
  icon: string;
  order: number;
  docs: Array<{
    pathname: string;
    title: string;
    entry: ReadEntry | undefined;
    status: 'unread' | 'partial' | 'complete';
  }>;
  pagesTotal: number;
  pagesTouched: number;
  pagesComplete: number;
};

export default function ProgressByTool({allDocs, entries}: Props): JSX.Element {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const entriesByPath = useMemo(() => {
    const m = new Map<string, ReadEntry>();
    for (const e of entries) {
      m.set(e.pathname, e);
      // Also register normalized variants so sidebar links and doc paths match
      m.set(e.pathname.replace(/\/$/, ''), e);
      m.set(e.pathname.replace(/\/$/, '') + '/', e);
    }
    return m;
  }, [entries]);

  const groups = useMemo<Group[]>(() => {
    const map = new Map<string, Group>();
    for (const cat of CATEGORIES) {
      map.set(cat.id, {
        id: cat.id,
        label: cat.label,
        icon: cat.icon,
        order: cat.order,
        docs: [],
        pagesTotal: 0,
        pagesTouched: 0,
        pagesComplete: 0,
      });
    }

    for (const doc of allDocs) {
      const cat = classifyDoc(doc.pathname);
      if (!cat) continue;
      const entry =
        entriesByPath.get(doc.pathname) ??
        entriesByPath.get(doc.pathname.replace(/\/$/, '')) ??
        entriesByPath.get(doc.pathname.replace(/\/$/, '') + '/');
      const status: 'unread' | 'partial' | 'complete' = !entry
        ? 'unread'
        : entry.isComplete
          ? 'complete'
          : 'partial';
      const g = map.get(cat.id)!;
      g.docs.push({pathname: doc.pathname, title: doc.title, entry, status});
      g.pagesTotal += 1;
      if (entry) g.pagesTouched += 1;
      if (status === 'complete') g.pagesComplete += 1;
    }

    return Array.from(map.values())
      .filter((g) => g.pagesTotal > 0)
      .sort((a, b) => a.order - b.order);
  }, [allDocs, entriesByPath]);

  const toggle = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  if (groups.length === 0) {
    return (
      <section className={styles.emptyState}>
        <h2>No docs indexed</h2>
        <p>The Docusaurus docs plugin isn't reporting any pages. Check your build.</p>
      </section>
    );
  }

  return (
    <section className={styles.byTool} aria-label="Progress by tool and path">
      <h2 className={styles.sectionHeading}>By Category</h2>
      <div className={styles.groupList}>
        {groups.map((g) => {
          const pct = g.pagesTotal > 0 ? (g.pagesComplete / g.pagesTotal) * 100 : 0;
          const touchedPct = g.pagesTotal > 0 ? (g.pagesTouched / g.pagesTotal) * 100 : 0;
          const isOpen = expanded.has(g.id);
          return (
            <article
              key={g.id}
              className={`${styles.group} ${g.pagesComplete === g.pagesTotal && g.pagesTotal > 0 ? styles.groupDone : ''}`}>
              <button
                type="button"
                className={styles.groupHeader}
                onClick={() => toggle(g.id)}
                aria-expanded={isOpen}>
                <span className={styles.groupIcon} aria-hidden="true">
                  {g.icon}
                </span>
                <div className={styles.groupMeta}>
                  <span className={styles.groupLabel}>{g.label}</span>
                  <span className={styles.groupSubtitle}>
                    {g.pagesComplete} / {g.pagesTotal} pages complete
                    {g.pagesTouched > g.pagesComplete && (
                      <> · {g.pagesTouched - g.pagesComplete} in progress</>
                    )}
                  </span>
                </div>
                <div className={styles.groupBarWrap} aria-hidden="true">
                  <div className={styles.groupBarTouched} style={{width: `${touchedPct}%`}} />
                  <div className={styles.groupBarComplete} style={{width: `${pct}%`}} />
                </div>
                <span className={styles.groupChevron} aria-hidden="true">
                  {isOpen ? '▾' : '▸'}
                </span>
              </button>

              {isOpen && (
                <ul className={styles.docList}>
                  {g.docs
                    .slice()
                    .sort((a, b) => a.title.localeCompare(b.title))
                    .map((doc) => (
                      <li key={doc.pathname} className={`${styles.docRow} ${styles[`docRow_${doc.status}`]}`}>
                        <span className={styles.docStatus} aria-hidden="true">
                          {doc.status === 'complete'
                            ? '●'
                            : doc.status === 'partial'
                              ? '◐'
                              : '○'}
                        </span>
                        <Link to={doc.pathname} className={styles.docLink}>
                          {doc.title}
                        </Link>
                        {doc.entry && doc.entry.totalSections ? (
                          <span className={styles.docMeta}>
                            {doc.entry.readCount} / {doc.entry.totalSections}
                          </span>
                        ) : doc.entry ? (
                          <span className={styles.docMeta}>{doc.entry.readCount} read</span>
                        ) : (
                          <span className={styles.docMetaMuted}>not started</span>
                        )}
                      </li>
                    ))}
                </ul>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}
