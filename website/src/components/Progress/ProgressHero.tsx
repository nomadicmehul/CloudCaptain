import React from 'react';
import styles from './styles.module.css';

type Props = {
  pagesTouched: number;
  pagesComplete: number;
  sectionsRead: number;
  lastUpdated: number | undefined;
};

function relativeTime(ts: number | undefined): string {
  if (!ts) return 'never';
  const diff = Date.now() - ts;
  if (diff < 60_000) return 'just now';
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} min ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} h ago`;
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} d ago`;
  return new Date(ts).toLocaleDateString();
}

export default function ProgressHero({
  pagesTouched,
  pagesComplete,
  sectionsRead,
  lastUpdated,
}: Props): JSX.Element {
  return (
    <section className={styles.hero} aria-label="Progress summary">
      <div className={styles.heroTitleRow}>
        <h1 className={styles.heroTitle}>
          <span className={styles.heroEmoji}>📖</span> Your Progress
        </h1>
        <span className={styles.heroSubtitle}>
          Tracked privately in this browser via Captain's Bridge reading mode.
        </span>
      </div>

      <div className={styles.heroStats}>
        <div className={styles.statCard}>
          <span className={styles.statLabel}>Sections read</span>
          <span className={styles.statValue}>{sectionsRead}</span>
          <span className={styles.statFoot}>across your learning journey</span>
        </div>
        <div className={styles.statCard}>
          <span className={styles.statLabel}>Pages touched</span>
          <span className={styles.statValue}>{pagesTouched}</span>
          <span className={styles.statFoot}>started but not complete</span>
        </div>
        <div className={styles.statCard}>
          <span className={styles.statLabel}>Pages complete</span>
          <span className={styles.statValue}>{pagesComplete}</span>
          <span className={styles.statFoot}>🏁 every section marked</span>
        </div>
        <div className={styles.statCard}>
          <span className={styles.statLabel}>Last active</span>
          <span className={styles.statValueSmall}>{relativeTime(lastUpdated)}</span>
          <span className={styles.statFoot}>
            {lastUpdated ? new Date(lastUpdated).toLocaleString() : '—'}
          </span>
        </div>
      </div>
    </section>
  );
}
