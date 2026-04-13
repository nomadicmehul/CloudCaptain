/**
 * groupDocs — classify every doc path into a category/tool bucket so the
 * /progress dashboard can show per-tool completion.
 *
 * The full list of docs comes from Docusaurus's useAllDocsData(); this file
 * only defines the grouping rules (pathname regex → category label + icon).
 */

export type Category = {
  id: string;
  label: string;
  icon: string;
  /** Preferred display order (lower = higher) */
  order: number;
  match: RegExp;
};

export const CATEGORIES: Category[] = [
  // Learning paths
  {
    id: 'path-devops',
    label: 'DevOps Path',
    icon: '🚀',
    order: 10,
    match: /^\/docs\/learning-paths\/devops(\/|$)/,
  },
  {
    id: 'path-linux',
    label: 'Linux Master',
    icon: '🐧',
    order: 11,
    match: /^\/docs\/learning-paths\/linux-master(\/|$)/,
  },
  {
    id: 'path-aiml',
    label: 'AI / ML Ops',
    icon: '🤖',
    order: 12,
    match: /^\/docs\/learning-paths\/ai-ml(\/|$)/,
  },
  {
    id: 'path-other',
    label: 'Other Learning Paths',
    icon: '📚',
    order: 13,
    match: /^\/docs\/learning-paths\//,
  },

  // Tools
  {id: 'tool-docker',     label: 'Docker',     icon: '🐳', order: 20, match: /^\/docs\/tools\/docker(\/|$)/},
  {id: 'tool-kubernetes', label: 'Kubernetes', icon: '☸️',  order: 21, match: /^\/docs\/tools\/kubernetes(\/|$)/},
  {id: 'tool-terraform',  label: 'Terraform',  icon: '🏗️',  order: 22, match: /^\/docs\/tools\/terraform(\/|$)/},
  {id: 'tool-ansible',    label: 'Ansible',    icon: '🔧', order: 23, match: /^\/docs\/tools\/ansible(\/|$)/},
  {id: 'tool-helm',       label: 'Helm',       icon: '⎈',  order: 24, match: /^\/docs\/tools\/helm(\/|$)/},
  {id: 'tool-linux',      label: 'Linux',      icon: '🐧', order: 25, match: /^\/docs\/tools\/linux(\/|$)/},
  {id: 'tool-bash',       label: 'Bash',       icon: '💻', order: 26, match: /^\/docs\/tools\/bash(\/|$)/},
  {id: 'tool-python',     label: 'Python',     icon: '🐍', order: 27, match: /^\/docs\/tools\/python(\/|$)/},
  {id: 'tool-git',        label: 'Git',        icon: '🔀', order: 28, match: /^\/docs\/tools\/git(\/|$)/},
  {id: 'tool-yaml',       label: 'YAML',       icon: '📄', order: 29, match: /^\/docs\/tools\/yaml(\/|$)/},
  {id: 'tool-other',      label: 'Other Tools',icon: '🔧', order: 30, match: /^\/docs\/tools\//},

  // Cloud
  {id: 'cloud-aws',   label: 'AWS',   icon: '☁️', order: 40, match: /^\/docs\/cloud\/aws(\/|$)/},
  {id: 'cloud-azure', label: 'Azure', icon: '🌩️', order: 41, match: /^\/docs\/cloud\/azure(\/|$)/},
  {id: 'cloud-gcp',   label: 'GCP',   icon: '🌥️', order: 42, match: /^\/docs\/cloud\/gcp(\/|$)/},
  {id: 'cloud-other', label: 'Other Cloud', icon: '☁️', order: 43, match: /^\/docs\/cloud\//},

  // Interview prep + catch-all
  {id: 'interview', label: 'Interview Prep', icon: '🎯', order: 50, match: /^\/docs\/interview-prep(\/|$)/},
  {id: 'other',     label: 'Other Docs',     icon: '📄', order: 99, match: /^\/docs\//},
];

export function classifyDoc(pathname: string): Category | null {
  for (const cat of CATEGORIES) {
    if (cat.match.test(pathname)) return cat;
  }
  return null;
}

/**
 * Normalize a pathname for matching against localStorage keys.
 * Docusaurus may or may not include trailing slashes depending on config;
 * we compare both variants.
 */
export function normalizeCandidates(pathname: string): string[] {
  const trimmed = pathname.replace(/\/$/, '');
  return [pathname, trimmed, `${trimmed}/`];
}
