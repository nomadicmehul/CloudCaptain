import {useEffect, useState} from 'react';

export type Command = {
  text: string;
  language: string;
  blockIndex: number;
};

export type Concept = {
  text: string;
  count: number;
  firstSeenId?: string;
};

const CLI_LANGS = new Set([
  'bash',
  'sh',
  'shell',
  'zsh',
  'fish',
  'console',
  'ps1',
  'powershell',
  'hcl',
  'yaml',
  'yml',
  'dockerfile',
  'docker',
]);

const CLI_KEYWORDS = [
  'kubectl',
  'docker',
  'terraform',
  'helm',
  'aws',
  'az',
  'gcloud',
  'ansible',
  'git',
  'curl',
  'wget',
  'npm',
  'yarn',
  'python',
  'pip',
  'go',
  'cargo',
  'make',
  'sudo',
  'apt',
  'brew',
  'systemctl',
  'journalctl',
];

const STOPWORDS = new Set([
  'the',
  'and',
  'for',
  'with',
  'this',
  'that',
  'from',
  'your',
  'will',
  'can',
  'you',
  'are',
  'not',
  'but',
  'all',
  'one',
  'any',
  'more',
  'also',
  'into',
  'they',
  'them',
  'their',
  'been',
  'when',
  'what',
  'which',
  'how',
  'why',
  'use',
  'using',
  'used',
  'see',
  'get',
  'set',
  'run',
  'new',
  'example',
]);

function classify(node: Element): string {
  const cls = node.className || '';
  const match = /language-([a-z0-9]+)/.exec(String(cls));
  return match ? match[1].toLowerCase() : '';
}

function extractCommands(article: HTMLElement): Command[] {
  const blocks = Array.from(article.querySelectorAll<HTMLElement>('pre code, pre'));
  const out: Command[] = [];
  blocks.forEach((block, blockIndex) => {
    const lang = classify(block) || classify(block.parentElement ?? block);
    if (lang && !CLI_LANGS.has(lang)) return;
    const text = block.textContent ?? '';
    const lines = text.split('\n').slice(0, 12);
    for (const rawLine of lines) {
      const line = rawLine.trim();
      if (!line) continue;
      if (line.startsWith('#') || line.startsWith('//')) continue;
      const cleaned = line.replace(/^[\$>#]\s+/, '');
      if (cleaned.length < 3 || cleaned.length > 140) continue;
      const first = cleaned.split(/\s+/)[0] ?? '';
      const looksLikeCmd =
        CLI_KEYWORDS.includes(first) ||
        /^[a-z][a-z0-9_-]{1,30}$/.test(first) && /\s/.test(cleaned);
      if (!looksLikeCmd) continue;
      out.push({text: cleaned, language: lang || 'sh', blockIndex});
      if (out.length >= 40) return;
    }
  });
  // Dedup by text, keep first
  const seen = new Set<string>();
  return out.filter((c) => {
    if (seen.has(c.text)) return false;
    seen.add(c.text);
    return true;
  });
}

function extractConcepts(article: HTMLElement): Concept[] {
  const counts = new Map<string, {count: number; firstSeenId?: string}>();
  // Inline <code> in prose (skip those inside <pre>)
  const inlines = Array.from(article.querySelectorAll<HTMLElement>('code')).filter(
    (c) => !c.closest('pre')
  );
  const strongs = Array.from(article.querySelectorAll<HTMLElement>('strong, b'));
  const all = [...inlines, ...strongs];
  for (const el of all) {
    const raw = (el.textContent ?? '').trim();
    if (!raw) continue;
    if (raw.length < 2 || raw.length > 40) continue;
    const lower = raw.toLowerCase();
    if (STOPWORDS.has(lower)) continue;
    // skip if it's just punctuation/numbers
    if (!/[a-zA-Z]/.test(raw)) continue;
    // skip long sentences
    if (raw.split(/\s+/).length > 4) continue;

    const key = raw;
    const existing = counts.get(key);
    const heading = el.closest('section')?.querySelector('h2, h3')?.id;
    if (existing) existing.count += 1;
    else counts.set(key, {count: 1, firstSeenId: heading});
  }
  return Array.from(counts.entries())
    .map(([text, v]) => ({text, count: v.count, firstSeenId: v.firstSeenId}))
    .sort((a, b) => b.count - a.count)
    .slice(0, 30);
}

export function useExtractors(article: HTMLElement | null) {
  const [commands, setCommands] = useState<Command[]>([]);
  const [concepts, setConcepts] = useState<Concept[]>([]);

  useEffect(() => {
    if (!article) {
      setCommands([]);
      setConcepts([]);
      return undefined;
    }
    const run = () => {
      try {
        setCommands(extractCommands(article));
        setConcepts(extractConcepts(article));
      } catch {
        setCommands([]);
        setConcepts([]);
      }
    };
    const t = window.setTimeout(run, 300);
    return () => window.clearTimeout(t);
  }, [article]);

  return {commands, concepts};
}
