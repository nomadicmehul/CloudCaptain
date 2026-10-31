/**
 * Joins the generated curriculum with Captain's Bridge read entries and quiz
 * records to answer: which lessons are done, which phase is how far along,
 * and what to do next.
 *
 * A lesson is complete when every section has been read AND the post quiz
 * was passed with full marks. Anything touched (sections read or a quiz
 * answered) is in progress.
 */
import type {ReadEntry} from '../Progress/readProgress';
import type {QuizRecord} from './storage';
import type {CurriculumLesson, CurriculumPhase} from './types';
import {curriculumModel} from './curriculumTitles';

export type LessonStatus = 'todo' | 'in-progress' | 'complete';

export type LessonProgress = {
  lesson: CurriculumLesson;
  status: LessonStatus;
  readCount: number;
  totalSections?: number;
  quiz?: QuizRecord;
  postPassed: boolean;
};

export type PhaseProgress = {
  phase: CurriculumPhase;
  lessons: LessonProgress[];
  complete: number;
  total: number;
  percent: number;
};

export type CurriculumProgressSummary = {
  phases: PhaseProgress[];
  complete: number;
  total: number;
  next: LessonProgress | null;
  byId: Record<string, LessonProgress>;
};

function norm(p: string): string {
  return p.replace(/\/+$/, '') || '/';
}

export function computeCurriculumProgress(entries: ReadEntry[], quizzes: Record<string, QuizRecord>): CurriculumProgressSummary {
  const entryByPath = new Map<string, ReadEntry>();
  for (const e of entries) entryByPath.set(norm(e.pathname), e);
  const quizByPath = new Map<string, QuizRecord>();
  for (const [p, q] of Object.entries(quizzes)) quizByPath.set(norm(p), q);

  const byId: Record<string, LessonProgress> = {};
  const phases: PhaseProgress[] = curriculumModel.phases
    .filter((phase) => phase.live)
    .map((phase) => {
      const lessons = phase.lessons.map((lesson) => {
        const entry = entryByPath.get(norm(lesson.route));
        const quiz = quizByPath.get(norm(lesson.route));
        const post = quiz?.scores?.post;
        const postPassed = Boolean(post && post[1] > 0 && post[0] === post[1]);
        const readComplete = Boolean(entry?.isComplete);
        const touched = Boolean((entry && entry.readCount > 0) || quiz);
        const status: LessonStatus = readComplete && postPassed ? 'complete' : touched ? 'in-progress' : 'todo';
        const lp: LessonProgress = {lesson, status, readCount: entry?.readCount ?? 0, totalSections: entry?.totalSections, quiz, postPassed};
        byId[lesson.id] = lp;
        return lp;
      });
      const complete = lessons.filter((l) => l.status === 'complete').length;
      return {phase, lessons, complete, total: lessons.length, percent: lessons.length ? Math.round((complete / lessons.length) * 100) : 0};
    });

  const all = phases.flatMap((p) => p.lessons);
  const complete = all.filter((l) => l.status === 'complete').length;

  const order = (curriculumModel.paths[0]?.lessons.map((l) => l.id) ?? []).concat(all.map((l) => l.lesson.id));
  const seen = new Set<string>();
  let next: LessonProgress | null = null;
  for (const id of order) {
    if (seen.has(id)) continue;
    seen.add(id);
    const lp = byId[id];
    if (!lp || lp.status === 'complete') continue;
    const prereqsDone = lp.lesson.prereqs.every((pre) => byId[pre]?.status === 'complete' || !byId[pre]);
    if (prereqsDone) {
      next = lp;
      break;
    }
  }

  return {phases, complete, total: all.length, next, byId};
}
