import React from 'react';
import {githubUrl, useLessonFrontMatter} from './useLesson';
import styles from './styles.module.css';

function describe(name: string): {kind: string; install: string} {
  if (name.startsWith('skill-')) {
    const slug = name.replace(/^skill-/, '').replace(/\.md$/, '');
    return {kind: 'Skill', install: `.claude/skills/${slug}/SKILL.md (Cursor: .cursor/skills/, Codex: .codex/skills/)`};
  }
  if (name.startsWith('prompt-')) return {kind: 'Prompt', install: 'paste into any assistant, or save as a saved prompt / slash command'};
  if (name.startsWith('agent-')) return {kind: 'Agent', install: '.claude/agents/ or your agent framework of choice'};
  if (name.startsWith('mcp-')) return {kind: 'MCP server', install: 'add to your agent’s MCP config; see the folder README'};
  return {kind: 'Artifact', install: ''};
}

export default function ShipIt(): React.JSX.Element | null {
  const {lesson} = useLessonFrontMatter();
  if (!lesson || lesson.outputs.length === 0) return null;
  return (
    <div className={styles.panel}>
      <div className={styles.panelTitle}>📦 Artifacts from this lesson</div>
      <ul className={styles.list}>
        {lesson.outputs.map((name) => {
          const isDir = name.endsWith('/');
          const {kind, install} = describe(name);
          return (
            <li key={name}>
              <span className={styles.badge}>{kind}</span>{' '}
              <a href={githubUrl(lesson, `outputs/${name}`, isDir)} target="_blank" rel="noopener noreferrer">
                <code>{name}</code>
              </a>
              {install && <div className={styles.muted}>Install: {install}</div>}
            </li>
          );
        })}
      </ul>
      <div className={styles.muted}>
        Or install every skill from the curriculum at once: <code>npx skills add {lesson.repo}</code>
      </div>
    </div>
  );
}
