// Crew cards: the shared rules behind the /crew page, the crew-welcome
// workflow and scripts/__tests__/crew.test.mjs. Plain ESM with no imports,
// so Node (tests, github-script) and webpack (the site) both load it as is.
// The editable data (handles, ranks, ports) lives in website/src/data/crew.json
// and is passed in as `config`.

/** @param {{login?: string, type?: string} | null | undefined} user */
export function isBot(user) {
  return !user || user.type === 'Bot' || /\[bot\]$/i.test(user.login || '');
}

/**
 * Turn GitHub's pull request list into the crew roster: one entry per author,
 * in the order of their first merged PR. The captain is listed but not numbered.
 * @param {Array<any>} pulls  items from GET /repos/{repo}/pulls?state=closed
 * @param {any} config        website/src/data/crew.json
 * @returns {Map<string, {login: string, avatar: string, captain: boolean, number: number | null, voyages: Array<{number: number, title: string, mergedAt: string, labels: string[]}>}>}
 */
export function buildRoster(pulls, config) {
  const captain = config.captain.github.toLowerCase();
  const merged = pulls
    .filter((p) => p && p.merged_at && !isBot(p.user))
    .sort((a, b) => a.merged_at.localeCompare(b.merged_at) || a.number - b.number);
  const roster = new Map();
  for (const p of merged) {
    const key = p.user.login.toLowerCase();
    if (!roster.has(key)) {
      roster.set(key, {login: p.user.login, avatar: p.user.avatar_url || '', captain: key === captain, number: null, voyages: []});
    }
    roster.get(key).voyages.push({
      number: p.number,
      title: p.title,
      mergedAt: p.merged_at,
      labels: (p.labels || []).map((l) => (typeof l === 'string' ? l : l.name)),
    });
  }
  let n = 0;
  for (const member of roster.values()) if (!member.captain) member.number = ++n;
  return roster;
}

/**
 * The rank for a number of merged PRs, and how many more it takes to reach the next one.
 * @param {number} voyages
 * @param {boolean} captain
 * @param {any} config
 */
export function rankFor(voyages, captain, config) {
  if (captain) return {title: 'Captain', emoji: '🧑‍✈️', next: null};
  const ranks = [...config.ranks].sort((a, b) => a.min - b.min);
  let current = null;
  let next = null;
  for (const r of ranks) {
    if (voyages >= r.min) current = r;
    else if (!next) next = r;
  }
  if (!current) return {title: 'Stowaway', emoji: '🛶', next: ranks[0] ? {title: ranks[0].title, needed: ranks[0].min - voyages} : null};
  return {title: current.title, emoji: current.emoji, next: next ? {title: next.title, needed: next.min - voyages} : null};
}

/**
 * The "ports" (areas of the repo) a crew member's PRs touched, from PR labels
 * first and title keywords second, in the order the ports are listed in config.
 * @param {Array<{title: string, labels: string[]}>} voyages
 * @param {any} config
 * @returns {Array<{id: string, name: string, area: string}>}
 */
export function portsFor(voyages, config) {
  const hit = new Set();
  for (const v of voyages) {
    const labels = new Set((v.labels || []).map((l) => l.toLowerCase()));
    const title = ` ${String(v.title || '').toLowerCase()} `;
    for (const port of config.ports) {
      if (port.labels.some((l) => labels.has(l)) || port.keywords.some((k) => title.includes(k))) hit.add(port.id);
    }
  }
  return config.ports.filter((p) => hit.has(p.id)).map((p) => ({id: p.id, name: p.name, area: p.area || p.name}));
}

/** "a Deckhand", "an Able Seaman" */
export function withArticle(word) {
  return `${/^[aeiou]/i.test(word) ? 'an' : 'a'} ${word}`;
}

export function ordinal(n) {
  const s = ['th', 'st', 'nd', 'rd'];
  const v = n % 100;
  return `${n}${s[(v - 20) % 10] || s[v] || s[0]}`;
}

export function shortDate(iso) {
  const d = new Date(iso);
  return `${d.getUTCDate()} ${d.toLocaleString('en-US', {month: 'short', timeZone: 'UTC'})} ${d.getUTCFullYear()}`;
}

export function monthYear(iso) {
  const d = new Date(iso);
  return `${d.toLocaleString('en-US', {month: 'short', timeZone: 'UTC'})} ${d.getUTCFullYear()}`;
}

