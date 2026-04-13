import {useCallback, useEffect, useState} from 'react';

const STORAGE_PREFIX = 'cloudcaptain.bridge.read.';

type State = {
  readSections: string[];
  totalSections?: number;
  lastUpdated?: number;
};

export type ReadEntry = {
  readCount: number;
  totalSections?: number;
  lastUpdated?: number;
};

export function useReadProgress(pathname: string) {
  const key = STORAGE_PREFIX + pathname;
  const [state, setState] = useState<State>({readSections: []});

  useEffect(() => {
    try {
      const raw = localStorage.getItem(key);
      if (raw) {
        const parsed = JSON.parse(raw);
        setState({
          readSections: parsed.readSections ?? [],
          totalSections: parsed.totalSections,
          lastUpdated: parsed.lastUpdated,
        });
      } else {
        setState({readSections: []});
      }
    } catch {
      setState({readSections: []});
    }
  }, [key]);

  const persist = useCallback(
    (next: State) => {
      setState(next);
      try {
        localStorage.setItem(key, JSON.stringify(next));
        // Broadcast to other components (e.g. SidebarSpine) without waiting
        // for the storage event (which doesn't fire in same-window writes).
        window.dispatchEvent(
          new CustomEvent('cc-bridge-read-change', {detail: {key, next}})
        );
      } catch {}
    },
    [key]
  );

  const toggleRead = useCallback(
    (sectionId: string) => {
      if (!sectionId) return;
      setState((prev) => {
        const exists = prev.readSections.includes(sectionId);
        const next: State = {
          readSections: exists
            ? prev.readSections.filter((s) => s !== sectionId)
            : [...prev.readSections, sectionId],
          totalSections: prev.totalSections,
          lastUpdated: Date.now(),
        };
        try {
          localStorage.setItem(key, JSON.stringify(next));
          window.dispatchEvent(
            new CustomEvent('cc-bridge-read-change', {detail: {key, next}})
          );
        } catch {}
        return next;
      });
    },
    [key]
  );

  const setTotalSections = useCallback(
    (total: number) => {
      setState((prev) => {
        if (prev.totalSections === total) return prev;
        const next: State = {...prev, totalSections: total};
        try {
          localStorage.setItem(key, JSON.stringify(next));
        } catch {}
        return next;
      });
    },
    [key]
  );

  const isRead = useCallback(
    (id: string) => state.readSections.includes(id),
    [state.readSections]
  );

  return {
    readSections: state.readSections,
    totalSections: state.totalSections,
    toggleRead,
    setTotalSections,
    isRead,
    clear: () => persist({readSections: []}),
  };
}

/**
 * Read all persisted read-progress entries out of localStorage.
 * Used by the SidebarSpine to annotate sidebar links with progress dots.
 */
export function getAllReadEntries(): Map<string, ReadEntry> {
  const out = new Map<string, ReadEntry>();
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const k = localStorage.key(i);
      if (!k || !k.startsWith(STORAGE_PREFIX)) continue;
      const pathname = k.slice(STORAGE_PREFIX.length);
      const raw = localStorage.getItem(k);
      if (!raw) continue;
      try {
        const parsed = JSON.parse(raw);
        const readCount = Array.isArray(parsed.readSections)
          ? parsed.readSections.length
          : 0;
        if (readCount > 0 || parsed.totalSections) {
          out.set(pathname, {
            readCount,
            totalSections: parsed.totalSections,
            lastUpdated: parsed.lastUpdated,
          });
        }
      } catch {}
    }
  } catch {}
  return out;
}
