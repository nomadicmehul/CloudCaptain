import fs from 'node:fs';
import path from 'node:path';

export const LESSON_DIR_RE = /^(\d{2})-([a-z0-9]+(?:-[a-z0-9]+)*)$/;

export const ENTRY_FILES = {
  python: 'main.py',
  bash: 'main.sh',
  go: 'main.go',
  typescript: 'index.ts',
  yaml: 'main.yaml',
};

export function exists(p) {
  try {
    fs.accessSync(p);
    return true;
  } catch {
    return false;
  }
}

export function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

export function loadManifest(curriculumRoot) {
  return readJson(path.join(curriculumRoot, 'manifest.json'));
}

export function walk(dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, {withFileTypes: true})) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === '__pycache__' || entry.name === 'node_modules') continue;
      out.push(...walk(full));
    } else {
      out.push(full);
    }
  }
  return out.sort();
}

const TEST_PATTERNS = [
  /^\s*def test_\w+/gm,
  /^\s*@test\b/gm,
  /\b(?:test|it)\(\s*['"`]/g,
  /^func Test[A-Z]\w*\(/gm,
];

export function countTests(files) {
  let total = 0;
  for (const file of files) {
    let text;
    try {
      text = fs.readFileSync(file, 'utf8');
    } catch {
      continue;
    }
    for (const re of TEST_PATTERNS) {
      const matches = text.match(re);
      if (matches) total += matches.length;
    }
  }
  return total;
}

function readLesson(ctx) {
  const files = {
    json: path.join(ctx.dir, 'lesson.json'),
    md: path.join(ctx.dir, 'docs', 'en.md'),
    quiz: path.join(ctx.dir, 'quiz.json'),
    code: path.join(ctx.dir, 'code'),
    tests: path.join(ctx.dir, 'code', 'tests'),
    outputs: path.join(ctx.dir, 'outputs'),
  };
  const lesson = {...ctx, files, json: null, quiz: null, md: null, errors: [], warnings: []};

  for (const [key, file, label] of [
    ['json', files.json, 'lesson.json'],
    ['quiz', files.quiz, 'quiz.json'],
  ]) {
    if (!exists(file)) {
      lesson.errors.push(`${label} is missing`);
      continue;
    }
    try {
      lesson[key] = readJson(file);
    } catch (e) {
      lesson.errors.push(`${label} is not valid JSON: ${e.message}`);
    }
  }

  if (exists(files.md)) lesson.md = fs.readFileSync(files.md, 'utf8');
  else lesson.errors.push('docs/en.md is missing');

  lesson.testFiles = exists(files.tests) ? walk(files.tests) : [];
  lesson.testCount = countTests(lesson.testFiles);
  lesson.outputFiles = exists(files.outputs) ? fs.readdirSync(files.outputs).sort() : [];
  return lesson;
}

export function discover(curriculumRoot, manifest) {
  return manifest.phases.map((phase, phaseIndex) => {
    const phaseDir = path.join(curriculumRoot, 'phases', phase.dir);
    const lessons = [];
    if (exists(phaseDir)) {
      const entries = fs
        .readdirSync(phaseDir, {withFileTypes: true})
        .filter((e) => e.isDirectory())
        .sort((a, b) => a.name.localeCompare(b.name));
      for (const entry of entries) {
        const m = LESSON_DIR_RE.exec(entry.name);
        if (!m) continue;
        lessons.push(
          readLesson({
            phase,
            phaseIndex,
            dirName: entry.name,
            number: m[1],
            slug: m[2],
            dir: path.join(phaseDir, entry.name),
            relDir: `phases/${phase.dir}/${entry.name}`,
          }),
        );
      }
    }
    return {
      ...phase,
      number: phase.dir.slice(0, 2),
      phaseIndex,
      dirExists: exists(phaseDir),
      readmeExists: exists(path.join(phaseDir, 'README.md')),
      lessons,
    };
  });
}