/** Strip a conventional "Fix:" / "Add:" prefix and trailing "(#123)" from a PR title. */
export function cleanTitle(title) {
  return String(title || '')
    .replace(/^\s*(add|fix|update|docs?|chore|feat|refactor|test)\s*(\([^)]*\))?\s*:\s*/i, '')
    .replace(/\s*\(#\d+\)\s*$/, '')
    .trim();
}

function listJoin(items) {
  return items.length < 3 ? items.join(' and ') : `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

function clip(text, max) {
  const chars = [...text];
  return chars.length <= max ? text : `${chars.slice(0, Math.max(1, max - 1)).join('').trimEnd()}…`;
}

/**
 * Length as X counts it: a URL is always 23, and characters outside the basic
 * Latin/Latin-1/punctuation ranges (emoji, CJK) count 2. Overcounts slightly
 * for emoji sequences, which keeps posts safely under the limit.
 */
export function xLength(text) {
  let n = 0;
  for (const part of text.split(/(https?:\/\/\S+)/)) {
    if (/^https?:\/\//.test(part)) {
      n += 23;
      continue;
    }
    for (const ch of part) {
      const c = ch.codePointAt(0);
      const light = c <= 4351 || (c >= 8192 && c <= 8205) || (c >= 8208 && c <= 8223) || (c >= 8242 && c <= 8247);
      n += light ? 1 : 2;
    }
  }
  return n;
}

/** Length as Bluesky counts it (graphemes, links in full). */
export function graphemeLength(text) {
  if (typeof Intl !== 'undefined' && Intl.Segmenter) {
    let n = 0;
    for (const _ of new Intl.Segmenter('en', {granularity: 'grapheme'}).segment(text)) n += 1;
    return n;
  }
  return [...text].length;
}

export const STYLES = ['log', 'pass', 'plain'];

/**
 * Ready-to-post text for each channel.
 * @param {{login: string, captain: boolean, number: number | null, voyages: Array<{number: number, title: string, mergedAt: string, labels: string[]}>}} member
 * @param {any} config
 * @param {string} style  one of STYLES
 * @returns {{linkedin: string, x: string, bluesky: string, url: string}}
 */
export function buildPosts(member, config, style = 'log') {
  const count = member.voyages.length;
  const latest = member.voyages[count - 1];
  const rank = rankFor(count, member.captain, config);
  const visited = portsFor(member.voyages, config);
  const ports = visited.map((p) => p.name);
  const areas = visited.map((p) => p.area);
  const url = `${config.siteUrl}/crew?u=${encodeURIComponent(member.login)}`;
  const crewNo = member.captain ? 'the captain' : `crew member #${member.number}`;
  const nth = ordinal(count);
  const title = cleanTitle(latest.title);
  const tags = config.project.hashtags.map((h) => `#${h}`);
  const cap = config.captain;
  const xCap = cap.x ? `@${cap.x}` : cap.name;
  const bskyCap = cap.bluesky ? `@${cap.bluesky}` : cap.name;
  const liCap = `@${cap.linkedin || cap.name}`;
  const liProject = `@${config.project.linkedin}`;
  const portLine = ports.length ? ports.join(' · ') : 'the open sea';
  const pitch =
    'CloudCaptain is a free, open-source course that teaches Cloud, DevOps and AIOps by building the tools from scratch: every lesson ships runnable code, tests and a quiz.';

  const linkedin = {
    log: [
      `⚓ Ship's log, ${shortDate(latest.mergedAt)}`,
      '',
      `Reporting as ${crewNo} of ${config.project.name}. My ${nth} pull request just merged: "${title}" (#${latest.number}).`,
      '',
      `Rank: ${rank.title} ${rank.emoji}`,
      `Ports visited: ${portLine}`,
      '',
      pitch,
      '',
      `Thanks ${liCap} for captaining the ship and for a thoughtful review, and to the ${liProject} crew.`,
      '',
      `Want to come aboard? My crew card and the open issues: ${url}`,
      '',
      tags.join(' '),
    ],
    pass: [
      `🎫 Boarding pass stamped: I'm now ${crewNo} on ${config.project.name}!`,
      '',
      `${count === 1 ? 'My first contribution' : `Contribution number ${count}`} just merged: "${title}" (#${latest.number}). That makes me ${withArticle(rank.title)} ${rank.emoji}${rank.next ? `, ${rank.next.needed} more PR${rank.next.needed === 1 ? '' : 's'} to ${rank.next.title}` : ''}.`,
      '',
      pitch,
      '',
      `Big thanks to ${liCap} and the ${liProject} community for making first-time contributors feel at home.`,
      '',
      `If you've been looking for a friendly open-source project, there are good first issues waiting: ${url}`,
      '',
      tags.join(' '),
    ],
    plain: [
      `I contributed to ${config.project.name}, an open-source learning platform for Cloud, DevOps and AIOps.`,
      '',
      `My ${nth} pull request, "${title}" (#${latest.number}), was just merged.${areas.length ? ` So far I've worked on ${listJoin(areas)}.` : ''}`,
      '',
      pitch,
      '',
      `Thank you ${liCap} for the review and for building this with the ${liProject} community.`,
      '',
      `See my contributor card and find an issue to start with: ${url}`,
      '',
      tags.join(' '),
    ],
  }[style].join('\n');

  const shortTags = tags.slice(0, 2).join(' ');
  const fit = (make, measure, limit) => {
    for (let max = 90; max >= 12; max -= 4) {
      const text = make(clip(title, max));
      if (measure(text) <= limit) return text;
    }
    return make(clip(title, 12));
  };
  const short = {
    log: (t, who) => `⚓ Ship's log: my ${nth} PR to ${config.project.name} just merged, "${t}". I'm ${crewNo}, rank ${rank.title} ${rank.emoji}\n\nThanks ${who} for the review! Come aboard: ${url}\n\n${shortTags}`,
    pass: (t, who) => `🎫 Boarding pass stamped: I'm ${crewNo} on ${config.project.name}! My ${nth} PR merged: "${t}"\n\nThanks ${who} for running such a welcoming ship. Good first issues: ${url}\n\n${shortTags}`,
    plain: (t, who) => `My ${nth} pull request to ${config.project.name}, the open-source Cloud & DevOps course, was merged: "${t}"\n\nThanks ${who} for the review. Want to contribute too? ${url}\n\n${shortTags}`,
  }[style];

  return {
    linkedin,
    x: fit((t) => short(t, xCap), xLength, 280),
    bluesky: fit((t) => short(t, bskyCap), graphemeLength, 300),
    url,
  };
}

/** Composer links that open with the text filled in. */
export function shareUrl(channel, text) {
  const q = encodeURIComponent(text);
  if (channel === 'x') return `https://x.com/intent/post?text=${q}`;
  if (channel === 'bluesky') return `https://bsky.app/intent/compose?text=${q}`;
  return `https://www.linkedin.com/feed/?shareActive=true&text=${q}`;
}
