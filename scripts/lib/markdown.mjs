const FENCE_RE = /^(\s*)(```+|~~~+)\s*([\w+-]*)/;
const HEADING_RE = /^(#{1,6})\s+(.+?)\s*#*\s*$/;
const LINK_RE = /\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;
const QUIZ_TAG_RE = /<Quiz\s+stage=["'](pre|check|post)["'][^>]*\/>/g;
const COMPONENT_TAG_RE = /<([A-Z][A-Za-z0-9]*)\b[^>]*\/?>/g;

export function parseMarkdown(text) {
  const lines = text.split(/\r?\n/);
  const headings = [];
  const fences = [];
  let open = null;

  lines.forEach((line, i) => {
    const fm = FENCE_RE.exec(line);
    if (open) {
      if (fm && fm[2][0] === open.marker[0] && fm[2].length >= open.marker.length && fm[3] === '') {
        open.end = i;
        fences.push(open);
        open = null;
      }
      return;
    }
    if (fm) {
      open = {lang: fm[3] || '', start: i, end: -1, marker: fm[2]};
      return;
    }
    const hm = HEADING_RE.exec(line);
    if (hm) headings.push({level: hm[1].length, text: hm[2].trim(), line: i});
  });
  if (open) {
    open.end = lines.length;
    fences.push(open);
  }

  const inFence = (i) => fences.some((f) => i > f.start && i < f.end);

  const h1 = headings.find((h) => h.level === 1) || null;
  let motto = null;
  if (h1) {
    for (let i = h1.line + 1; i < lines.length; i++) {
      const t = lines[i].trim();
      if (!t) continue;
      if (t.startsWith('#') || t.startsWith('<')) break;
      motto = t.replace(/^[*_]+|[*_]+$/g, '').trim();
      break;
    }
  }

  const h2s = headings.filter((h) => h.level === 2);
  const sections = h2s.map((h, idx) => {
    const start = h.line;
    const end = idx + 1 < h2s.length ? h2s[idx + 1].line : lines.length;
    const body = lines.slice(start + 1, end).join('\n');
    return {
      title: h.text,
      start,
      end,
      body,
      fences: fences.filter((f) => f.start > start && f.start < end),
      h3s: headings.filter((x) => x.level === 3 && x.line > start && x.line < end).map((x) => x.text),
      tables: parseTables(lines.slice(start + 1, end), start + 1, inFence),
      links: [...body.matchAll(LINK_RE)].map((m) => ({text: m[1], url: m[2]})),
    };
  });

  const prose = lines.filter((_, i) => !inFence(i)).join('\n');
  const quizTags = [...prose.matchAll(QUIZ_TAG_RE)].map((m) => m[1]);
  const components = [...prose.matchAll(COMPONENT_TAG_RE)].map((m) => m[1]);

  return {lines, headings, fences, h1, motto, sections, quizTags, components};
}

function parseTables(sectionLines, offset, inFence) {
  const tables = [];
  let current = null;
  sectionLines.forEach((line, j) => {
    const abs = offset + j;
    const isRow = line.trim().startsWith('|') && !inFence(abs);
    if (isRow) {
      const cells = line
        .trim()
        .replace(/^\|/, '')
        .replace(/\|$/, '')
        .split('|')
        .map((c) => c.trim());
      const isSeparator = cells.every((c) => /^:?-{3,}:?$/.test(c));
      if (!current) current = {header: cells, rows: []};
      else if (!isSeparator) current.rows.push(cells);
    } else if (current) {
      tables.push(current);
      current = null;
    }
  });
  if (current) tables.push(current);
  return tables;
}

export function sectionByTitle(parsed, title) {
  return parsed.sections.find((s) => s.title === title) || null;
}

export function tableValue(table, field) {
  if (!table) return null;
  const row = table.rows.find((r) => (r[0] || '').toLowerCase() === field.toLowerCase());
  return row ? row[1] || '' : null;
}
