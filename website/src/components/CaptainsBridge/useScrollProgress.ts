import {useEffect, useState} from 'react';

export type Section = {
  id: string;
  title: string;
  level: number;
  top: number;
};

export type ScrollState = {
  progress: number; // 0..1
  currentSection: number; // index into sections[]
  totalSections: number;
  sections: Section[];
  readingTimeLeft: number; // minutes
};

const WORDS_PER_MINUTE = 220;

export function useScrollProgress(article: HTMLElement | null): ScrollState {
  const [state, setState] = useState<ScrollState>({
    progress: 0,
    currentSection: -1,
    totalSections: 0,
    sections: [],
    readingTimeLeft: 0,
  });

  useEffect(() => {
    if (!article) return undefined;

    const computeSections = (): Section[] => {
      const headings = Array.from(
        article.querySelectorAll<HTMLElement>('h2, h3')
      ).filter((h) => h.id);
      return headings.map((h) => ({
        id: h.id,
        title: h.textContent?.replace(/#$/, '').trim() ?? '',
        level: h.tagName === 'H2' ? 2 : 3,
        top: h.offsetTop,
      }));
    };

    let sections = computeSections();
    let totalWords =
      (article.textContent ?? '').split(/\s+/).filter(Boolean).length;

    const compute = () => {
      const scrollTop =
        window.scrollY || document.documentElement.scrollTop;
      const viewport = window.innerHeight;
      const articleTop = article.getBoundingClientRect().top + scrollTop;
      const articleHeight = article.scrollHeight;

      const scrolled = Math.max(0, scrollTop - articleTop + viewport * 0.3);
      const progress = Math.min(1, Math.max(0, scrolled / articleHeight));

      let currentSection = -1;
      const probe = scrollTop + viewport * 0.3;
      for (let i = 0; i < sections.length; i += 1) {
        const s = sections[i];
        const top = articleTop + s.top - article.offsetTop;
        if (probe >= top) currentSection = i;
      }

      const readingTimeTotal = totalWords / WORDS_PER_MINUTE;
      const readingTimeLeft = Math.max(
        0,
        Math.round(readingTimeTotal * (1 - progress))
      );

      setState({
        progress,
        currentSection,
        totalSections: sections.length,
        sections,
        readingTimeLeft,
      });
    };

    let rafId = 0;
    const onScroll = () => {
      cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(compute);
    };
    const onResize = () => {
      sections = computeSections();
      totalWords =
        (article.textContent ?? '').split(/\s+/).filter(Boolean).length;
      compute();
    };

    // Recompute after layout settles (images/fonts)
    const settleTimer = window.setTimeout(onResize, 400);

    compute();
    window.addEventListener('scroll', onScroll, {passive: true});
    window.addEventListener('resize', onResize, {passive: true});
    return () => {
      cancelAnimationFrame(rafId);
      window.clearTimeout(settleTimer);
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onResize);
    };
  }, [article]);

  return state;
}
