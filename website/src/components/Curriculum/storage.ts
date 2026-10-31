/**
 * Quiz results live next to Captain's Bridge reading progress in localStorage:
 *   key = `cloudcaptain.quiz.{pathname}`
 * and fire the same `cc-bridge-read-change` event so the sidebar spine,
 * /progress and the resume widget refresh without extra plumbing.
 */
import type {QuizStage} from './types';

export const QUIZ_PREFIX = 'cloudcaptain.quiz.';
export const CHANGE_EVENT = 'cc-bridge-read-change';

export type QuizRecord = {
  answers: Record<string, number>;
  scores: Partial<Record<QuizStage, [number, number]>>;
  attempts: number;
  passedAt?: number;
  lastUpdated: number;
};

export function quizKey(pathname: string): string {
  return QUIZ_PREFIX + pathname.replace(/\/+$/, '');
}

export function readQuizRecord(pathname: string): QuizRecord | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.localStorage.getItem(quizKey(pathname));
    return raw ? (JSON.parse(raw) as QuizRecord) : null;
  } catch {
    return null;
  }
}

export function writeQuizRecord(pathname: string, record: QuizRecord): void {
  if (typeof window === 'undefined') return;
  try {
    const key = quizKey(pathname);
    window.localStorage.setItem(key, JSON.stringify(record));
    window.dispatchEvent(new CustomEvent(CHANGE_EVENT, {detail: {key, next: record}}));
  } catch {
    /* private mode or storage disabled: the quiz still works, it just does not persist */
  }
}

export function allQuizRecords(): Record<string, QuizRecord> {
  const out: Record<string, QuizRecord> = {};
  if (typeof window === 'undefined') return out;
  try {
    for (let i = 0; i < window.localStorage.length; i++) {
      const key = window.localStorage.key(i);
      if (key && key.startsWith(QUIZ_PREFIX)) {
        const raw = window.localStorage.getItem(key);
        if (raw) out[key.slice(QUIZ_PREFIX.length)] = JSON.parse(raw) as QuizRecord;
      }
    }
  } catch {
    /* ignore */
  }
  return out;
}
