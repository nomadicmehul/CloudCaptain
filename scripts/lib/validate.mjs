import fs from 'node:fs';
import path from 'node:path';
import {ENTRY_FILES, exists} from './discover.mjs';
import {parseMarkdown, sectionByTitle, tableValue} from './markdown.mjs';

export const SECTIONS = [
  'Metadata',
  'The Problem',
  'The Concept',
  'Build It',
  'Use It',
  'Ship It',
  'Exercises',
  'Key Terms',
  'Further Reading',
];

export const QUIZ_STAGE_COUNTS = {pre: 1, check: 3, post: 2};
const LESSON_TYPES = ['Build', 'Learn'];
const LESSON_STATUS = ['draft', 'preview', 'published'];
const LANGS = Object.keys(ENTRY_FILES);
const OUTPUT_RE = /^(prompt|skill|agent)-[a-z0-9-]+\.md$|^mcp-[a-z0-9-]+$/;
const ID_RE = /^[a-z0-9-]+\/[a-z0-9-]+$/;
const SITE_DOC_LINK_RE = /^(?:https:\/\/cloudcaptain\.io)?(\/docs\/[^#?\s]*)/;

export function resolveReferenceDoc(docPath, websiteDocsRoot) {
  const rel = docPath.replace(/^\/docs\/?/, '').replace(/\/$/, '');
  if (!rel) return null;
  const candidates = [
    path.join(websiteDocsRoot, `${rel}.md`),
    path.join(websiteDocsRoot, `${rel}.mdx`),
    path.join(websiteDocsRoot, rel, 'index.md'),
    path.join(websiteDocsRoot, rel, 'index.mdx'),
  ];
  return candidates.find(exists) || null;
}

export function validateLesson(lesson, ctx) {
  const err = (m) => lesson.errors.push(m);
  const warn = (m) => lesson.warnings.push(m);
  const json = lesson.json;

  if (json) {
    for (const key of ['id', 'title', 'motto', 'type', 'status', 'minutes', 'langs', 'prereqs', 'outputs', 'tests']) {
      if (!(key in json)) err(`lesson.json: missing "${key}"`);
    }
    const expectedId = `${lesson.phase.slug}/${lesson.slug}`;
    if (json.id !== expectedId) err(`lesson.json: id must be "${expectedId}" (folder ${lesson.dirName}), got "${json.id}"`);
    if (json.id && !ID_RE.test(json.id)) err(`lesson.json: id "${json.id}" is not <phase>/<lesson>`);
    if (!LESSON_TYPES.includes(json.type)) err(`lesson.json: type must be Build or Learn`);
    if (!LESSON_STATUS.includes(json.status)) err(`lesson.json: status must be draft, preview or published`);
    if (!Number.isInteger(json.minutes) || json.minutes < 15 || json.minutes > 180) err(`lesson.json: minutes must be an integer between 15 and 180`);
    if (!Array.isArray(json.langs) || json.langs.length === 0) err(`lesson.json: langs must list at least one language`);
    else for (const l of json.langs) if (!LANGS.includes(l)) err(`lesson.json: unknown language "${l}" (allowed: ${LANGS.join(', ')})`);
    if (typeof json.title === 'string' && json.title.length > 80) err('lesson.json: title longer than 80 characters');
    if (typeof json.motto === 'string' && json.motto.length > 140) err('lesson.json: motto longer than 140 characters');
    if (json.last_verified && !/^\d{4}-\d{2}-\d{2}$/.test(json.last_verified)) err('lesson.json: last_verified must be YYYY-MM-DD');
    if (!json.last_verified) warn('lesson.json: no last_verified date; set it when you have run the commands');
    for (const ref of json.reference || []) {
      if (!resolveReferenceDoc(ref, ctx.websiteDocsRoot)) err(`lesson.json: reference "${ref}" does not exist under website/docs`);
    }
    if (json.tests && typeof json.tests === 'object') {
      for (const lang of json.langs || []) if (!json.tests[lang]) warn(`lesson.json: no test command for language "${lang}"`);
    } else err('lesson.json: tests must be an object of <language>: <command>');

    for (const lang of json.langs || []) {
      const entry = ENTRY_FILES[lang];
      if (entry && !exists(path.join(lesson.files.code, entry))) err(`code/${entry} is missing for language "${lang}"`);
    }
    for (const out of json.outputs || []) {
      const name = out.replace(/\/$/, '');
      if (!OUTPUT_RE.test(name)) err(`outputs: "${out}" must be prompt-*.md, skill-*.md, agent-*.md or mcp-*/`);
      if (!exists(path.join(lesson.files.outputs, name))) err(`outputs/${out} is listed in lesson.json but missing`);
    }
    if ((json.outputs || []).length === 0) err('lesson.json: every lesson ships at least one artifact in outputs');
  }

  if (!exists(lesson.files.tests)) err('code/tests/ is missing');
  else if (lesson.testCount < 5) err(`code/tests/ has ${lesson.testCount} tests; at least 5 are required`);

  if (lesson.quiz) validateQuiz(lesson.quiz, json, err);
  if (lesson.md && json) validateDoc(lesson, json, ctx, err, warn);
}

function validateQuiz(quiz, json, err) {
  if (json && quiz.lesson !== json.id) err(`quiz.json: lesson must be "${json.id}"`);
  const qs = Array.isArray(quiz.questions) ? quiz.questions : [];
  if (qs.length !== 6) err(`quiz.json: expected 6 questions, found ${qs.length}`);
  const counts = {pre: 0, check: 0, post: 0};
  const ids = new Set();
  qs.forEach((q, i) => {
    const label = `quiz.json question ${i + 1}`;
    if (!q.id) err(`${label}: missing id`);
    else if (ids.has(q.id)) err(`${label}: duplicate id "${q.id}"`);
    else ids.add(q.id);
    if (!(q.stage in counts)) err(`${label}: stage must be pre, check or post`);
    else counts[q.stage]++;
    if (!q.prompt || q.prompt.length < 10) err(`${label}: prompt too short`);
    if (!Array.isArray(q.choices) || q.choices.length < 3) err(`${label}: needs at least 3 choices`);
    else if (!Number.isInteger(q.answer) || q.answer < 0 || q.answer >= q.choices.length) err(`${label}: answer index out of range`);
    if (!q.explanation || q.explanation.length < 10) err(`${label}: explanation too short`);
  });
  for (const [stage, want] of Object.entries(QUIZ_STAGE_COUNTS)) {
    if (counts[stage] !== want) err(`quiz.json: expected ${want} "${stage}" question(s), found ${counts[stage]}`);
  }
}

function validateDoc(lesson, json, ctx, err, warn) {
  const parsed = parseMarkdown(lesson.md);
  lesson.parsed = parsed;

  if (!parsed.h1) err('docs/en.md: missing H1 title');
  else if (parsed.h1.text !== json.title) err(`docs/en.md: H1 "${parsed.h1.text}" must equal lesson.json title "${json.title}"`);
  if (!parsed.motto) err('docs/en.md: the line after the title must be the motto in italics');
  else if (parsed.motto !== json.motto) err(`docs/en.md: motto must equal lesson.json motto`);

  const titles = parsed.sections.map((s) => s.title);
  if (titles.join('|') !== SECTIONS.join('|')) {
    err(`docs/en.md: H2 sections must be exactly, in order: ${SECTIONS.join(' → ')}. Found: ${titles.join(' → ') || '(none)'}`);
  }

  const meta = sectionByTitle(parsed, 'Metadata');
  if (meta) {
    const table = meta.tables[0];
    if (!table) err('Metadata: needs a Field | Value table');
    else {
      const type = tableValue(table, 'Type');
      if (type !== json.type) err(`Metadata: Type is "${type}", lesson.json says "${json.type}"`);
      const time = tableValue(table, 'Time');
      if (time !== `${json.minutes} minutes`) err(`Metadata: Time must be "${json.minutes} minutes", found "${time}"`);
      const langs = (tableValue(table, 'Languages') || '').toLowerCase();
      for (const l of json.langs || []) if (!langs.includes(l)) warn(`Metadata: Languages row does not mention "${l}"`);
    }
  }

  const concept = sectionByTitle(parsed, 'The Concept');
  if (concept) {
    const diagrams = concept.fences.filter((f) => f.lang === 'mermaid');
    const hasImage = /!\[[^\]]*\]\(/.test(concept.body);
    if (diagrams.length === 0 && !hasImage) err('The Concept: needs a mermaid diagram or an image');
    if (concept.fences.some((f) => f.lang !== 'mermaid')) err('The Concept: no code fences other than mermaid (save code for Build It)');
  }

  const build = sectionByTitle(parsed, 'Build It');
  if (build && build.fences.length === 0) err('Build It: needs at least one code fence');

  const exercises = sectionByTitle(parsed, 'Exercises');
  if (exercises) {
    const want = ['Easy', 'Medium', 'Hard'];
    if (exercises.h3s.join('|') !== want.join('|')) err(`Exercises: needs exactly ### Easy, ### Medium, ### Hard (found: ${exercises.h3s.join(', ') || 'none'})`);
  }

  const terms = sectionByTitle(parsed, 'Key Terms');
  if (terms) {
    const table = terms.tables[0];
    if (!table || table.rows.length < 5) err(`Key Terms: table needs at least 5 rows (found ${table ? table.rows.length : 0})`);
  }

  const further = sectionByTitle(parsed, 'Further Reading');
  if (further) {
    const siteLinks = further.links.map((l) => SITE_DOC_LINK_RE.exec(l.url)).filter(Boolean).map((m) => m[1]);
    if (siteLinks.length === 0) err('Further Reading: link at least one Reference-layer page (https://cloudcaptain.io/docs/...)');
    for (const p of siteLinks) if (!resolveReferenceDoc(p, ctx.websiteDocsRoot)) err(`Further Reading: ${p} does not exist under website/docs`);
  }

  for (const s of parsed.sections) {
    for (const l of s.links) {
      const m = SITE_DOC_LINK_RE.exec(l.url);
      if (m && !resolveReferenceDoc(m[1], ctx.websiteDocsRoot)) err(`${s.title}: link ${m[1]} does not exist under website/docs`);
    }
  }

  for (const stage of Object.keys(QUIZ_STAGE_COUNTS)) {
    const n = parsed.quizTags.filter((t) => t === stage).length;
    if (n !== 1) err(`docs/en.md: expected exactly one <Quiz stage="${stage}" /> tag, found ${n}`);
  }
  if (!parsed.components.includes('ShipIt')) warn('Ship It: add <ShipIt /> so the site links the artifacts');
  if (!parsed.components.includes('RunTests')) warn('Build It: add <RunTests /> so the site shows the test command');
  if (/\bTODO\b/.test(lesson.md)) warn('docs/en.md still contains TODO');
}

export function validateGlobal(phases, manifest, ctx) {
  const errors = [];
  const warnings = [];
  const byId = new Map();

  for (const phase of phases) {
    if (phase.lessons.length > 0 && !phase.readmeExists) errors.push(`phases/${phase.dir}/README.md is required once the phase has lessons`);
    for (const lesson of phase.lessons) {
      if (!lesson.json || !lesson.json.id) continue;
      if (byId.has(lesson.json.id)) errors.push(`duplicate lesson id ${lesson.json.id}`);
      byId.set(lesson.json.id, {lesson, phase});
    }
  }

  for (const [id, {lesson, phase}] of byId) {
    for (const pre of lesson.json.prereqs || []) {
      const target = byId.get(pre);
      if (!target) {
        errors.push(`${id}: prerequisite ${pre} does not exist`);
      } else if (target.phase.phaseIndex > phase.phaseIndex) {
        warnings.push(`${id}: prerequisite ${pre} is in a later phase`);
      }
    }
  }

  const visiting = new Set();
  const done = new Set();
  const visit = (id, trail) => {
    if (done.has(id)) return;
    if (visiting.has(id)) {
      errors.push(`prerequisite cycle: ${[...trail, id].join(' → ')}`);
      return;
    }
    visiting.add(id);
    const entry = byId.get(id);
    for (const pre of entry?.lesson.json.prereqs || []) if (byId.has(pre)) visit(pre, [...trail, id]);
    visiting.delete(id);
    done.add(id);
  };
  for (const id of byId.keys()) visit(id, []);

  const seenPath = new Set();
  for (const p of manifest.paths || []) {
    if (seenPath.has(p.id)) errors.push(`manifest: duplicate path id ${p.id}`);
    seenPath.add(p.id);
    if (!exists(path.join(ctx.curriculumRoot, 'paths', `${p.id}.md`))) errors.push(`manifest: path "${p.id}" needs curriculum/paths/${p.id}.md`);
    for (const id of p.lessons || []) if (!byId.has(id)) errors.push(`manifest: path "${p.id}" lists unknown lesson ${id}`);
  }

  const slugs = new Set();
  for (const phase of manifest.phases) {
    if (slugs.has(phase.slug)) errors.push(`manifest: duplicate phase slug ${phase.slug}`);
    slugs.add(phase.slug);
  }

  for (const cert of manifest.certifications || []) {
    if (!resolveReferenceDoc(cert.referenceDoc, ctx.websiteDocsRoot)) errors.push(`manifest: certification "${cert.id}" referenceDoc ${cert.referenceDoc} does not exist`);
    for (const slug of cert.phases || []) if (!slugs.has(slug)) errors.push(`manifest: certification "${cert.id}" references unknown phase "${slug}"`);
  }

  if (!exists(path.join(ctx.curriculumRoot, 'README.md'))) errors.push('curriculum/README.md (the /learn landing page) is missing');

  return {errors, warnings, byId};
}

export function formatReport(phases, global) {
  const lines = [];
  let errorCount = global.errors.length;
  let warningCount = global.warnings.length;
  for (const phase of phases) {
    for (const lesson of phase.lessons) {
      errorCount += lesson.errors.length;
      warningCount += lesson.warnings.length;
      if (lesson.errors.length || lesson.warnings.length) {
        lines.push(`\n${lesson.relDir}`);
        for (const e of lesson.errors) lines.push(`  ✖ ${e}`);
        for (const w of lesson.warnings) lines.push(`  ⚠ ${w}`);
      }
    }
  }
  if (global.errors.length || global.warnings.length) {
    lines.push('\ncurriculum');
    for (const e of global.errors) lines.push(`  ✖ ${e}`);
    for (const w of global.warnings) lines.push(`  ⚠ ${w}`);
  }
  return {text: lines.join('\n'), errorCount, warningCount};
}

export function readFileIfExists(file) {
  return exists(file) ? fs.readFileSync(file, 'utf8') : null;
}
