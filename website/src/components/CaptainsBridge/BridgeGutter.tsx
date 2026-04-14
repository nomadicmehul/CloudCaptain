import React from 'react';
import styles from './styles.module.css';
import type {Section} from './useScrollProgress';

type Props = {
  progress: number;
  sections: Section[];
  isRead: (id: string) => boolean;
};

/**
 * Reading gutter — a thin vertical ribbon along the left edge of the canvas.
 * Fills from top to bottom as you scroll, and overlays small markers for each
 * H2 section (✓ filled = read).
 */
export default function BridgeGutter({progress, sections, isRead}: Props): JSX.Element {
  return (
    <div className={styles.gutter} aria-hidden="true">
      <div className={styles.gutterTrack}>
        <div className={styles.gutterFill} style={{height: `${progress * 100}%`}} />
      </div>
      <div className={styles.gutterMarkers}>
        {sections
          .filter((s) => s.level === 2)
          .map((s, i, arr) => {
            const pos = arr.length > 0 ? (i / Math.max(1, arr.length - 1)) * 100 : 0;
            const read = isRead(s.id);
            return (
              <a
                key={s.id}
                href={`#${s.id}`}
                className={`${styles.gutterMarker} ${read ? styles.gutterMarkerRead : ''}`}
                style={{top: `${pos}%`}}
                title={s.title}>
                <span className={styles.gutterMarkerDot} />
              </a>
            );
          })}
      </div>
    </div>
  );
}
