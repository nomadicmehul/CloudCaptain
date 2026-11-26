import React, {forwardRef} from 'react';
import type {JSX} from 'react';
import {cleanTitle, monthYear} from './crewCore.mjs';

export type Voyage = {number: number; title: string; mergedAt: string; labels: string[]};
export type Member = {login: string; avatar: string; captain: boolean; number: number | null; voyages: Voyage[]};
export type Rank = {title: string; emoji: string; next: {title: string; needed: number} | null};
export type Port = {id: string; name: string; area: string};

type Props = {
  member: Member;
  rank: Rank;
  ports: Port[];
  /** Avatar as a data: URL, so the PNG export can draw it. Falls back to initials. */
  avatarData: string | null;
  captainHandle: string;
  site: string;
};

export const CARD_WIDTH = 1200;
export const CARD_HEIGHT = 630;

const SANS = "Inter, 'Segoe UI', system-ui, -apple-system, Roboto, sans-serif";
const MONO = "'JetBrains Mono', ui-monospace, Menlo, Consolas, monospace";
const STAMP_COLORS = ['#38BDF8', '#F59E0B', '#34D399', '#F472B6', '#A78BFA', '#06B6D4'];

function clip(text: string, max: number): string {
  const chars = [...text];
  return chars.length <= max ? text : `${chars.slice(0, max - 1).join('').trimEnd()}…`;
}

/** Deterministic tilt per stamp, so the card looks hand-stamped but renders the same every time. */
function tilt(seed: string): number {
  let h = 0;
  for (const ch of seed) h = (h * 31 + ch.charCodeAt(0)) | 0;
  return (Math.abs(h) % 25) - 12;
}

function Stamp({port, x, y, color, date}: {port: Port; x: number; y: number; color: string; date: string}): JSX.Element {
  const words = port.name.toUpperCase().split(' ');
  return (
    <g transform={`translate(${x} ${y}) rotate(${tilt(port.id)})`} opacity={0.92}>
      <circle r={60} fill="none" stroke={color} strokeWidth={4} />
      <circle r={50} fill="none" stroke={color} strokeWidth={1.5} strokeDasharray="3 4" />
      {words.map((w, i) => (
        <text
          key={w}
          y={(i - (words.length - 1) / 2) * 20 - 4}
          textAnchor="middle"
          dominantBaseline="middle"
          fontFamily={SANS}
          fontSize={w.length > 8 ? 13 : w.length > 6 ? 15 : 17}
          fontWeight={800}
          letterSpacing={1}
          fill={color}>
          {w}
        </text>
      ))}
      <text y={34} textAnchor="middle" fontFamily={MONO} fontSize={11} fill={color}>
        {date}
      </text>
    </g>
  );
}

/**
 * The crew card: a 1200×630 boarding pass (the size LinkedIn and X use for
 * link images). Pure SVG so it stays sharp and can be exported to PNG.
 */
