/**
 * Lookups over the generated curriculum model that other site components
 * (progress dashboard, resume widget, sidebar spine, backlink card) need.
 */
import curriculumJson from '@site/src/data/generated/curriculum.json';
import type {CurriculumLesson, CurriculumModel} from './types';

export const curriculumModel = curriculumJson as unknown as CurriculumModel;
export const learnBase = curriculumModel.site.routeBasePath.replace(/\/$/, '');

const titles = new Map<string, string>();
const lessonsByRoute = new Map<string, CurriculumLesson>();

for (const lesson of Object.values(curriculumModel.lessonsById)) {
  titles.set(lesson.route, lesson.title);
  lessonsByRoute.set(lesson.route, lesson);
}
for (const phase of curriculumModel.phases) titles.set(phase.route, `${phase.title} · phase overview`);
for (const p of curriculumModel.paths) titles.set(p.route, `${p.title} path`);
titles.set(learnBase, 'Learn: start here');

function norm(pathname: string): string {
  return pathname.replace(/\/+$/, '') || '/';
}

export function isCurriculumPath(pathname: string): boolean {
  const p = norm(pathname);
  return p === learnBase || p.startsWith(`${learnBase}/`);
}

export function curriculumTitle(pathname: string): string | undefined {
  return titles.get(norm(pathname));
}

export function lessonForPath(pathname: string): CurriculumLesson | undefined {
  return lessonsByRoute.get(norm(pathname));
}

export function backlinksForPath(pathname: string): CurriculumLesson[] {
  const ids = curriculumModel.backlinks[norm(pathname)] || [];
  return ids.map((id) => curriculumModel.lessonsById[id]).filter(Boolean);
}
