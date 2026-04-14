import {useEffect, useState} from 'react';

export type Command = {
  text: string;
  language: string;
  blockIndex: number;
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
        (/^[a-z][a-z0-9_-]{1,30}$/.test(first) && /\s/.test(cleaned));
      if (!looksLikeCmd) continue;
      out.push({text: cleaned, language: lang || 'sh', blockIndex});
      if (out.length >= 40) return;
    }
  });
  const seen = new Set<string>();
  return out.filter((c) => {
    if (seen.has(c.text)) return false;
    seen.add(c.text);
    return true;
  });
}

export function useExtractors(article: HTMLElement | null) {
  const [commands, setCommands] = useState<Command[]>([]);

  useEffect(() => {
    if (!article) {
      setCommands([]);
      return undefined;
    }
    const run = () => {
      try {
        setCommands(extractCommands(article));
      } catch {
        setCommands([]);
      }
    };
    const t = window.setTimeout(run, 300);
    return () => window.clearTimeout(t);
  }, [article]);

  return {commands};
}
