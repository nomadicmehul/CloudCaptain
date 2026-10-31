/**
 * Injects Docusaurus front matter for files under curriculum/ so lesson
 * markdown stays clean for GitHub, agents and the book edition.
 *
 * Wired into docusaurus.config.ts via markdown.parseFrontMatter.
 */
import fs from 'node:fs';
import path from 'node:path';

type Json = Record<string, any>;

const siteDir = typeof __dirname !== 'undefined' ? path.resolve(__dirname, '..') : process.cwd();
export const curriculumRoot = path.resolve(siteDir, '..', 'curriculum');

let manifestCache: Json | null = null;

function manifest(): Json {
  if (!manifestCache) {
    manifestCache = JSON.parse(fs.readFileSync(path.join(curriculumRoot, 'manifest.json'), 'utf8'));
  }
  return manifestCache as Json;
}

function readJson(file: string): Json | null {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return null;
  }
}

export function isCurriculumFile(filePath: string): boolean {
  return path.resolve(filePath).startsWith(curriculumRoot + path.sep);
}

export function curriculumFrontMatter(filePath: string): Json {
  const rel = path.relative(curriculumRoot, path.resolve(filePath)).split(path.sep).join('/');
  const m = manifest();

  if (rel === 'README.md') {
    return {
      slug: '/',
      title: 'Learn Cloud, DevOps & AIOps from scratch',
      sidebar_label: 'Start here',
      description: 'Build the tool, use the tool, ship an artifact. The CloudCaptain curriculum.',
      hide_table_of_contents: true,
      cc_kind: 'landing',
    };
  }

  let match = /^phases\/([^/]+)\/README\.md$/.exec(rel);
  if (match) {
    const phase = m.phases.find((p: Json) => p.dir === match![1]);
    if (!phase) return {};
    return {
      slug: `/${phase.slug}`,
      title: phase.title,
      sidebar_label: phase.title,
      description: phase.tagline,
      cc_kind: 'phase',
      cc_phase: {...phase, number: phase.dir.slice(0, 2)},
    };
  }

  match = /^phases\/([^/]+)\/([^/]+)\/docs\/en\.md$/.exec(rel);
  if (match) {
    const phase = m.phases.find((p: Json) => p.dir === match![1]);
    const lessonDir = path.join(curriculumRoot, 'phases', match[1], match[2]);
    const lesson = readJson(path.join(lessonDir, 'lesson.json'));
    if (!phase || !lesson) return {};
    const quiz = readJson(path.join(lessonDir, 'quiz.json'));
    const lessonSlug = String(lesson.id).split('/')[1];
    return {
      slug: `/${phase.slug}/${lessonSlug}`,
      title: lesson.title,
      sidebar_label: lesson.short || lesson.title,
      description: lesson.motto,
      keywords: [...(lesson.tools || []), phase.slug, 'from scratch'],
      draft: lesson.status === 'draft',
      unlisted: lesson.status === 'preview',
      cc_kind: 'lesson',
      cc_lesson: {
        ...lesson,
        phase: phase.slug,
        phaseTitle: phase.title,
        phaseNumber: phase.dir.slice(0, 2),
        dir: `phases/${match[1]}/${match[2]}`,
        repo: m.site.repo,
        branch: m.site.branch || 'main',
      },
      cc_quiz: quiz,
    };
  }

  match = /^paths\/([^/]+)\.md$/.exec(rel);
  if (match) {
    const p = (m.paths || []).find((x: Json) => x.id === match![1]);
    return {
      slug: `/paths/${match[1]}`,
      title: p ? `${p.title} path` : match[1],
      sidebar_label: p ? p.title : match[1],
      description: p?.description,
      hide_table_of_contents: true,
      cc_kind: 'path',
      cc_path: p || null,
    };
  }

  return {};
}
