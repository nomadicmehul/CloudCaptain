import React from 'react';
import Link from '@docusaurus/Link';
import {useLocation} from '@docusaurus/router';
import {backlinksForPath, isCurriculumPath} from './curriculumTitles';
import styles from './styles.module.css';

/**
 * On Reference docs that a lesson builds on, point readers at the lesson.
 * Renders nothing on curriculum pages and on docs without backlinks.
 */
export default function FromScratchCard(): React.JSX.Element | null {
  const {pathname} = useLocation();
  if (isCurriculumPath(pathname)) return null;
  const lessons = backlinksForPath(pathname);
  if (lessons.length === 0) return null;
  return (
    <aside className={styles.panel} aria-label="Build this from scratch">
      <div className={styles.panelTitle}>🧪 Build this from scratch</div>
      <ul className={styles.list}>
        {lessons.map((l) => (
          <li key={l.id}>
            <span className={styles.badge} data-type={l.type}>{l.type}</span>{' '}
            <Link to={l.route}>{l.title}</Link>
            <span className={styles.muted}> · Phase {l.phaseNumber} · {l.minutes} min</span>
          </li>
        ))}
      </ul>
    </aside>
  );
}
