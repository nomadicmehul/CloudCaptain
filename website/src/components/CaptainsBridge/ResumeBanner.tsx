import React from 'react';
import styles from './styles.module.css';
import type {Section} from './useScrollProgress';

type Props = {
  section: Section | undefined;
  readCount: number;
  total: number;
};

export default function ResumeBanner({section, readCount, total}: Props): JSX.Element | null {
  if (!section) return null;
  const pct = total > 0 ? Math.round((readCount / total) * 100) : 0;
  return (
    <div className={styles.resume} role="status">
      <div className={styles.resumeIcon}>⏵</div>
      <div className={styles.resumeBody}>
        <div className={styles.resumeLine1}>
          <span className={styles.resumeLabel}>Resume where you left off</span>
          <span className={styles.resumeMeta}>
            {readCount} / {total} sections · {pct}%
          </span>
        </div>
        <a href={`#${section.id}`} className={styles.resumeLink}>
          Jump to §{section.title || section.id}
        </a>
      </div>
      <div className={styles.resumeBar} aria-hidden="true">
        <div className={styles.resumeBarFill} style={{width: `${pct}%`}} />
      </div>
    </div>
  );
}
