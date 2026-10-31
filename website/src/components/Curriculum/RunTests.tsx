import React from 'react';
import CodeBlock from '@theme/CodeBlock';
import {githubUrl, useLessonFrontMatter} from './useLesson';
import styles from './styles.module.css';

export default function RunTests(): React.JSX.Element | null {
  const {lesson} = useLessonFrontMatter();
  if (!lesson) return null;
  const commands = Object.entries(lesson.tests || {});
  const repoName = lesson.repo.split('/')[1];
  const script = [
    `git clone https://github.com/${lesson.repo}.git`,
    `cd ${repoName}/curriculum/${lesson.dir}`,
    ...commands.map(([, cmd]) => cmd),
  ].join('\n');
  return (
    <div className={styles.panel}>
      <div className={styles.panelTitle}>🧪 Run the tests for this lesson</div>
      <p className={styles.muted}>
        The tests are the specification for <em>Build It</em>. They pass on macOS and Linux without root; only the
        namespace and cgroup calls themselves need a Linux box.
      </p>
      <CodeBlock language="bash">{script}</CodeBlock>
      <div className={styles.muted}>
        Browse the code:{' '}
        <a href={githubUrl(lesson, 'code', true)} target="_blank" rel="noopener noreferrer">
          curriculum/{lesson.dir}/code
        </a>
      </div>
    </div>
  );
}
