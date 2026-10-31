import {useDoc} from '@docusaurus/plugin-content-docs/client';
import type {LessonMetaData, QuizData} from './types';

export function useLessonFrontMatter(): {lesson?: LessonMetaData; quiz?: QuizData; kind?: string} {
  const {frontMatter} = useDoc();
  const fm = frontMatter as Record<string, unknown>;
  return {
    lesson: fm.cc_lesson as LessonMetaData | undefined,
    quiz: (fm.cc_quiz as QuizData | null | undefined) || undefined,
    kind: fm.cc_kind as string | undefined,
  };
}

export function githubUrl(lesson: LessonMetaData, relPath: string, tree = false): string {
  const kind = tree ? 'tree' : 'blob';
  return `https://github.com/${lesson.repo}/${kind}/${lesson.branch}/curriculum/${lesson.dir}/${relPath}`.replace(/\/$/, '');
}
