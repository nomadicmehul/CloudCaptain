import React, {useEffect, useMemo, useState} from 'react';
import type {JSX} from 'react';
import {buildPosts, graphemeLength, shareUrl, xLength} from './crewCore.mjs';
import type {Member} from './CrewCard';
import styles from './styles.module.css';

type Channel = 'linkedin' | 'x' | 'bluesky';
type Style = 'log' | 'pass' | 'plain';

const STYLE_LABELS: Record<Style, string> = {
  log: "⚓ Ship's log",
  pass: '🎫 Boarding pass',
  plain: '✍️ Straight and simple',
};

type ChannelInfo = {name: string; limit: number; measure: (t: string) => number; tip: (config: any) => string};

const CHANNELS: Record<Channel, ChannelInfo> = {
  linkedin: {
    name: 'LinkedIn',
    limit: 3000,
    measure: (t) => t.length,
    tip: (c) =>
      `LinkedIn only turns a mention into a tag when you pick it from its list. In the composer, delete "@${c.captain.linkedin}" and "@${c.project.linkedin}", type them again and choose ${c.captain.name} and the ${c.project.name} page. Then add your card image.`,
  },
  x: {
    name: 'X',
    limit: 280,
    measure: xLength,
    tip: (c) => `@${c.captain.x} is already tagged. Add your card image before you post: posts with an image get far more attention.`,
  },
  bluesky: {
    name: 'Bluesky',
    limit: 300,
    measure: graphemeLength,
    tip: (c) =>
      c.captain.bluesky
        ? `@${c.captain.bluesky} is already tagged. Add your card image before you post.`
        : `Add your card image before you post. If you know ${c.captain.name}'s Bluesky handle, swap it in for the name.`,
  },
};

async function copy(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function ChannelBox({channel, initial, config}: {channel: Channel; initial: string; config: any}): JSX.Element {
  const info = CHANNELS[channel];
  const [text, setText] = useState(initial);
  const [copied, setCopied] = useState(false);
  useEffect(() => setText(initial), [initial]);
  const used = info.measure(text);
  const over = used > info.limit;

  const onCopy = async () => {
    if (await copy(text)) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    }
  };

  return (
    <div className={styles.channel}>
      <div className={styles.channelHead}>
        <h3 className={styles.channelName}>{info.name}</h3>
        <span className={over ? styles.countOver : styles.count} aria-live="polite">
          {used}/{info.limit}
        </span>
      </div>
      <textarea
        className={styles.post}
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={channel === 'linkedin' ? 14 : 7}
        aria-label={`${info.name} post text`}
      />
      <div className={styles.channelActions}>
        <button type="button" className="button button--secondary button--sm" onClick={onCopy}>
          {copied ? 'Copied ✓' : 'Copy text'}
        </button>
        <a
          className="button button--primary button--sm"
          href={shareUrl(channel, text)}
          target="_blank"
          rel="noopener noreferrer"
          onClick={() => {
            // LinkedIn sometimes drops the prefilled text; having it on the clipboard is the fallback.
            void copy(text);
          }}>
          Open {info.name} ↗
        </a>
      </div>
      <p className={styles.tip}>{info.tip(config)}</p>
    </div>
  );
}

/** Ready-to-post text for LinkedIn, X and Bluesky, in three styles. */
export default function ShipsLog({member, config}: {member: Member; config: any}): JSX.Element {
  const [style, setStyle] = useState<Style>('log');
  const posts = useMemo(() => buildPosts(member, config, style), [member, config, style]);

  return (
    <section className={styles.log} aria-labelledby="ships-log">
      <h2 id="ships-log" className={styles.sectionTitle}>
        Write your Ship's Log
      </h2>
      <p className={styles.lead}>
        Pick a style, edit anything you like, then post it with your card. Each post links back to your card, so the
        people who see it can find an issue and come aboard too.
      </p>
      <div className={styles.styles} role="radiogroup" aria-label="Post style">
        {(Object.keys(STYLE_LABELS) as Style[]).map((s) => (
          <button
            key={s}
            type="button"
            role="radio"
            aria-checked={style === s}
            className={style === s ? styles.styleOn : styles.style}
            onClick={() => setStyle(s)}>
            {STYLE_LABELS[s]}
          </button>
        ))}
      </div>
      <div className={styles.channels}>
        <ChannelBox channel="linkedin" initial={posts.linkedin} config={config} />
        <ChannelBox channel="x" initial={posts.x} config={config} />
        <ChannelBox channel="bluesky" initial={posts.bluesky} config={config} />
      </div>
    </section>
  );
}
