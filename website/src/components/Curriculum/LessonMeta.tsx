import React from 'react';
import Link from '@docusaurus/Link';
import {useLessonFrontMatter} from './useLesson';
import styles from './styles.module.css';
import {formatDuration} from './duration';

export default function LessonMeta(): React.JSX.Element | null {
  const {lesson} = useLessonFrontMatter();
  if (!lesson) return null;
  return (
    <div className={styles.meta}>
      <span className={styles.badge} data-type={lesson.type}>{lesson.type}</span>
      <span className={styles.metaItem}>⏱ {formatDuration(lesson.minutes)}</span>
      {lesson.type === 'Capstone' && lesson.milestones && lesson.pass_score !== undefined && (
        <span className={styles.metaItem}>
          {lesson.milestones.length} milestones · pass at {lesson.pass_score}/{lesson.milestones.reduce((a, m) => a + m.points, 0)} points
        </span>
      )}
      <span className={styles.metaItem}>{lesson.langs.map((l) => l[0].toUpperCase() + l.slice(1)).join(' · ')}</span>
      {lesson.tools && lesson.tools.length > 0 && <span className={styles.metaItem}>Tools: {lesson.tools.join(', ')}</span>}
      {lesson.prereqs.length > 0 && (
        <span className={styles.metaItem}>
          Prerequisites:{' '}
          {lesson.prereqs.map((id, i) => (
            <React.Fragment key={id}>
              {i > 0 && ', '}
              <Link to={`/learn/${id}`}>{id.split('/')[1]}</Link>
            </React.Fragment>
          ))}
        </span>
      )}
      {lesson.last_verified && <span className={styles.metaItem}>Verified {lesson.last_verified}</span>}
    </div>
  );
}
