import React, {useEffect, useMemo, useState} from 'react';
import Link from '@docusaurus/Link';
import type {ReadEntry} from '../Progress/readProgress';
import {allQuizRecords, CHANGE_EVENT, type QuizRecord} from './storage';
import {computeCurriculumProgress, type LessonProgress} from './lessonProgress';
import styles from './styles.module.css';

function scoreText(lp: LessonProgress): string {
  const s = lp.quiz?.scores;
  if (!s) return 'no quiz yet';
  const part = (k: 'pre' | 'check' | 'post') => (s[k] ? `${s[k]![0]}/${s[k]![1]}` : '–');
  return `quiz ${part('pre')} · ${part('check')} · ${part('post')}`;
}

export default function CurriculumProgress({entries}: {entries: ReadEntry[]}): React.JSX.Element | null {
  const [quizzes, setQuizzes] = useState<Record<string, QuizRecord>>({});
  const [open, setOpen] = useState<Set<string>>(new Set());

  useEffect(() => {
    const refresh = () => setQuizzes(allQuizRecords());
    refresh();
    window.addEventListener(CHANGE_EVENT, refresh);
    window.addEventListener('storage', refresh);
    return () => {
      window.removeEventListener(CHANGE_EVENT, refresh);
      window.removeEventListener('storage', refresh);
    };
  }, []);

  const summary = useMemo(() => computeCurriculumProgress(entries, quizzes), [entries, quizzes]);
  if (summary.total === 0) return null;

  const toggle = (slug: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(slug)) next.delete(slug);
      else next.add(slug);
      return next;
    });

  return (
    <section className={styles.progressSection} aria-label="Curriculum progress">
      <div className={styles.progressHeader}>
        <h2 className={styles.progressTitle}>🎓 Curriculum</h2>
        <span className={styles.muted}>
          {summary.complete} / {summary.total} lessons complete
        </span>
      </div>

      {summary.next ? (
        <Link to={summary.next.lesson.route} className={styles.continueCard}>
          <div className={styles.cardKicker}>{summary.next.status === 'in-progress' ? 'Continue' : 'Up next'}</div>
          <div className={styles.cardTitle}>{summary.next.lesson.title}</div>
          <div className={styles.muted}>
            Phase {summary.next.lesson.phaseNumber} · {summary.next.lesson.phaseTitle} · {summary.next.lesson.type} · {summary.next.lesson.minutes} min
            {summary.next.totalSections ? ` · ${summary.next.readCount}/${summary.next.totalSections} sections read` : ''}
          </div>
        </Link>
      ) : (
        <div className={styles.panel}>
          <div className={styles.panelTitle}>🏁 Every published lesson is complete</div>
          <div className={styles.muted}>New lessons land weekly; see the roadmap for what is next.</div>
        </div>
      )}

      <div className={styles.phaseList}>
        {summary.phases.map((pp) => {
          const isOpen = open.has(pp.phase.slug);
          return (
            <article key={pp.phase.slug} className={styles.phaseRow}>
              <button type="button" className={styles.phaseHeader} onClick={() => toggle(pp.phase.slug)} aria-expanded={isOpen}>
                <span className={styles.phaseIcon} aria-hidden="true">{pp.phase.icon || '📘'}</span>
                <span className={styles.phaseMeta}>
                  <span className={styles.phaseLabel}>
                    {pp.phase.number} · {pp.phase.title}
                  </span>
                  <span className={styles.muted}>
                    {pp.complete} / {pp.total} complete
                  </span>
                </span>
                <span className={styles.phaseBar} aria-hidden="true">
                  <span className={styles.phaseBarFill} style={{width: `${pp.percent}%`}} />
                </span>
                <span className={styles.muted} aria-hidden="true">{isOpen ? '▾' : '▸'}</span>
              </button>
              {isOpen && (
                <ul className={styles.lessonList}>
                  {pp.lessons.map((lp) => (
                    <li key={lp.lesson.id} className={styles.lessonRow} data-status={lp.status}>
                      <span className={styles.lessonDot} aria-hidden="true">
                        {lp.status === 'complete' ? '●' : lp.status === 'in-progress' ? '◐' : '○'}
                      </span>
                      <span className={styles.badge} data-type={lp.lesson.type}>{lp.lesson.type}</span>
                      <Link to={lp.lesson.route}>{lp.lesson.title}</Link>
                      <span className={styles.muted}>
                        {lp.totalSections ? `${lp.readCount}/${lp.totalSections} read · ` : ''}
                        {scoreText(lp)}
                      </span>
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
