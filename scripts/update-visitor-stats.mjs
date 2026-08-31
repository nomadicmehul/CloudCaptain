#!/usr/bin/env node
// Pulls visit counts from GoatCounter and regenerates the badges + line
// chart that the README embeds. Run daily by .github/workflows/visitor-stats.yml.
import {mkdir, writeFile} from 'node:fs/promises';
import path from 'node:path';

const SITE_CODE = process.env.GOATCOUNTER_SITE_CODE;
const API_TOKEN = process.env.GOATCOUNTER_API_TOKEN;

if (!SITE_CODE || !API_TOKEN) {
  // Not configured yet (see issue #53) — skip instead of failing so the
  // scheduled workflow doesn't show as a red X every day until it's set up.
  console.warn(
    'GOATCOUNTER_SITE_CODE / GOATCOUNTER_API_TOKEN not set — skipping visitor stats update.',
  );
  process.exit(0);
}

const API_BASE = `https://${SITE_CODE}.goatcounter.com/api/v0`;
const CHART_WINDOW_DAYS = 90;
const OUT_DIR = path.join(process.cwd(), '.github', 'stats');

const BRAND = {
  line: '#1E9BD7',
  navy: '#0A1628',
  panelDark: '#1B2A4A',
  panelLight: '#E2E8F0',
};

async function fetchTotal(start, end) {
  const url = new URL(`${API_BASE}/stats/total`);
  url.searchParams.set('start', start.toISOString());
  url.searchParams.set('end', end.toISOString());

  const res = await fetch(url, {
    headers: {
      Authorization: `Bearer ${API_TOKEN}`,
      'Content-Type': 'application/json',
    },
  });

  if (!res.ok) {
    throw new Error(
      `GoatCounter API error ${res.status} on ${url}: ${await res.text()}`,
    );
  }

  return res.json();
}

function badgeJson(label, message, color) {
  return JSON.stringify({schemaVersion: 1, label, message, color}, null, 2) + '\n';
}

function buildChartSvg(points, theme) {
  const width = 800;
  const height = 220;
  const padding = {top: 20, right: 20, bottom: 28, left: 20};
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;

  const isDark = theme === 'dark';
  const bg = isDark ? BRAND.navy : '#FFFFFF';
  const gridColor = isDark ? BRAND.panelDark : BRAND.panelLight;
  const textColor = isDark ? '#94A3B8' : '#475569';
  const fillTop = isDark ? 'rgba(30,155,215,0.35)' : 'rgba(30,155,215,0.25)';

  if (points.length === 0) {
    return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="No visitor data yet">
  <rect width="${width}" height="${height}" fill="${bg}" rx="8"/>
  <text x="${width / 2}" y="${height / 2}" font-family="monospace" font-size="13" fill="${textColor}" text-anchor="middle">No visitor data yet</text>
</svg>`;
  }

  const max = Math.max(1, ...points.map((p) => p.count));
  const stepX = points.length > 1 ? plotWidth / (points.length - 1) : 0;
  const coords = points.map((p, i) => [
    padding.left + i * stepX,
    padding.top + plotHeight - (p.count / max) * plotHeight,
  ]);

  const linePath = coords
    .map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`)
    .join(' ');
  const floorY = padding.top + plotHeight;
  const areaPath = `${linePath} L${coords.at(-1)[0].toFixed(1)},${floorY} L${coords[0][0].toFixed(1)},${floorY} Z`;

  const firstLabel = points[0].day;
  const lastLabel = points.at(-1).day;

  return `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Visitor traffic over the last ${points.length} days">
  <rect width="${width}" height="${height}" fill="${bg}" rx="8"/>
  <line x1="${padding.left}" y1="${floorY}" x2="${width - padding.right}" y2="${floorY}" stroke="${gridColor}" stroke-width="1"/>
  <defs>
    <linearGradient id="fill-${theme}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${fillTop}"/>
      <stop offset="100%" stop-color="rgba(30,155,215,0)"/>
    </linearGradient>
  </defs>
  <path d="${areaPath}" fill="url(#fill-${theme})" stroke="none"/>
  <path d="${linePath}" fill="none" stroke="${BRAND.line}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
  <text x="${padding.left}" y="${height - 8}" font-family="monospace" font-size="11" fill="${textColor}">${firstLabel}</text>
  <text x="${width - padding.right}" y="${height - 8}" font-family="monospace" font-size="11" fill="${textColor}" text-anchor="end">${lastLabel}</text>
</svg>`;
}

const now = new Date();
const windowStart = new Date(now.getTime() - CHART_WINDOW_DAYS * 24 * 60 * 60 * 1000);
// GoatCounter has no "all time" endpoint — a start date before any real
// site can have launched stands in for one.
const allTimeStart = new Date('2015-01-01T00:00:00Z');

const [recent, allTime] = await Promise.all([
  fetchTotal(windowStart, now),
  fetchTotal(allTimeStart, now),
]);

// A stat bucket may report its count under `daily`, `weekly`, or `monthly`
// depending on how GoatCounter auto-groups the requested range.
const points = (recent.stats ?? []).map((s) => ({
  day: (s.day ?? '').slice(5, 10) || '?',
  count: s.daily ?? s.weekly ?? s.monthly ?? 0,
}));

const today = points.at(-1)?.count ?? 0;
const thisWeek = points.slice(-7).reduce((sum, p) => sum + p.count, 0);
const totalAllTime = allTime.total ?? 0;

await mkdir(OUT_DIR, {recursive: true});

await Promise.all([
  writeFile(
    path.join(OUT_DIR, 'visitors-total.json'),
    badgeJson('visits total', totalAllTime.toLocaleString('en-US'), '1E9BD7'),
  ),
  writeFile(
    path.join(OUT_DIR, 'visitors-week.json'),
    badgeJson('visits this week', thisWeek.toLocaleString('en-US'), '38BDF8'),
  ),
  writeFile(
    path.join(OUT_DIR, 'visitors-today.json'),
    badgeJson('visits today', today.toLocaleString('en-US'), '06B6D4'),
  ),
  writeFile(path.join(OUT_DIR, 'chart-light.svg'), buildChartSvg(points, 'light')),
  writeFile(path.join(OUT_DIR, 'chart-dark.svg'), buildChartSvg(points, 'dark')),
]);

console.log(
  `Updated visitor stats: total=${totalAllTime} week=${thisWeek} today=${today} chartPoints=${points.length}`,
);
