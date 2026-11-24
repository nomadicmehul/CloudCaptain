// Seeded, so the server render and the client hydration agree on the order.

// FNV-1a: turns the seed text into a 32-bit number.
function hashSeed(text: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

// mulberry32: a small deterministic generator returning floats in [0, 1).
function mulberry32(seed: number): () => number {
  let a = seed;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const PINNED_LAST = /^(all|none) of the above\.?$/i;

/**
 * Display order for a question's choices, as original indices. The same seed
 * always gives the same order; "All of the above" and "None of the above"
 * stay at the end in their original order.
 */
export function choiceOrder(choices: string[], seed: string): number[] {
  const free: number[] = [];
  const pinned: number[] = [];
  choices.forEach((choice, i) => (PINNED_LAST.test(choice.trim()) ? pinned : free).push(i));
  const random = mulberry32(hashSeed(seed));
  for (let i = free.length - 1; i > 0; i--) {
    const j = Math.floor(random() * (i + 1));
    [free[i], free[j]] = [free[j], free[i]];
  }
  return [...free, ...pinned];
}
