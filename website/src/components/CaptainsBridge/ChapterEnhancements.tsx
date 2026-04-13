/**
 * ChapterEnhancements — enriches every H2 in the article with:
 *   1. A "chapter card" BEFORE the heading:
 *         ○/◐/●  Section title             4 min · beginner
 *      The bubble reflects read state (unread / current / read).
 *   2. A "Mark section complete" button AFTER the last block of that H2.
 *
 * Works via React portals into sibling DOM nodes that we inject once per
 * h2 and keep stable across re-renders. The markdown itself is untouched.
 */
import React, {useEffect, useMemo, useState} from 'react';
import {createPortal} from 'react-dom';
import styles from './styles.module.css';
import type {Section} from './useScrollProgress';

type Props = {
  article: HTMLElement;
  sections: Section[];
  isRead: (id: string) => boolean;
  onToggleRead: (id: string) => void;
};

type ChapterData = {
  id: string;
  title: string;
  cardHost: HTMLElement; // inserted BEFORE the h2
  buttonHost: HTMLElement; // inserted AFTER the last block of the section
  wordCount: number;
  codeBlocks: number;
  inlineCodes: number;
};

const WORDS_PER_MIN = 220;

function classifyDifficulty(
  wordCount: number,
  codeBlocks: number,
  inlineCodes: number
): {label: string; dots: string} {
  // Heuristic: density of code vs prose, plus length.
  const score = codeBlocks * 2 + inlineCodes * 0.15 + wordCount / 400;
  if (score < 4) return {label: 'beginner', dots: '●○○'};
  if (score < 10) return {label: 'intermediate', dots: '●●○'};
  return {label: 'advanced', dots: '●●●'};
}

function buildChapters(article: HTMLElement): ChapterData[] {
  const h2s = Array.from(article.querySelectorAll<HTMLHeadingElement>('h2[id]'));
  if (!h2s.length) return [];
  const result: ChapterData[] = [];
  for (let i = 0; i < h2s.length; i += 1) {
    const h2 = h2s[i];
    const next = h2s[i + 1] ?? null;

    // Scope: all siblings between this h2 and next h2 (same parent chain)
    const scopeNodes: HTMLElement[] = [];
    let cur: Element | null = h2.nextElementSibling;
    while (cur && cur !== next) {
      if (cur instanceof HTMLElement) scopeNodes.push(cur);
      cur = cur.nextElementSibling;
    }

    const textContent = scopeNodes.map((n) => n.textContent ?? '').join(' ');
    const wordCount = textContent.split(/\s+/).filter(Boolean).length;
    const codeBlocks = scopeNodes.reduce(
      (acc, n) => acc + n.querySelectorAll('pre').length + (n.tagName === 'PRE' ? 1 : 0),
      0
    );
    const inlineCodes = scopeNodes.reduce(
      (acc, n) =>
        acc +
        Array.from(n.querySelectorAll('code')).filter((c) => !c.closest('pre')).length,
      0
    );

    // Create/reuse hosts
    let cardHost = h2.previousElementSibling as HTMLElement | null;
    if (!cardHost || !cardHost.classList?.contains('cc-bridge-card-host')) {
      cardHost = document.createElement('div');
      cardHost.className = 'cc-bridge-card-host';
      h2.parentNode?.insertBefore(cardHost, h2);
    }

    // Button host: insert AFTER last scope node, or right before next h2
    let buttonHost: HTMLElement | null = null;
    if (scopeNodes.length > 0) {
      const lastNode = scopeNodes[scopeNodes.length - 1];
      const maybeHost = lastNode.nextElementSibling as HTMLElement | null;
      if (maybeHost && maybeHost.classList?.contains('cc-bridge-btn-host')) {
        buttonHost = maybeHost;
      } else {
        buttonHost = document.createElement('div');
        buttonHost.className = 'cc-bridge-btn-host';
        lastNode.parentNode?.insertBefore(buttonHost, lastNode.nextSibling);
      }
    } else {
      // No content under this h2, skip button
      const placeholder = document.createElement('div');
      placeholder.className = 'cc-bridge-btn-host';
      buttonHost = placeholder;
      h2.parentNode?.insertBefore(placeholder, next);
    }

    result.push({
      id: h2.id,
      title: h2.textContent?.replace(/#$/, '').trim() ?? '',
      cardHost,
      buttonHost,
      wordCount,
      codeBlocks,
      inlineCodes,
    });
  }
  return result;
}

export default function ChapterEnhancements({
  article,
  sections,
  isRead,
  onToggleRead,
}: Props): JSX.Element | null {
  const [chapters, setChapters] = useState<ChapterData[]>([]);
  const sectionIdsKey = useMemo(
    () =>
      sections
        .filter((s) => s.level === 2)
        .map((s) => s.id)
        .join('|'),
    [sections]
  );

  useEffect(() => {
    if (!article) return undefined;
    // Let the page finish painting before we measure and inject
    let cancelled = false;
    const t = window.setTimeout(() => {
      if (cancelled) return;
      const built = buildChapters(article);
      setChapters(built);
    }, 120);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
      // Cleanup hosts we injected — leave next effect to rebuild if needed
      article
        .querySelectorAll('.cc-bridge-card-host, .cc-bridge-btn-host')
        .forEach((n) => n.parentNode?.removeChild(n));
    };
  }, [article, sectionIdsKey]);

  if (!chapters.length) return null;

  return (
    <>
      {chapters.map((ch) => {
        const read = isRead(ch.id);
        const minutes = Math.max(1, Math.round(ch.wordCount / WORDS_PER_MIN));
        const diff = classifyDifficulty(ch.wordCount, ch.codeBlocks, ch.inlineCodes);

        return (
          <React.Fragment key={ch.id}>
            {createPortal(
              <div className={`${styles.chapterCard} ${read ? styles.chapterCardRead : ''}`}>
                <button
                  type="button"
                  className={styles.chapterBubble}
                  onClick={() => onToggleRead(ch.id)}
                  aria-label={read ? 'Mark chapter unread' : 'Mark chapter read'}
                  title={read ? 'Mark chapter unread' : 'Mark chapter read'}>
                  {read ? '●' : '○'}
                </button>
                <div className={styles.chapterMeta}>
                  <span className={styles.chapterEta}>⏱ {minutes} min</span>
                  <span className={styles.chapterDot}>·</span>
                  <span className={styles.chapterDiff} data-level={diff.label}>
                    <span className={styles.chapterDiffDots}>{diff.dots}</span> {diff.label}
                  </span>
                  {ch.codeBlocks > 0 && (
                    <>
                      <span className={styles.chapterDot}>·</span>
                      <span className={styles.chapterCode}>
                        {ch.codeBlocks} code block{ch.codeBlocks === 1 ? '' : 's'}
                      </span>
                    </>
                  )}
                </div>
              </div>,
              ch.cardHost
            )}
            {createPortal(
              <button
                type="button"
                className={`${styles.markBtn} ${read ? styles.markBtnRead : ''}`}
                onClick={() => onToggleRead(ch.id)}>
                {read ? (
                  <>
                    <span className={styles.markBtnIcon}>✓</span> Section complete —{' '}
                    <em>click to reopen</em>
                  </>
                ) : (
                  <>
                    <span className={styles.markBtnIcon}>✓</span> Mark section complete
                  </>
                )}
              </button>,
              ch.buttonHost
            )}
          </React.Fragment>
        );
      })}
    </>
  );
}
