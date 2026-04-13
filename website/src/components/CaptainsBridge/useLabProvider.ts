/**
 * useLabProvider — picks a live sandbox provider for the current doc based
 * on the pathname.
 *
 * Phase 1 (current): all providers render as LAUNCHERS, not embedded
 * iframes. KillerCoda — like most modern sandbox services — sets
 * X-Frame-Options: DENY / CSP frame-ancestors, which blocks iframe
 * embedding as an anti-clickjacking measure. Instead of fighting that,
 * the Lab pane shows a prominent "Launch in new tab" card with provider
 * attribution and keeps the reading experience undisturbed.
 *
 * Phase 3 will introduce truly-embeddable providers on a per-tool basis
 * (e.g. WebVM for Linux, Pyodide for Python, StackBlitz for Node).
 */
import {useMemo} from 'react';

export type LabMode = 'launcher' | 'iframe';

export type LabProvider = {
  id: string;
  name: string;
  url: string;
  attribution: string;
  attributionUrl: string;
  /** Short tag shown in the telemetry button, e.g. "K8s" */
  shortLabel: string;
  /** A short description shown on the launcher card */
  description: string;
  /** Rendering mode — most Phase 1 providers are 'launcher' only */
  mode: LabMode;
  /** Optional override URL used when mode === 'iframe' */
  embedUrl?: string;
};

const KILLERCODA_ATTRIB = {
  attribution: 'Powered by KillerCoda',
  attributionUrl: 'https://killercoda.com/',
};

const PROVIDERS: Array<{match: RegExp; provider: LabProvider}> = [
  {
    match: /^\/docs\/tools\/kubernetes(\/|$)/,
    provider: {
      id: 'killercoda-k8s',
      name: 'Kubernetes Playground',
      url: 'https://killercoda.com/playgrounds/scenario/kubernetes',
      shortLabel: 'K8s',
      description:
        'A fresh, real Kubernetes cluster (kubeadm) ready to use. kubectl is pre-configured. Session is ephemeral.',
      mode: 'launcher',
      ...KILLERCODA_ATTRIB,
    },
  },
  {
    match: /^\/docs\/tools\/docker(\/|$)/,
    provider: {
      id: 'killercoda-docker',
      name: 'Docker Playground',
      url: 'https://killercoda.com/playgrounds/scenario/docker',
      shortLabel: 'Docker',
      description:
        'A clean Linux host with Docker Engine pre-installed. Build, run, and network containers in a throw-away sandbox.',
      mode: 'launcher',
      ...KILLERCODA_ATTRIB,
    },
  },
  {
    match: /^\/docs\/tools\/terraform(\/|$)/,
    provider: {
      id: 'killercoda-terraform',
      name: 'Terraform Playground',
      url: 'https://killercoda.com/playgrounds/scenario/terraform',
      shortLabel: 'TF',
      description:
        'Terraform CLI pre-installed on a Linux host. Run terraform init/plan/apply against local or LocalStack providers.',
      mode: 'launcher',
      ...KILLERCODA_ATTRIB,
    },
  },
  {
    match: /^\/docs\/tools\/ansible(\/|$)/,
    provider: {
      id: 'killercoda-ansible',
      name: 'Ansible Playground',
      url: 'https://killercoda.com/playgrounds/scenario/ansible',
      shortLabel: 'Ansible',
      description:
        'Ansible pre-installed on a control node with a managed target. Write playbooks and watch them converge.',
      mode: 'launcher',
      ...KILLERCODA_ATTRIB,
    },
  },
  {
    match: /^\/docs\/tools\/(linux|bash)(\/|$)/,
    provider: {
      id: 'killercoda-ubuntu',
      name: 'Ubuntu Playground',
      url: 'https://killercoda.com/playgrounds/scenario/ubuntu',
      shortLabel: 'Linux',
      description:
        'A fresh Ubuntu shell. Practice bash, system admin commands, package management — no VM setup required.',
      mode: 'launcher',
      ...KILLERCODA_ATTRIB,
    },
  },
];

export function useLabProvider(pathname: string): LabProvider | null {
  return useMemo(() => {
    for (const {match, provider} of PROVIDERS) {
      if (match.test(pathname)) return provider;
    }
    return null;
  }, [pathname]);
}
