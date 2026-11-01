import type { JSX } from 'react';
import React, {useState} from 'react';
import styles from './styles.module.css';
import type {Section} from './useScrollProgress';
import type {Command} from './useExtractors';
import Link from '@docusaurus/Link';
import cstyles from '../Curriculum/styles.module.css';
import type {LessonMetaData} from '../Curriculum/types';
import type {QuizRecord} from '../Curriculum/storage';
import {githubUrl} from '../Curriculum/useLesson';

function downloadShellScript(commands: Command[]): void {
  const pageTitle = (document.title || 'cloudcaptain').replace(/\s*\|.*$/, '').trim();
  const url = typeof window !== 'undefined' ? window.location.href : '';
  const date = new Date().toISOString();
  const header =
    `#!/usr/bin/env bash\n` +
    `# CloudCaptain — extracted commands\n` +
    `# Page:  ${pageTitle}\n` +
    (url ? `# URL:   ${url}\n` : '') +
    `# Saved: ${date}\n` +
    `#\n` +
    `# Auto-extracted from the page's code blocks. Review before running.\n` +
    `set -euo pipefail\n\n`;
  const body = commands.map((c) => `# (${c.language})\n${c.text}\n`).join('\n');
  const blob = new Blob([header + body], {type: 'text/x-shellscript;charset=utf-8'});
  const href = URL.createObjectURL(blob);
  const safeTitle = pageTitle
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 48) || 'cloudcaptain';
  const a = document.createElement('a');
  a.href = href;
  a.download = `${safeTitle}-commands.sh`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}

function copyAll(commands: Command[], copy: (t: string) => void): void {
  copy(commands.map((c) => c.text).join('\n'));
}

type Tab = 'contents' | 'commands' | 'lesson';

type Props = {
  lesson?: LessonMetaData;
  quiz?: QuizRecord | null;
  sections: Section[];
  commands: Command[];
  activeTab: Tab;
  onTabChange: (t: Tab) => void;
  currentSection: number;
  isRead: (id: string) => boolean;
  onToggleRead: (id: string) => void;
};

