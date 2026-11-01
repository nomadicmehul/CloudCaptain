import React from 'react';
import Link from '@docusaurus/Link';
import curriculumJson from '@site/src/data/generated/curriculum.json';
import type {CurriculumModel} from './types';
import styles from './styles.module.css';

const curriculum = curriculumJson as unknown as CurriculumModel;

export default function CurriculumHome(): React.JSX.Element {
  const {stats, phases, paths, certifications, versions, deprecations} = curriculum;
  const tracked = versions ? Object.entries(versions.versions).map(([key, v]) => ({key, label: versions.track[key]?.label || key, ...v})) : [];
  const watch = (deprecations || []).slice().sort((a, b) => (a.date < b.date ? 1 : -1));
  const live = phases.filter((p) => p.live);
  const planned = phases.filter((p) => !p.live);

  return (
    <div className={styles.home}>
      <div className={styles.stats}>
        <Stat value={stats.lessons} label={stats.lessons === 1 ? 'lesson' : 'lessons'} />
        <Stat value={`${stats.phasesLive}/${stats.phasesTotal}`} label="phases live" />
        <Stat value={stats.hours} label="hours of guided work" />
        <Stat value={stats.tests} label="tests you run" />
        <Stat value={stats.buildLessons} label="from-scratch builds" />
      </div>

      <h2>Phases</h2>
      <div className={styles.grid}>
        {live.map((p) => (
          <Link key={p.slug} to={p.route} className={styles.card}>
            <div className={styles.cardKicker}>
              {p.icon} Phase {p.number}
            </div>
            <div className={styles.cardTitle}>{p.title}</div>
            <div className={styles.cardBody}>{p.tagline}</div>
            <div className={styles.cardFoot}>
              {p.lessons.length} {p.lessons.length === 1 ? 'lesson' : 'lessons'} · {Math.round((p.minutes / 60) * 10) / 10} h
            </div>
            <ol className={styles.cardLessons}>
              {p.lessons.map((l) => (
                <li key={l.id}>
                  <span className={styles.badge} data-type={l.type}>{l.type}</span> {l.short || l.title}
                </li>
              ))}
            </ol>
          </Link>
        ))}
      </div>

      {planned.length > 0 && (
        <>
          <h3>Planned phases</h3>
          <p className={styles.muted}>
            Numbered once, published as lessons land. Track progress in{' '}
            <a href={`https://github.com/${curriculum.site.repo}/blob/main/ROADMAP.md`}>ROADMAP.md</a>.
          </p>
          <div className={styles.chips}>
            {planned.map((p) => (
              <span key={p.slug} className={styles.chip} title={p.tagline}>
                {p.icon} {p.number} · {p.title}
              </span>
            ))}
          </div>
        </>
      )}

      {paths.length > 0 && (
        <>
          <h2>Learning paths</h2>
          <ul className={styles.list}>
            {paths.map((p) => (
              <li key={p.id}>
                <Link to={p.route}>
                  <strong>{p.title}</strong>
                </Link>{' '}
                <span className={styles.muted}>
                  {p.lessons.length} {p.lessons.length === 1 ? 'lesson' : 'lessons'} · {Math.round((p.minutes / 60) * 10) / 10} h
                </span>
                {p.description && <div className={styles.muted}>{p.description}</div>}
              </li>
            ))}
          </ul>
        </>
      )}

      {tracked.length > 0 && (
        <>
          <h2>Current tool versions</h2>
          <p className={styles.muted}>
            Refreshed weekly from GitHub releases (last check {versions!.updatedAt.slice(0, 10)}). Lessons reference these instead of pinning
            numbers that go stale.
          </p>
          <div className={styles.chips}>
            {tracked.map((t) => (
              <a key={t.key} className={styles.chip} href={`https://github.com/${versions!.track[t.key]?.repo}/releases`} title={t.publishedAt ? `released ${t.publishedAt}` : ''}>
                {t.label} <strong>{t.version}</strong>
              </a>
            ))}
          </div>
        </>
      )}

      {watch.length > 0 && (
        <>
          <h2>Deprecation watch</h2>
          <p className={styles.muted}>
            Retired, removed or relicensed tech that tutorials across the web still teach as current. Every lesson and reference page is
            scanned for these.
          </p>
          <table className={styles.watchTable}>
            <thead>
              <tr>
                <th>What</th>
                <th>Status</th>
                <th>Use instead</th>
              </tr>
            </thead>
            <tbody>
              {watch.map((d) => (
                <tr key={d.id}>
                  <td>{d.link ? <a href={d.link}>{d.term}</a> : d.term}</td>
                  <td>
                    <span className={styles.badge}>{d.status}</span> <span className={styles.muted}>{d.date}</span>
                  </td>
                  <td>{d.replacement}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {certifications.length > 0 && (
        <>
          <h2>Certification prep</h2>
          <p className={styles.muted}>Exam guides live in the Reference layer; phases tell you when you are ready.</p>
          <div className={styles.chips}>
            {certifications.map((c) => (
              <Link key={c.id} to={c.referenceDoc} className={styles.chip}>
                {c.title}
              </Link>
            ))}
          </div>
        </>
      )}
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
