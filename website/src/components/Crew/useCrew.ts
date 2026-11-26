import {useEffect, useState} from 'react';
import {buildRoster} from './crewCore.mjs';
import type {Member} from './CrewCard';

const CACHE_KEY = 'cloudcaptain.crew.pulls.v1';
const CACHE_MS = 10 * 60 * 1000;
const MAX_PAGES = 10;

type State =
  | {status: 'loading'}
  | {status: 'error'; message: string}
  | {status: 'ready'; roster: Map<string, Member>};

function readCache(): unknown[] | null {
  try {
    const raw = sessionStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const {at, pulls} = JSON.parse(raw);
    return Date.now() - at < CACHE_MS && Array.isArray(pulls) ? pulls : null;
  } catch {
    return null;
  }
}

function writeCache(pulls: unknown[]): void {
  try {
    sessionStorage.setItem(CACHE_KEY, JSON.stringify({at: Date.now(), pulls}));
  } catch {
    // storage full or blocked: the page still works, it just refetches next time
  }
}

/** Keep only the fields the roster needs, so the cache stays small. */
function slim(p: any) {
  return {
    number: p.number,
    title: p.title,
    merged_at: p.merged_at,
    user: p.user && {login: p.user.login, type: p.user.type, avatar_url: p.user.avatar_url},
    labels: (p.labels || []).map((l: any) => l.name),
  };
}

async function fetchPulls(repo: string, signal: AbortSignal): Promise<unknown[]> {
  const all: unknown[] = [];
  for (let page = 1; page <= MAX_PAGES; page++) {
    const res = await fetch(`https://api.github.com/repos/${repo}/pulls?state=closed&per_page=100&page=${page}`, {
      signal,
      headers: {Accept: 'application/vnd.github+json'},
    });
    if (res.status === 403 || res.status === 429) {
      throw new Error("GitHub's hourly limit for anonymous requests is used up on this network. Try again in a little while.");
    }
    if (!res.ok) throw new Error(`GitHub answered ${res.status}. Try again in a moment.`);
    const batch = await res.json();
    all.push(...batch.filter((p: any) => p.merged_at).map(slim));
    if (batch.length < 100) break;
  }
  return all;
}

/** The full crew roster, built from the repo's merged pull requests. */
export function useCrewRoster(repo: string, config: unknown): State {
  const [state, setState] = useState<State>({status: 'loading'});
  useEffect(() => {
    const controller = new AbortController();
    const cached = readCache();
    const load = cached ? Promise.resolve(cached) : fetchPulls(repo, controller.signal).then((p) => (writeCache(p), p));
    load
      .then((pulls) => setState({status: 'ready', roster: buildRoster(pulls, config) as Map<string, Member>}))
      .catch((e: Error) => {
        if (e.name !== 'AbortError') setState({status: 'error', message: e.message});
      });
    return () => controller.abort();
  }, [repo, config]);
  return state;
}

/** The avatar as a data: URL (GitHub serves avatars with CORS), or null if it can't be loaded. */
export function useAvatarData(avatarUrl: string | undefined): string | null {
  const [data, setData] = useState<string | null>(null);
  useEffect(() => {
    setData(null);
    if (!avatarUrl) return undefined;
    let alive = true;
    const src = `${avatarUrl}${avatarUrl.includes('?') ? '&' : '?'}s=344`;
    fetch(src)
      .then((r) => (r.ok ? r.blob() : Promise.reject(new Error(String(r.status)))))
      .then(
        (blob) =>
          new Promise<string>((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(String(reader.result));
            reader.onerror = () => reject(reader.error);
            reader.readAsDataURL(blob);
          }),
      )
      .then((url) => alive && setData(url))
      .catch(() => alive && setData(null));
    return () => {
      alive = false;
    };
  }, [avatarUrl]);
  return data;
}
