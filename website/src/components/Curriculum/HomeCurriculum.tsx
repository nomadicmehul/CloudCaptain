import React from 'react';
import Link from '@docusaurus/Link';
import {curriculumModel} from './curriculumTitles';
import styles from './styles.module.css';

const PILLARS = [
  {icon: '🔧', title: 'Build It', text: 'A container runtime, a plan/apply engine, a metrics store, an incident-triage agent, from the standard library, with tests.'},
  {icon: '🚀', title: 'Use It', text: 'The same job with Docker, Kubernetes, Terraform, Prometheus or an MCP server, so every flag maps onto something you typed.'},
  {icon: '📦', title: 'Ship It', text: 'Leave each lesson with a prompt, an agent skill or an MCP server in your own toolbox, plus a quiz score that proves it stuck.'},
];

export default function HomeCurriculum(): React.JSX.Element {
  const {stats, phases, site} = curriculumModel;
  const live = phases.filter((p) => p.live);
  return (
    <div className="section">
      <div className="container">
        <h2 className="section__title reveal">
          <span className="section-prompt">$ ./start-learning.sh</span> Learn Cloud, DevOps &amp; AIOps from scratch
        </h2>
        <p className="section__subtitle reveal">
          Build the tool. Use the tool. Ship an artifact. Every lesson has runnable code, tests and a quiz, and a tutor you can
          install into your coding agent.
        </p>

        <div className={`${styles.pillars} reveal`}>
          {PILLARS.map((p) => (
            <div key={p.title} className={styles.pillar}>
              <div className={styles.pillarIcon} aria-hidden="true">{p.icon}</div>
              <div className={styles.pillarTitle}>{p.title}</div>
              <div className={styles.pillarText}>{p.text}</div>
            </div>
          ))}
        </div>

        <div className={`${styles.stats} reveal`}>
          <Stat value={stats.lessons} label={stats.lessons === 1 ? 'lesson live' : 'lessons live'} />
          <Stat value={`${stats.phasesLive}/${stats.phasesTotal}`} label="phases started" />
          <Stat value={stats.hours} label="hours of guided work" />
          <Stat value={stats.tests} label="tests you run" />
          <Stat value={stats.buildLessons} label="from-scratch builds" />
        </div>

        {live.length > 0 && (
          <div className={`${styles.chips} reveal`} style={{justifyContent: 'center', marginTop: '1rem'}}>
            {live.map((p) => (
              <Link key={p.slug} to={p.route} className={styles.chip}>
                {p.icon} {p.number} · {p.title} · {p.lessons.length} {p.lessons.length === 1 ? 'lesson' : 'lessons'}
              </Link>
            ))}
            <a href={`https://github.com/${site.repo}/blob/main/ROADMAP.md`} className={styles.chip}>
              + {stats.phasesTotal - stats.phasesLive} phases on the roadmap
            </a>
          </div>
        )}

        <div className={`${styles.homeCta} reveal`}>
          <Link className="btn-primary" to="/learn">
            Start the course →
          </Link>
          <code className={styles.cmd}>npx skills add {site.repo}</code>
        </div>
        <p className={`${styles.muted} reveal`} style={{textAlign: 'center', marginTop: '0.75rem'}}>
          The tutor skills (<code>/captain-start</code>, <code>/captain-learn</code>, <code>/captain-guide</code>,{' '}
          <code>/captain-check</code>) work in Claude Code, Cursor, Codex and any agent that reads SKILL.md.
        </p>
      </div>
    </div>
  );
}

function Stat({value, label}: {value: number | string; label: string}): React.JSX.Element {
  return (
    <div className={styles.stat}>
      <div className={styles.statValue}>{value}</div>
      <div className={styles.statLabel}>{label}</div>
    </div>
  );
}