export default function BridgeRail({
  sections,
  commands,
  activeTab,
  onTabChange,
  currentSection,
  isRead,
  onToggleRead,
  lesson,
  quiz,
}: Props): JSX.Element {
  const [copied, setCopied] = useState<string | null>(null);

  const copy = (text: string) => {
    try {
      navigator.clipboard.writeText(text);
      setCopied(text);
      window.setTimeout(() => setCopied(null), 1200);
    } catch {}
  };

  return (
    <aside className={styles.rail} aria-label="Captain's Bridge rail">
      <div className={styles.railTabs} role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === 'contents'}
          className={`${styles.railTab} ${activeTab === 'contents' ? styles.railTabActive : ''}`}
          onClick={() => onTabChange('contents')}>
          Contents
          <span className={styles.railTabCount}>{sections.length}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={activeTab === 'commands'}
          className={`${styles.railTab} ${activeTab === 'commands' ? styles.railTabActive : ''}`}
          onClick={() => onTabChange('commands')}>
          Commands
          <span className={styles.railTabCount}>{commands.length}</span>
        </button>
        {lesson && (
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'lesson'}
            className={`${styles.railTab} ${activeTab === 'lesson' ? styles.railTabActive : ''}`}
            onClick={() => onTabChange('lesson')}>
            Lesson
            <span className={styles.railTabCount}>{lesson.outputs.length}</span>
          </button>
        )}
      </div>

      <div className={styles.railBody}>
        {activeTab === 'lesson' && lesson && (
          <div style={{padding: '0 14px 14px', fontSize: 13, lineHeight: 1.5}}>
            <div style={{marginBottom: 10}}>
              <span className={cstyles.badge} data-type={lesson.type}>{lesson.type}</span>{' '}
              <span className={cstyles.muted}>{lesson.minutes} min · Phase {lesson.phaseNumber} {lesson.phaseTitle}</span>
            </div>
            <div style={{fontWeight: 700, marginTop: 10}}>Quiz</div>
            <div className={cstyles.muted}>
              {(['pre', 'check', 'post'] as const).map((stage) => {
                const s = quiz?.scores?.[stage];
                return (
                  <div key={stage}>
                    {stage}: {s ? `${s[0]} / ${s[1]}` : 'not taken'}
                  </div>
                );
              })}
            </div>
            <div style={{fontWeight: 700, marginTop: 10}}>Prerequisites</div>
            <div className={cstyles.muted}>
              {lesson.prereqs.length === 0 && 'None'}
              {lesson.prereqs.map((id) => (
                <div key={id}>
                  <Link to={`/learn/${id}`}>{id}</Link>
                </div>
              ))}
            </div>
            {lesson.tools && lesson.tools.length > 0 && (
              <>
                <div style={{fontWeight: 700, marginTop: 10}}>Tools</div>
                <div className={cstyles.muted}>{lesson.tools.join(', ')}</div>
              </>
            )}
            <div style={{fontWeight: 700, marginTop: 10}}>Tests</div>
            <div className={cstyles.muted}>
              {Object.entries(lesson.tests || {}).map(([lang, cmd]) => (
                <code key={lang} style={{display: 'block', fontSize: 11, whiteSpace: 'normal', wordBreak: 'break-all'}}>{cmd}</code>
              ))}
              <a href={githubUrl(lesson, 'code', true)} target="_blank" rel="noopener noreferrer">
                code on GitHub ↗
              </a>
            </div>
            <div style={{fontWeight: 700, marginTop: 10}}>Artifacts</div>
            <div className={cstyles.muted}>
              {lesson.outputs.map((name) => (
                <div key={name}>
                  <a href={githubUrl(lesson, `outputs/${name}`, name.endsWith('/'))} target="_blank" rel="noopener noreferrer">
                    {name}
                  </a>
                </div>
              ))}
            </div>
            {lesson.reference && lesson.reference.length > 0 && (
              <>
                <div style={{fontWeight: 700, marginTop: 10}}>Reference</div>
                <div className={cstyles.muted}>
                  {lesson.reference.map((r) => (
                    <div key={r}>
                      <Link to={r}>{r.replace('/docs/', '')}</Link>
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        )}
        {activeTab === 'contents' && (
          <ol className={styles.railList}>
            {sections.length === 0 && (
              <li className={styles.railEmpty}>No sections detected on this page.</li>
            )}
            {sections.map((s, i) => {
              const read = isRead(s.id);
              const active = i === currentSection;
              return (
                <li
                  key={s.id}
                  className={`${styles.railItem} ${styles[`railLevel${s.level}`]} ${
                    active ? styles.railItemActive : ''
                  } ${read ? styles.railItemRead : ''}`}>
                  <button
                    type="button"
                    className={styles.railReadToggle}
                    onClick={() => onToggleRead(s.id)}
                    aria-label={read ? 'Mark as unread' : 'Mark as read'}
                    title={read ? 'Mark as unread' : 'Mark as read'}>
                    {read ? '✓' : '○'}
                  </button>
                  <a href={`#${s.id}`} className={styles.railLink}>
                    {s.title || s.id}
                  </a>
                </li>
              );
            })}
          </ol>
        )}

        {activeTab === 'commands' && (
          <>
            {commands.length > 0 && (
              <div className={styles.railToolbar}>
                <button
                  type="button"
                  className={styles.railToolBtn}
                  onClick={() => downloadShellScript(commands)}
                  title="Download all commands as a runnable .sh script">
                  ⬇ .sh
                </button>
                <button
                  type="button"
                  className={styles.railToolBtn}
                  onClick={() => copyAll(commands, (t) => copy(t))}
                  title="Copy all commands">
                  ⎘ all
                </button>
                <span className={styles.railToolHint}>
                  {commands.length} command{commands.length === 1 ? '' : 's'}
                </span>
              </div>
            )}
            <ol className={styles.railList}>
              {commands.length === 0 && (
                <li className={styles.railEmpty}>
                  No CLI commands detected. This page may be prose-only.
                </li>
              )}
              {commands.map((c, i) => (
                <li key={`${c.text}-${i}`} className={styles.railCmd}>
                  <code className={styles.railCmdText} title={c.text}>
                    <span className={styles.railCmdPrompt}>$</span> {c.text}
                  </code>
                  <button
                    type="button"
                    className={styles.railCmdCopy}
                    onClick={() => copy(c.text)}
                    aria-label="Copy command">
                    {copied === c.text ? '✓' : '⎘'}
                  </button>
                </li>
              ))}
            </ol>
          </>
        )}
      </div>
    </aside>
  );
}