const CrewCard = forwardRef<SVGSVGElement, Props>(function CrewCard(
  {member, rank, ports, avatarData, captainHandle, site},
  ref,
) {
  const count = member.voyages.length;
  const latest = member.voyages[count - 1];
  const first = member.voyages[0];
  const crewNo = member.captain ? 'CAPT' : `#${String(member.number).padStart(3, '0')}`;
  const shown: Port[] = ports.length ? ports.slice(0, 6) : [{id: 'sea', name: 'Open Sea', area: 'open sea'}];
  const stampDate = monthYear(first.mergedAt).toUpperCase();
  const progress = rank.next ? count / (count + rank.next.needed) : 1;
  const host = site.replace(/^https?:\/\//, '');

  return (
    <svg
      ref={ref}
      xmlns="http://www.w3.org/2000/svg"
      viewBox={`0 0 ${CARD_WIDTH} ${CARD_HEIGHT}`}
      width={CARD_WIDTH}
      height={CARD_HEIGHT}
      role="img"
      aria-label={`CloudCaptain crew card for @${member.login}: ${rank.title}, ${count} merged pull request${count === 1 ? '' : 's'}`}>
      <defs>
        <linearGradient id="cc-ticket" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#13294B" />
          <stop offset="100%" stopColor="#0A1628" />
        </linearGradient>
        <linearGradient id="cc-ring" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#38BDF8" />
          <stop offset="100%" stopColor="#06B6D4" />
        </linearGradient>
        <linearGradient id="cc-bar" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0%" stopColor="#1E9BD7" />
          <stop offset="100%" stopColor="#38BDF8" />
        </linearGradient>
        <clipPath id="cc-avatar">
          <circle cx={160} cy={262} r={86} />
        </clipPath>
      </defs>

      <rect width={CARD_WIDTH} height={CARD_HEIGHT} fill="#050D1C" />
      {/* the sea */}
      <path
        d="M0 560 C 150 530, 300 590, 450 560 S 750 530, 900 560 S 1100 590, 1200 560 L1200 630 L0 630 Z"
        fill="#0B2A4A"
        opacity={0.6}
      />
      <rect x={32} y={32} width={1136} height={566} rx={28} fill="url(#cc-ticket)" stroke="#1E9BD7" strokeWidth={2} />

      {/* perforation between pass and stub */}
      <line x1={858} y1={64} x2={858} y2={566} stroke="#2B4A75" strokeWidth={3} strokeDasharray="2 12" strokeLinecap="round" />
      <circle cx={858} cy={32} r={22} fill="#050D1C" />
      <circle cx={858} cy={598} r={22} fill="#050D1C" />

      {/* header */}
      <text x={72} y={96} fontFamily={SANS} fontSize={26} fontWeight={800} letterSpacing={4} fill="#38BDF8">
        ⚓ CLOUDCAPTAIN
      </text>
      <text x={820} y={96} textAnchor="end" fontFamily={MONO} fontSize={16} letterSpacing={3} fill="#64748B">
        CREW MANIFEST · BOARDING PASS
      </text>
      <line x1={72} y1={120} x2={820} y2={120} stroke="#1B3358" strokeWidth={2} />

      {/* avatar */}
      <circle cx={160} cy={262} r={96} fill="url(#cc-ring)" />
      <circle cx={160} cy={262} r={89} fill="#0A1628" />
      {avatarData ? (
        <image href={avatarData} x={74} y={176} width={172} height={172} clipPath="url(#cc-avatar)" preserveAspectRatio="xMidYMid slice" />
      ) : (
        <text x={160} y={262} textAnchor="middle" dominantBaseline="middle" fontFamily={SANS} fontSize={72} fontWeight={800} fill="#38BDF8">
          {member.login.slice(0, 2).toUpperCase()}
        </text>
      )}

      {/* who */}
      <text x={290} y={212} fontFamily={SANS} fontSize={member.login.length > 16 ? 40 : 52} fontWeight={800} fill="#F8FAFC">
        @{clip(member.login, 22)}
      </text>
      <text x={290} y={262} fontFamily={SANS} fontSize={32} fontWeight={700} fill="#38BDF8">
        {rank.emoji} {rank.title.toUpperCase()}
      </text>

      {/* fields */}
      {[
        {label: 'CREW NO.', value: crewNo, x: 290},
        {label: 'VOYAGES', value: String(count), x: 470},
        {label: 'ABOARD SINCE', value: monthYear(first.mergedAt), x: 610},
      ].map((f) => (
        <g key={f.label}>
          <text x={f.x} y={318} fontFamily={MONO} fontSize={14} letterSpacing={2} fill="#64748B">
            {f.label}
          </text>
          <text x={f.x} y={358} fontFamily={MONO} fontSize={34} fontWeight={700} fill="#F8FAFC">
            {f.value}
          </text>
        </g>
      ))}

      {/* latest voyage */}
      <text x={72} y={420} fontFamily={MONO} fontSize={14} letterSpacing={2} fill="#64748B">
        LATEST VOYAGE · PR #{latest.number}
      </text>
      <text x={72} y={456} fontFamily={SANS} fontSize={26} fontWeight={600} fill="#E2E8F0">
        {clip(cleanTitle(latest.title), 52)}
      </text>

      {/* next rank */}
      <text x={72} y={506} fontFamily={MONO} fontSize={14} letterSpacing={2} fill="#64748B">
        {rank.next ? `NEXT RANK · ${rank.next.title.toUpperCase()} IN ${rank.next.needed} PR${rank.next.needed === 1 ? '' : 'S'}` : 'TOP OF THE LADDER'}
      </text>
      <rect x={72} y={520} width={748} height={12} rx={6} fill="#1B3358" />
      <rect x={72} y={520} width={Math.max(12, 748 * progress)} height={12} rx={6} fill="url(#cc-bar)" />
      <text x={72} y={572} fontFamily={MONO} fontSize={16} fill="#94A3B8">
        {host}/crew · Captain @{captainHandle}
      </text>

      {/* stub: ports visited */}
      <text x={1013} y={96} textAnchor="middle" fontFamily={MONO} fontSize={16} letterSpacing={3} fill="#64748B">
        PORTS VISITED
      </text>
      {shown.map((port, i) => (
        <Stamp
          key={port.id}
          port={port}
          x={shown.length === 1 ? 1013 : i % 2 === 0 ? 940 : 1088}
          y={shown.length === 1 ? 300 : 192 + Math.floor(i / 2) * 140}
          color={STAMP_COLORS[i % STAMP_COLORS.length]}
          date={stampDate}
        />
      ))}
    </svg>
  );
});

export default CrewCard;

/** Render the card to a PNG blob (2× for sharp uploads). */
export async function cardToPng(svg: SVGSVGElement, scale = 2): Promise<Blob> {
  const xml = new XMLSerializer().serializeToString(svg);
  const url = URL.createObjectURL(new Blob([xml], {type: 'image/svg+xml;charset=utf-8'}));
  try {
    const img = new Image();
    img.decoding = 'async';
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve();
      img.onerror = () => reject(new Error('could not render the card'));
      img.src = url;
    });
    const canvas = document.createElement('canvas');
    canvas.width = CARD_WIDTH * scale;
    canvas.height = CARD_HEIGHT * scale;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('canvas is not available');
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    return await new Promise<Blob>((resolve, reject) =>
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('could not create the PNG'))), 'image/png'),
    );
  } finally {
    URL.revokeObjectURL(url);
  }
}
