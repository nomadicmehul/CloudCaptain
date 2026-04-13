import {useCallback, useEffect, useState} from 'react';

const STORAGE_PREFIX = 'cloudcaptain.bridge.read.';

type State = {
  readSections: string[];
};

export function useReadProgress(pathname: string) {
  const key = STORAGE_PREFIX + pathname;
  const [state, setState] = useState<State>({readSections: []});

  useEffect(() => {
    try {
      const raw = localStorage.getItem(key);
      if (raw) setState(JSON.parse(raw));
      else setState({readSections: []});
    } catch {
      setState({readSections: []});
    }
  }, [key]);

  const persist = useCallback(
    (next: State) => {
      setState(next);
      try {
        localStorage.setItem(key, JSON.stringify(next));
      } catch {}
    },
    [key]
  );

  const toggleRead = useCallback(
    (sectionId: string) => {
      if (!sectionId) return;
      setState((prev) => {
        const exists = prev.readSections.includes(sectionId);
        const next = {
          readSections: exists
            ? prev.readSections.filter((s) => s !== sectionId)
            : [...prev.readSections, sectionId],
        };
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
    toggleRead,
    isRead,
    clear: () => persist({readSections: []}),
  };
}
