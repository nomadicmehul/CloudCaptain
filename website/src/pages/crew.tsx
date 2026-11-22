import React, {useEffect, useMemo, useRef, useState} from 'react';
import type {JSX} from 'react';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';
import {useHistory, useLocation} from '@docusaurus/router';

import crewConfig from '@site/src/data/crew.json';
import CrewCard, {cardToPng, type Member} from '@site/src/components/Crew/CrewCard';
import ShipsLog from '@site/src/components/Crew/ShipsLog';
import {useAvatarData, useCrewRoster} from '@site/src/components/Crew/useCrew';
import {portsFor, rankFor} from '@site/src/components/Crew/crewCore.mjs';
import styles from '@site/src/components/Crew/styles.module.css';

const REPO = crewConfig.repo;
const GOOD_FIRST = `https://github.com/${REPO}/issues?q=is%3Aopen+is%3Aissue+label%3A%22good+first+issue%22+no%3Aassignee`;
const ROADMAP = `https://github.com/${REPO}/issues/230`;

function MemberView({member}: {member: Member}): JSX.Element {
  const svgRef = useRef<SVGSVGElement>(null);
  const avatarData = useAvatarData(member.avatar);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const rank = rankFor(member.voyages.length, member.captain, crewConfig);
  const ports = portsFor(member.voyages, crewConfig);
  const canCopyImage = typeof window !== 'undefined' && 'ClipboardItem' in window;

  const withPng = async (label: string, fn: (png: Blob) => Promise<void>) => {
    if (!svgRef.current) return;
    setBusy(label);
    setNote(null);
    try {
      await fn(await cardToPng(svgRef.current));
    } catch (e) {
      setNote(`Sorry, that didn't work (${(e as Error).message}). Try the other button or a different browser.`);
    } finally {
      setBusy(null);
    }
  };

  const download = () =>
    withPng('download', async (png) => {
      const url = URL.createObjectURL(png);
      const a = document.createElement('a');
      a.href = url;
      a.download = `cloudcaptain-crew-${member.login}.png`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      setNote('Saved. Attach it to your post.');
    });

  const copyImage = () =>
    withPng('copy', async (png) => {
      await navigator.clipboard.write([new ClipboardItem({'image/png': png})]);
      setNote('Card copied. Paste it straight into your post.');
    });

  return (
    <>
      <section className={styles.cardWrap} aria-label="Your crew card">
        <div className={styles.cardFrame}>
          <CrewCard
            ref={svgRef}
            member={member}
            rank={rank}
            ports={ports}
            avatarData={avatarData}
            captainHandle={crewConfig.captain.github}
            site={crewConfig.siteUrl}
          />
        </div>
        <div className={styles.cardActions}>
          <button type="button" className="button button--primary" onClick={download} disabled={busy !== null}>
            {busy === 'download' ? 'Preparing…' : '⬇ Download card (PNG)'}
          </button>
          {canCopyImage && (
            <button type="button" className="button button--secondary" onClick={copyImage} disabled={busy !== null}>
              {busy === 'copy' ? 'Copying…' : '📋 Copy card image'}
            </button>
          )}
        </div>
        {note && (
          <p className={styles.note} role="status">
            {note}
          </p>
        )}
        <ul className={styles.voyages}>
          {[...member.voyages].reverse().map((v) => (
            <li key={v.number}>
              <a href={`https://github.com/${REPO}/pull/${v.number}`} target="_blank" rel="noopener noreferrer">
                #{v.number}
              </a>{' '}
              {v.title}
            </li>
          ))}
        </ul>
      </section>
      <ShipsLog member={member} config={crewConfig} />
    </>
  );
}

function NotAboard({login}: {login: string}): JSX.Element {
  return (
    <section className={styles.empty}>
      <h2 className={styles.sectionTitle}>No merged pull requests from @{login} yet</h2>
      <p>
        Every crew member started with one. Pick a{' '}
        <a href={GOOD_FIRST} target="_blank" rel="noopener noreferrer">
          good first issue
        </a>{' '}
        or browse the{' '}
        <a href={ROADMAP} target="_blank" rel="noopener noreferrer">
          contributor roadmap
        </a>
        . Your card appears here as soon as your first PR is merged.
      </p>
    </section>
  );
}

