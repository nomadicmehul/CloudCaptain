/**
 * BridgeLayout — Captain's Bridge v2 doc layout.
 *
 * Pure CSS Grid, no floating overlays. Four grid regions:
 *   ┌────────────┬──────────────────────────┬───────────┐
 *   │  header    │        header            │   header  │   <- BridgeTelemetry
 *   ├────────────┼──────────────────────────┼───────────┤
 *   │  gutter    │        main              │   rail    │   <- canvas row
 *   ├────────────┴──────────────────────────┴───────────┤
 *   │                    footer                          │   <- BridgeConsole (fixed)
 *   └────────────────────────────────────────────────────┘
 *
 * State lives here; child components are dumb and get props.
 */
import React, {useEffect, useRef, useState, useCallback} from 'react';
import {useLocation} from '@docusaurus/router';
import {useDoc} from '@docusaurus/plugin-content-docs/client';
import DocItemPaginator from '@theme/DocItem/Paginator';
import DocVersionBanner from '@theme/DocVersionBanner';
import DocVersionBadge from '@theme/DocVersionBadge';
import DocItemFooter from '@theme/DocItem/Footer';
import DocItemTOCMobile from '@theme/DocItem/TOC/Mobile';
import DocItemContent from '@theme/DocItem/Content';
import DocBreadcrumbs from '@theme/DocBreadcrumbs';
import ContentVisibility from '@theme/ContentVisibility';
import type {Props} from '@theme/DocItem/Layout';

import ClassicLayout from './ClassicLayout';
import BridgeTelemetry from './BridgeTelemetry';
import BridgeRail from './BridgeRail';
import BridgeConsole from './BridgeConsole';
import BridgeHelp from './BridgeHelp';
import BridgeGutter from './BridgeGutter';
import ResumeBanner from './ResumeBanner';
import ChapterEnhancements from './ChapterEnhancements';
import CompletionToast from './CompletionToast';
import SidebarSpine from './SidebarSpine';
import {useScrollProgress} from './useScrollProgress';
import {useExtractors} from './useExtractors';
import {useReadProgress} from './useReadProgress';

import styles from './styles.module.css';

const STORAGE_ENABLED = 'cloudcaptain.bridge.enabled';
const STORAGE_FOCUS = 'cloudcaptain.bridge.focus';
const STORAGE_RAIL = 'cloudcaptain.bridge.rail';
const STORAGE_TAB = 'cloudcaptain.bridge.railTab';

type Tab = 'contents' | 'commands';

