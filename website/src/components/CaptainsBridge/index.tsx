/**
 * CaptainsBridge — a novel reading experience for CloudCaptain docs.
 *
 * Renders three elements as fixed siblings to the doc layout:
 *  - BridgeTelemetry    (top strip: progress %, reading time, section X/Y, toggle)
 *  - BridgeRail         (right rail: Contents / Commands / Concepts auto-extracted)
 *  - BridgeConsole      (bottom: vim-style shortcuts, prev/next, mark-read)
 *
 * All state is client-only and persists in localStorage.
 * Users can disable Bridge to fall back to the Classic Docusaurus layout.
 */
import React, {useEffect, useMemo, useRef, useState, useCallback} from 'react';
import {useLocation} from '@docusaurus/router';
import BridgeTelemetry from './BridgeTelemetry';
import BridgeRail from './BridgeRail';
import BridgeConsole from './BridgeConsole';
import BridgeHelp from './BridgeHelp';
import {useScrollProgress} from './useScrollProgress';
import {useExtractors} from './useExtractors';
import {useReadProgress} from './useReadProgress';
import styles from './styles.module.css';

const STORAGE_KEY_ENABLED = 'cloudcaptain.bridge.enabled';
const STORAGE_KEY_FOCUS = 'cloudcaptain.bridge.focus';
const STORAGE_KEY_RAIL = 'cloudcaptain.bridge.rail';