function Roster({roster, active}: {roster: Map<string, Member>; active: string}): JSX.Element {
  const crew = [...roster.values()].sort((a, b) => (a.captain ? -1 : b.captain ? 1 : (a.number ?? 0) - (b.number ?? 0)));
  return (
    <section aria-labelledby="crew-roster">
      <h2 id="crew-roster" className={styles.sectionTitle}>
        The crew so far
      </h2>
      <ul className={styles.roster}>
        {crew.map((m) => {
          const rank = rankFor(m.voyages.length, m.captain, crewConfig);
          return (
            <li key={m.login}>
              <Link
                to={`/crew?u=${encodeURIComponent(m.login)}`}
                className={m.login.toLowerCase() === active ? styles.crewOn : styles.crew}>
                <img src={`${m.avatar}${m.avatar.includes('?') ? '&' : '?'}s=80`} alt="" width={40} height={40} loading="lazy" />
                <span className={styles.crewText}>
                  <span className={styles.crewLogin}>@{m.login}</span>
                  <span className={styles.crewMeta}>
                    {m.captain ? 'Captain' : `#${m.number} · ${rank.title}`} · {m.voyages.length} PR{m.voyages.length === 1 ? '' : 's'}
                  </span>
                </span>
              </Link>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export default function CrewPage(): JSX.Element {
  const location = useLocation();
  const history = useHistory();
  const fromUrl = useMemo(() => new URLSearchParams(location.search).get('u')?.trim().replace(/^@/, '') || '', [location.search]);
  const [input, setInput] = useState(fromUrl);
  useEffect(() => setInput(fromUrl), [fromUrl]);
  const state = useCrewRoster(REPO, crewConfig);
  const member = state.status === 'ready' && fromUrl ? state.roster.get(fromUrl.toLowerCase()) : undefined;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const login = input.trim().replace(/^@/, '').replace(/^https?:\/\/github\.com\//i, '').split('/')[0];
    history.push(login ? `/crew?u=${encodeURIComponent(login)}` : '/crew');
  };

  return (
    <Layout
      title="Crew Cards"
      description="Contributed to CloudCaptain? Get your crew card and a ready-to-post Ship's Log for LinkedIn, X and Bluesky.">
      <main className={styles.page}>
        <header className={styles.hero}>
          <p className={styles.kicker}>⚓ CloudCaptain crew manifest</p>
          <h1 className={styles.title}>Every merged PR earns you a place on the crew</h1>
          <p className={styles.subtitle}>
            Enter your GitHub username to get your boarding-pass crew card, with your rank and the ports you've visited,
            plus a post for LinkedIn, X and Bluesky that's ready to share and tags{' '}
            <a href={`https://x.com/${crewConfig.captain.x}`} target="_blank" rel="noopener noreferrer">
              @{crewConfig.captain.x}
            </a>
            .
          </p>
          <form className={styles.form} onSubmit={submit} role="search">
            <label htmlFor="crew-login" className={styles.srOnly}>
              GitHub username
            </label>
            <span className={styles.prefix} aria-hidden="true">
              github.com/
            </span>
            <input
              id="crew-login"
              className={styles.input}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="your-username"
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
            />
            <button type="submit" className="button button--primary">
              Show my card
            </button>
          </form>
          <p className={styles.ranks}>
            Ranks:{' '}
            {crewConfig.ranks.map((r, i) => (
              <span key={r.title}>
                {i > 0 && ' → '}
                {r.emoji} {r.title} <span className={styles.rankMin}>({r.min}+)</span>
              </span>
            ))}
          </p>
        </header>

        {state.status === 'loading' && <p className={styles.status}>Reading the crew manifest from GitHub…</p>}
        {state.status === 'error' && (
          <p className={styles.status} role="alert">
            {state.message}
          </p>
        )}
        {state.status === 'ready' && (
          <>
            {member && <MemberView key={member.login} member={member} />}
            {fromUrl && !member && <NotAboard login={fromUrl} />}
            <Roster roster={state.roster} active={fromUrl.toLowerCase()} />
          </>
        )}

        <section className={styles.join} aria-labelledby="come-aboard">
          <h2 id="come-aboard" className={styles.sectionTitle}>
            Not on the crew yet?
          </h2>
          <ol className={styles.steps}>
            <li>
              Pick a{' '}
              <a href={GOOD_FIRST} target="_blank" rel="noopener noreferrer">
                good first issue
              </a>{' '}
              or browse the{' '}
              <a href={ROADMAP} target="_blank" rel="noopener noreferrer">
                contributor roadmap
              </a>
              , and comment that you'd like to work on it.
            </li>
            <li>
              Read the <Link to="/contribute">contributing guide</Link>, fork the repo and open a pull request.
            </li>
            <li>When it's merged, come back here for your card. Every merged PR moves you up a rank.</li>
          </ol>
        </section>
      </main>
    </Layout>
  );
}