export default function BridgeLayout({children}: Props): JSX.Element {
  const location = useLocation();
  const {frontMatter} = useDoc();
  const hideToc = frontMatter.hide_table_of_contents === true;

  const articleRef = useRef<HTMLElement | null>(null);
  const [article, setArticle] = useState<HTMLElement | null>(null);
  const setArticleEl = useCallback((el: HTMLElement | null) => {
    articleRef.current = el;
    setArticle(el);
  }, []);

  const [enabled, setEnabled] = useState(true);
  const [focus, setFocus] = useState(false);
  const [railOpen, setRailOpen] = useState(true);
  const [helpOpen, setHelpOpen] = useState(false);
  const [railTab, setRailTab] = useState<Tab>('contents');

  // Hydrate persisted prefs once
  useEffect(() => {
    try {
      const e = localStorage.getItem(STORAGE_ENABLED);
      if (e !== null) setEnabled(e === 'true');
      const f = localStorage.getItem(STORAGE_FOCUS);
      if (f !== null) setFocus(f === 'true');
      const r = localStorage.getItem(STORAGE_RAIL);
      if (r !== null) setRailOpen(r === 'true');
      const t = localStorage.getItem(STORAGE_TAB);
      if (t === 'contents' || t === 'commands') setRailTab(t);
      // Migrate: any previously-persisted 'concepts' value silently resets to 'contents'
    } catch {}
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_ENABLED, String(enabled));
    } catch {}
  }, [enabled]);
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_FOCUS, String(focus));
    } catch {}
  }, [focus]);
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_RAIL, String(railOpen));
    } catch {}
  }, [railOpen]);
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_TAB, railTab);
    } catch {}
  }, [railTab]);

  // Body class hooks for focus mode CSS
  useEffect(() => {
    const b = document.body;
    b.classList.toggle('cc-bridge-on', enabled);
    b.classList.toggle('cc-bridge-focus', focus && enabled);
    return () => {
      b.classList.remove('cc-bridge-on', 'cc-bridge-focus');
    };
  }, [enabled, focus]);

  const {progress, currentSection, totalSections, sections, readingTimeLeft} =
    useScrollProgress(article);
  const {commands} = useExtractors(article);
  const {readSections, toggleRead, isRead, setTotalSections} = useReadProgress(
    location.pathname
  );
  const [toastMsg, setToastMsg] = useState<string | null>(null);

  // Persist total section count so SidebarSpine can classify completion
  useEffect(() => {
    if (totalSections > 0) setTotalSections(totalSections);
  }, [totalSections, setTotalSections]);

  // Fire a toast whenever a new section gets marked read (delta detection)
  const prevReadRef = useRef<string[]>([]);
  useEffect(() => {
    const prev = prevReadRef.current;
    const added = readSections.find((s) => !prev.includes(s));
    if (added && prev.length > 0) {
      // Find the title for a nicer toast
      const sec = sections.find((s) => s.id === added);
      const title = sec?.title ?? 'Chapter';
      if (readSections.length === totalSections && totalSections > 0) {
        setToastMsg(`🏁 Page complete — ${totalSections} / ${totalSections}`);
      } else {
        setToastMsg(`🎉 ${title} complete — ${readSections.length} / ${totalSections}`);
      }
    }
    prevReadRef.current = readSections;
  }, [readSections, sections, totalSections]);


  // Focus-mode DOM observer
  useEffect(() => {
    if (!enabled || !focus || !article) return undefined;
    const nodes = Array.from(
      article.querySelectorAll<HTMLElement>(
        'p, li, pre, blockquote, table, .theme-admonition'
      )
    );
    if (!nodes.length) return undefined;
    let rafId = 0;
    const update = () => {
      const mid = window.innerHeight / 2;
      let best: HTMLElement | null = null;
      let bestDist = Infinity;
      for (const n of nodes) {
        const r = n.getBoundingClientRect();
        const d = Math.abs(r.top + r.height / 2 - mid);
        if (d < bestDist) {
          bestDist = d;
          best = n;
        }
      }
      for (const n of nodes) n.classList.toggle('cc-bridge-focus-active', n === best);
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
      for (const n of nodes) n.classList.remove('cc-bridge-focus-active');
    };
  }, [enabled, focus, article, location.pathname]);

  // Keyboard shortcuts
  const keyBufferRef = useRef('');
  const keyTimerRef = useRef<number | null>(null);
  const goToDoc = useCallback((dir: 'prev' | 'next') => {
    const sel =
      dir === 'prev' ? '.pagination-nav__link--prev' : '.pagination-nav__link--next';
    document.querySelector<HTMLAnchorElement>(sel)?.click();
  }, []);
  useEffect(() => {
    if (!enabled) return undefined;
    const handler = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target as HTMLElement;
      if (
        t &&
        (t.tagName === 'INPUT' ||
          t.tagName === 'TEXTAREA' ||
          (t as HTMLElement).isContentEditable)
      )
        return;
      const k = e.key;
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
        keyTimerRef.current = window.setTimeout(() => (keyBufferRef.current = ''), 500);
        return;
      }
      keyBufferRef.current = '';
      switch (k) {
        case 'j':
          window.scrollBy({top: window.innerHeight * 0.5, behavior: 'smooth'});
          e.preventDefault();
          break;
        case 'k':
          window.scrollBy({top: -window.innerHeight * 0.5, behavior: 'smooth'});
          e.preventDefault();
          break;
        case 'G':
          window.scrollTo({
            top: document.documentElement.scrollHeight,
            behavior: 'smooth',
          });
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
          if (sections.length && currentSection >= 0)
            toggleRead(sections[currentSection]?.id ?? '');
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
        case '/':
          document
            .querySelector<HTMLInputElement>('input.DocSearch-Input, input[type="search"]')
            ?.focus();
          e.preventDefault();
          break;
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [enabled, focus, helpOpen, sections, currentSection, toggleRead, goToDoc]);

  // Find first unread section (for Resume banner)
  const firstUnread =
    readSections.length > 0 && readSections.length < sections.length
      ? sections.findIndex((s) => !isRead(s.id))
      : -1;

  // Fallback: classic layout if disabled
  if (!enabled) {
    return (
      <div className={styles.classicWrap}>
        <ClassicLayout>{children}</ClassicLayout>
        <button
          type="button"
          className={styles.fab}
          onClick={() => setEnabled(true)}
          aria-label="Enable Captain's Bridge reading mode"
          title="Captain's Bridge">
          ⚓
        </button>
      </div>
    );
  }

  return (
    <div
      className={`${styles.canvas} ${railOpen && !hideToc ? styles.canvasWithRail : ''}`}>
      <header className={styles.canvasHeader}>
        <BridgeTelemetry
          progress={progress}
          currentSection={currentSection}
          totalSections={totalSections}
          readingTimeLeft={readingTimeLeft}
          focus={focus}
          railOpen={railOpen && !hideToc}
          onToggleFocus={() => setFocus((v) => !v)}
          onToggleRail={() => setRailOpen((v) => !v)}
          onDisable={() => setEnabled(false)}
          onHelp={() => setHelpOpen(true)}
        />
      </header>

      <aside className={styles.canvasGutter} aria-hidden="true">
        <BridgeGutter progress={progress} sections={sections} isRead={isRead} />
      </aside>

      <main className={styles.canvasMain}>
        <DocVersionBanner />
        <div className={styles.mainInner}>
          <article ref={setArticleEl as any}>
            <DocBreadcrumbs />
            <DocVersionBadge />
            <DocItemTOCMobile />
            {firstUnread > 0 && (
              <ResumeBanner
                section={sections[firstUnread]}
                readCount={readSections.length}
                total={sections.length}
              />
            )}
            <DocItemContent>{children}</DocItemContent>
            {article && (
              <ChapterEnhancements
                article={article}
                sections={sections}
                isRead={isRead}
                onToggleRead={toggleRead}
              />
            )}
            <DocItemFooter />
          </article>
          <ContentVisibility metadata={useDoc().metadata} />
          <DocItemPaginator />
        </div>
      </main>

      {railOpen && !hideToc && (
        <aside className={styles.canvasRail}>
          <BridgeRail
            sections={sections}
            commands={commands}
            activeTab={railTab}
            onTabChange={setRailTab}
            currentSection={currentSection}
            isRead={isRead}
            onToggleRead={toggleRead}
          />
        </aside>
      )}

      <footer className={styles.canvasFooter}>
        <BridgeConsole
          onMarkRead={() => {
            if (sections.length && currentSection >= 0)
              toggleRead(sections[currentSection]?.id ?? '');
          }}
          onPrev={() => goToDoc('prev')}
          onNext={() => goToDoc('next')}
          onFocus={() => setFocus((v) => !v)}
          onHelp={() => setHelpOpen(true)}
          readCount={readSections.length}
          totalSections={totalSections}
        />
      </footer>

      {helpOpen && <BridgeHelp onClose={() => setHelpOpen(false)} />}

      {/* Cross-page sidebar progress dots */}
      <SidebarSpine />

      {/* Transient toast when a section gets marked complete */}
      {toastMsg && (
        <CompletionToast message={toastMsg} onDone={() => setToastMsg(null)} />
      )}
    </div>
  );
}