export default function CaptainsBridge(): JSX.Element | null {
  const location = useLocation();
  const [mounted, setMounted] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [focus, setFocus] = useState(false);
  const [railOpen, setRailOpen] = useState(true);
  const [helpOpen, setHelpOpen] = useState(false);
  const [railTab, setRailTab] = useState<'contents' | 'commands' | 'concepts'>('contents');

  const article = useArticleElement(location.pathname);
  const {progress, currentSection, totalSections, sections, readingTimeLeft} =
    useScrollProgress(article);
  const {commands, concepts} = useExtractors(article);
  const {readSections, toggleRead, isRead} = useReadProgress(location.pathname);

  // Hydrate persisted prefs
  useEffect(() => {
    setMounted(true);
    try {
      const e = localStorage.getItem(STORAGE_KEY_ENABLED);
      if (e !== null) setEnabled(e === 'true');
      const f = localStorage.getItem(STORAGE_KEY_FOCUS);
      if (f !== null) setFocus(f === 'true');
      const r = localStorage.getItem(STORAGE_KEY_RAIL);
      if (r !== null) setRailOpen(r === 'true');
    } catch {}
  }, []);

  // Persist prefs
  useEffect(() => {
    if (mounted) localStorage.setItem(STORAGE_KEY_ENABLED, String(enabled));
  }, [enabled, mounted]);
  useEffect(() => {
    if (mounted) localStorage.setItem(STORAGE_KEY_FOCUS, String(focus));
  }, [focus, mounted]);
  useEffect(() => {
    if (mounted) localStorage.setItem(STORAGE_KEY_RAIL, String(railOpen));
  }, [railOpen, mounted]);

  // Apply body class for focus / enabled state so CSS can react
  useEffect(() => {
    const body = document.body;
    body.classList.toggle('cc-bridge-enabled', enabled && mounted);
    body.classList.toggle('cc-bridge-focus', focus && enabled && mounted);
    body.classList.toggle('cc-bridge-rail-open', enabled && mounted && railOpen);
    return () => {
      body.classList.remove('cc-bridge-enabled', 'cc-bridge-focus', 'cc-bridge-rail-open');
    };
  }, [enabled, focus, railOpen, mounted]);

  // Focus-mode paragraph dimming: track viewport-center paragraph
  useEffect(() => {
    if (!enabled || !focus || !article) return undefined;
    const paragraphs = Array.from(article.querySelectorAll<HTMLElement>('p, li, pre, blockquote, table'));
    if (!paragraphs.length) return undefined;

    let rafId = 0;
    const update = () => {
      const mid = window.innerHeight / 2;
      let best: HTMLElement | null = null;
      let bestDist = Infinity;
      for (const p of paragraphs) {
        const rect = p.getBoundingClientRect();
        const dist = Math.abs(rect.top + rect.height / 2 - mid);
        if (dist < bestDist) {
          bestDist = dist;
          best = p;
        }
      }
      for (const p of paragraphs) p.classList.toggle('cc-bridge-focus-active', p === best);
    };
    const onScroll = () => {
      cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(update);
    };
    update();
    window.addEventListener('scroll', onScroll, {passive: true});
    window.addEventListener('resize', onScroll, {passive: true});
    return () => {
      cancelAnimationFrame(rafId);
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('resize', onScroll);
      for (const p of paragraphs) p.classList.remove('cc-bridge-focus-active');
    };
  }, [enabled, focus, article, location.pathname]);

  // Keyboard shortcuts (vim-ish)
  const keyBufferRef = useRef<string>('');
  const keyTimerRef = useRef<number | null>(null);
  const scrollBy = useCallback((amount: number) => {
    window.scrollBy({top: amount, behavior: 'smooth'});
  }, []);

  const goToDoc = useCallback((direction: 'prev' | 'next') => {
    const sel = direction === 'prev' ? '.pagination-nav__link--prev' : '.pagination-nav__link--next';
    const link = document.querySelector<HTMLAnchorElement>(sel);
    if (link) link.click();
  }, []);

  useEffect(() => {
    if (!enabled) return undefined;
    const handler = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const target = e.target as HTMLElement;
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) return;

      const k = e.key;
      // Buffered keys for gg
      if (k === 'g') {
        if (keyBufferRef.current === 'g') {
          window.scrollTo({top: 0, behavior: 'smooth'});
          keyBufferRef.current = '';
          if (keyTimerRef.current) window.clearTimeout(keyTimerRef.current);
          e.preventDefault();
          return;
        }
        keyBufferRef.current = 'g';
        if (keyTimerRef.current) window.clearTimeout(keyTimerRef.current);
        keyTimerRef.current = window.setTimeout(() => {
          keyBufferRef.current = '';
        }, 500);
        return;
      }
      keyBufferRef.current = '';
      switch (k) {
        case 'j':
          scrollBy(window.innerHeight * 0.5);
          e.preventDefault();
          break;
        case 'k':
          scrollBy(-window.innerHeight * 0.5);
          e.preventDefault();
          break;
        case 'G':
          window.scrollTo({top: document.documentElement.scrollHeight, behavior: 'smooth'});
          e.preventDefault();
          break;
        case 'f':
          setFocus((v) => !v);
          e.preventDefault();
          break;
        case 't':
          setRailOpen((v) => !v);
          e.preventDefault();
          break;
        case '?':
          setHelpOpen((v) => !v);
          e.preventDefault();
          break;
        case 'Escape':
          if (helpOpen) setHelpOpen(false);
          else if (focus) setFocus(false);
          break;
        case 'm':
          if (sections.length && currentSection >= 0) {
            toggleRead(sections[currentSection]?.id ?? '');
          }
          e.preventDefault();
          break;
        case 'n':
          goToDoc('next');
          e.preventDefault();
          break;
        case 'p':
          goToDoc('prev');
          e.preventDefault();
          break;
        case '/': {
          const search = document.querySelector<HTMLInputElement>(
            'input.DocSearch-Input, input[type="search"]'
          );
          if (search) {
            search.focus();
            e.preventDefault();
          }
          break;
        }
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [enabled, focus, helpOpen, sections, currentSection, toggleRead, scrollBy, goToDoc]);

  if (!mounted) return null;
  if (!enabled) {
    // Render only a tiny FAB to re-enable Bridge
    return (
      <button
        type="button"
        className={styles.fab}
        onClick={() => setEnabled(true)}
        aria-label="Enable Captain's Bridge reading mode"
        title="Enable Captain's Bridge (⚓)">
        ⚓
      </button>
    );
  }

  return (
    <>
      <BridgeTelemetry
        progress={progress}
        currentSection={currentSection}
        totalSections={totalSections}
        readingTimeLeft={readingTimeLeft}
        focus={focus}
        railOpen={railOpen}
        onToggleFocus={() => setFocus((v) => !v)}
        onToggleRail={() => setRailOpen((v) => !v)}
        onDisable={() => setEnabled(false)}
        onHelp={() => setHelpOpen(true)}
      />
      {railOpen && (
        <BridgeRail
          sections={sections}
          commands={commands}
          concepts={concepts}
          activeTab={railTab}
          onTabChange={setRailTab}
          currentSection={currentSection}
          isRead={isRead}
          onToggleRead={toggleRead}
        />
      )}
      <BridgeConsole
        onMarkRead={() => {
          if (sections.length && currentSection >= 0) {
            toggleRead(sections[currentSection]?.id ?? '');
          }
        }}
        onPrev={() => goToDoc('prev')}
        onNext={() => goToDoc('next')}
        onFocus={() => setFocus((v) => !v)}
        onHelp={() => setHelpOpen(true)}
        readCount={readSections.length}
        totalSections={totalSections}
      />
      {helpOpen && <BridgeHelp onClose={() => setHelpOpen(false)} />}
    </>
  );
}

// Utility hook: find the <article> element after route change
function useArticleElement(pathname: string): HTMLElement | null {
  const [el, setEl] = useState<HTMLElement | null>(null);
  useEffect(() => {
    // Wait a tick for Docusaurus to render the article
    let cancelled = false;
    let attempts = 0;
    const find = () => {
      if (cancelled) return;
      const article = document.querySelector<HTMLElement>('article') ||
        document.querySelector<HTMLElement>('.theme-doc-markdown');
      if (article) {
        setEl(article);
        return;
      }
      attempts += 1;
      if (attempts < 30) setTimeout(find, 50);
    };
    find();
    return () => {
      cancelled = true;
    };
  }, [pathname]);
  return el;
}
