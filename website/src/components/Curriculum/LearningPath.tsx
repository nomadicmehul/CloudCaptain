import React from 'react';
import Link from '@docusaurus/Link';
import curriculumJson from '@site/src/data/generated/curriculum.json';
import type {CurriculumModel} from './types';
import styles from './styles.module.css';

const curriculum = curriculumJson as unknown as CurriculumModel;

export default function LearningPath({id}: {id: string}): React.JSX.Element {
  const path = curriculum.paths.find((p) => p.id === id);
  if (!path) {
    return <p className={styles.muted}>Unknown learning path “{id}”. Check curriculum/manifest.json.</p>;
  }
  if (path.lessons.length === 0) {
    return <p className={styles.muted}>No published lessons on this path yet.</p>;
  }
  return (
    <div className={styles.panel}>
      <div className={styles.panelTitle}>
        {path.title} · {path.lessons.length} {path.lessons.length === 1 ? 'lesson' : 'lessons'} ·{' '}
        {Math.round((path.minutes / 60) * 10) / 10} h
      </div>
      <ol className={styles.pathList}>
        {path.lessons.map((l) => (
          <li key={l.id}>
            <span className={styles.badge} data-type={l.type}>{l.type}</span>{' '}
            <Link to={l.route}>{l.title}</Link>
            <span className={styles.muted}>
              {' '}
              · Phase {l.phaseNumber} {l.phaseTitle} · {l.minutes} min
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}
