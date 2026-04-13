/**
 * useLabProvider — picks a live sandbox provider for the current doc based
 * on the pathname. Phase 1 uses KillerCoda playgrounds exclusively (single
 * provider → single attribution, single iframe origin, no CSP complexity).
 *
 * Returns `null` for pages without a matching playground; the Lab toggle in
 * BridgeTelemetry is hidden in that case.
 */
import {useMemo} from 'react';

export type LabProvider = {
  id: string;
  name: string;
  url: string;
  attribution: string;
  attributionUrl: string;
  /** Short tag shown in the telemetry button, e.g. "K8s" */
  shortLabel: string;
};

const PROVIDERS: Array<{match: RegExp; provider: LabProvider}> = [
  {
    match: /^\/docs\/tools\/kubernetes(\/|$)/,
    provider: {
      id: 'killercoda-k8s',
      name: 'Kubernetes Playground',
      url: 'https://killercoda.com/playgrounds/scenario/kubernetes',
      attribution: 'Lab by KillerCoda',
      attributionUrl: 'https://killercoda.com/',
      shortLabel: 'K8s',
    },
  },
  {
    match: /^\/docs\/tools\/docker(\/|$)/,
    provider: {
      id: 'killercoda-docker',
      name: 'Docker Playground',
      url: 'https://killercoda.com/playgrounds/scenario/docker',
      attribution: 'Lab by KillerCoda',
      attributionUrl: 'https://killercoda.com/',
      shortLabel: 'Docker',
    },
  },
  {
    match: /^\/docs\/tools\/terraform(\/|$)/,
    provider: {
      id: 'killercoda-terraform',
      name: 'Terraform Playground',
      url: 'https://killercoda.com/playgrounds/scenario/terraform',
      attribution: 'Lab by KillerCoda',
      attributionUrl: 'https://killercoda.com/',
      shortLabel: 'TF',
    },
  },
  {
    match: /^\/docs\/tools\/ansible(\/|$)/,
    provider: {
      id: 'killercoda-ansible',
      name: 'Ansible Playground',
      url: 'https://killercoda.com/playgrounds/scenario/ansible',
      attribution: 'Lab by KillerCoda',
      attributionUrl: 'https://killercoda.com/',
      shortLabel: 'Ansible',
    },
  },
  {
    match: /^\/docs\/tools\/(linux|bash)(\/|$)/,
    provider: {
      id: 'killercoda-ubuntu',
      name: 'Ubuntu Playground',
      url: 'https://killercoda.com/playgrounds/scenario/ubuntu',
      attribution: 'Lab by KillerCoda',
      attributionUrl: 'https://killercoda.com/',
      shortLabel: 'Linux',
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
