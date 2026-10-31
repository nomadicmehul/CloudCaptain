import React, {useEffect, useMemo, useState} from 'react';
import {useLocation} from '@docusaurus/router';
import {useLessonFrontMatter} from './useLesson';
import {readQuizRecord, writeQuizRecord} from './storage';
import type {QuizQuestion, QuizStage} from './types';
import styles from './styles.module.css';

const LABELS: Record<QuizStage, {title: string; hint: string}> = {
  pre: {title: 'Before you start', hint: 'One question to find out what you already know. No score pressure.'},
  check: {title: 'Check your understanding', hint: 'Three questions on what you just built and used.'},
  post: {title: 'Lock it in', hint: 'Two questions that connect this lesson to the next one. Both right marks the lesson complete.'},
};

type Props = {stage?: QuizStage; questions?: QuizQuestion[]};

export default function Quiz({stage = 'check', questions: explicit}: Props): React.JSX.Element | null {
  const {quiz} = useLessonFrontMatter();
  const {pathname} = useLocation();
  const questions = useMemo(
    () => (explicit || quiz?.questions || []).filter((q) => q.stage === stage),
    [explicit, quiz, stage],
  );
  const [picked, setPicked] = useState<Record<string, number>>({});
  const [submitted, setSubmitted] = useState(false);

  useEffect(() => {
    const record = readQuizRecord(pathname);
    if (record && record.scores[stage]) {
      const previous: Record<string, number> = {};
      for (const q of questions) if (q.id in record.answers) previous[q.id] = record.answers[q.id];
      setPicked(previous);
      setSubmitted(Object.keys(previous).length === questions.length && questions.length > 0);
    }
  }, [pathname, stage, questions]);

  if (questions.length === 0) return null;

  const correct = questions.filter((q) => picked[q.id] === q.answer).length;
  const answered = questions.every((q) => picked[q.id] !== undefined);

  const submit = () => {
    setSubmitted(true);
    const previous = readQuizRecord(pathname) || {answers: {}, scores: {}, attempts: 0, lastUpdated: 0};
    const perfect = correct === questions.length;
    writeQuizRecord(pathname, {
      ...previous,
      answers: {...previous.answers, ...picked},
      scores: {...previous.scores, [stage]: [correct, questions.length]},
      attempts: previous.attempts + 1,
      lastUpdated: Date.now(),
      passedAt: stage === 'post' && perfect ? Date.now() : previous.passedAt,
    });
  };

  const retry = () => {
    setPicked({});
    setSubmitted(false);
  };

  return (
    <section className={styles.quiz} data-stage={stage} aria-label={LABELS[stage].title}>
      <header className={styles.quizHeader}>
        <span className={styles.quizKicker}>Quiz · {stage}</span>
        <h4 className={styles.quizTitle}>{LABELS[stage].title}</h4>
        <p className={styles.quizHint}>{LABELS[stage].hint}</p>
      </header>

      {questions.map((q, qi) => {
        const chosen = picked[q.id];
        const isRight = chosen === q.answer;
        return (
          <fieldset key={q.id} className={styles.question} disabled={submitted}>
            <legend className={styles.prompt}>
              <span className={styles.qNumber}>{qi + 1}</span> {q.prompt}
            </legend>
            {q.choices.map((choice, ci) => {
              const state = !submitted ? '' : ci === q.answer ? 'right' : ci === chosen ? 'wrong' : '';
              return (
                <label key={ci} className={styles.choice} data-state={state}>
                  <input
                    type="radio"
                    name={`${pathname}-${q.id}`}
                    value={ci}
                    checked={chosen === ci}
                    onChange={() => setPicked((p) => ({...p, [q.id]: ci}))}
                  />
                  <span>{choice}</span>
                </label>
              );
            })}
            {submitted && (
              <p className={styles.explanation} data-state={isRight ? 'right' : 'wrong'}>
                <strong>{isRight ? 'Correct.' : 'Not quite.'}</strong> {q.explanation}
              </p>
            )}
          </fieldset>
        );
      })}

      <footer className={styles.quizFooter}>
        {!submitted ? (
          <button type="button" className="button button--primary button--sm" disabled={!answered} onClick={submit}>
            Check answer{questions.length > 1 ? 's' : ''}
          </button>
        ) : (
          <>
            <span className={styles.score} data-state={correct === questions.length ? 'right' : 'wrong'}>
              {correct} / {questions.length} correct
            </span>
            <button type="button" className="button button--secondary button--sm" onClick={retry}>
              Try again
            </button>
          </>
        )}
      </footer>
    </section>
  );
}
