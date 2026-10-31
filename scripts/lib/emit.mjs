import fs from 'node:fs';
import path from 'node:path';

export function lessonDocId(lesson) {
  return `${lesson.relDir}/docs/en`;
}

export function phaseDocId(phase) {
  return `phases/${phase.dir}/README`;
}

function publicLesson(lesson, phase, base, index) {
  const j = lesson.json;
  return {
    id: j.id,
    slug: lesson.slug,
    number: lesson.number,
    index,
    title: j.title,
    short: j.short || null,
    motto: j.motto,
    type: j.type,
    status: j.status,
    minutes: j.minutes,
    langs: j.langs,
    prereqs: j.prereqs || [],
    tools: j.tools || [],
    reference: j.reference || [],
    outputs: j.outputs || [],
    tests: j.tests || {},
    lastVerified: j.last_verified || null,
    phase: phase.slug,
    phaseTitle: phase.title,
    phaseNumber: phase.number,
    dir: lesson.relDir,
    docId: lessonDocId(lesson),
    route: `${base}/${phase.slug}/${lesson.slug}`,
    testCount: lesson.testCount,
  };
}

export function buildModel(phases, manifest) {
  const base = manifest.site.routeBasePath.replace(/\/$/, '');
  const lessonsById = {};
  const backlinks = {};
  const stats = {lessons: 0, buildLessons: 0, learnLessons: 0, minutes: 0, tests: 0, quizzes: 0, phasesLive: 0, phasesTotal: manifest.phases.length};

  const outPhases = phases.map((phase) => {
    const visible = phase.lessons.filter((l) => l.json && l.json.status !== 'draft');
    const lessons = visible.map((lesson, i) => {
      const pub = publicLesson(lesson, phase, base, i + 1);
      lessonsById[pub.id] = pub;
      stats.lessons += 1;
      stats.minutes += pub.minutes;
      stats.tests += lesson.testCount;
      if (lesson.quiz) stats.quizzes += 1;
      if (pub.type === 'Build') stats.buildLessons += 1;
      else stats.learnLessons += 1;
      for (const ref of pub.reference) (backlinks[ref] ||= []).push(pub.id);
      return pub;
    });
    if (lessons.length) stats.phasesLive += 1;
    return {
      number: phase.number,
      dir: phase.dir,
      slug: phase.slug,
      title: phase.title,
      icon: phase.icon || '',
      tagline: phase.tagline,
      docId: phaseDocId(phase),
      route: `${base}/${phase.slug}`,
      live: lessons.length > 0,
      minutes: lessons.reduce((a, l) => a + l.minutes, 0),
      lessons,
    };
  });

  stats.hours = Math.round((stats.minutes / 60) * 10) / 10;

  const paths = (manifest.paths || []).map((p) => {
    const lessons = (p.lessons || []).map((id) => lessonsById[id]).filter(Boolean);
    return {
      id: p.id,
      title: p.title,
      description: p.description || '',
      careerPath: p.careerPath || null,
      route: `${base}/paths/${p.id}`,
      docId: `paths/${p.id}`,
      minutes: lessons.reduce((a, l) => a + l.minutes, 0),
      lessons,
    };
  });

  const sidebar = [
    {type: 'doc', id: 'README', label: 'Start here'},
    ...outPhases
      .filter((p) => p.live)
      .map((p) => ({
        type: 'category',
        label: `${p.number} · ${p.title}`,
        collapsed: false,
        link: {type: 'doc', id: p.docId},
        items: p.lessons.map((l) => ({type: 'doc', id: l.docId, label: l.short || l.title})),
      })),
  ];
  if (paths.length) {
    sidebar.push({
      type: 'category',
      label: 'Learning Paths',
      collapsed: true,
      items: paths.map((p) => ({type: 'doc', id: p.docId, label: p.title})),
    });
  }

  return {
    generatedAt: new Date().toISOString(),
    site: {...manifest.site, routeBasePath: base},
    stats,
    phases: outPhases,
    paths,
    certifications: manifest.certifications || [],
    backlinks,
    lessonsById,
    sidebar,
  };
}

export function writeModel(model, outFile) {
  fs.mkdirSync(path.dirname(outFile), {recursive: true});
  fs.writeFileSync(outFile, JSON.stringify(model, null, 2) + '\n');
}

export function roadmapTable(model) {
  const rows = model.phases.map((p) => {
    const status = p.lessons.length === 0 ? 'planned' : p.lessons.length < 10 ? 'in progress' : 'live';
    return `| ${p.number} | ${p.title} | ${p.lessons.length} | ${status} |`;
  });
  return ['| # | Phase | Lessons live | Status |', '|---|---|---|---|', ...rows].join('\n');
}

export function updateRoadmap(roadmapFile, model) {
  const start = '<!-- curriculum:start -->';
  const end = '<!-- curriculum:end -->';
  const text = fs.readFileSync(roadmapFile, 'utf8');
  const a = text.indexOf(start);
  const b = text.indexOf(end);
  if (a === -1 || b === -1 || b < a) throw new Error(`ROADMAP.md needs ${start} and ${end} markers`);
  const next = text.slice(0, a + start.length) + '\n' + roadmapTable(model) + '\n' + text.slice(b);
  fs.writeFileSync(roadmapFile, next);
  return next !== text;
}
