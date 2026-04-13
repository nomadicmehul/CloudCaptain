import React from 'react';
import styles from './styles.module.css';
import type {LabProvider} from './useLabProvider';

type Props = {
  progress: number; // 0..1
  currentSection: number;
  totalSections: number;
  readingTimeLeft: number;
  focus: boolean;
  railOpen: boolean;
  labProvider: LabProvider | null;
  labOpen: boolean;
  onToggleFocus: () => void;
  onToggleRail: () => void;
  onToggleLab: () => void;
  onDisable: () => void;
  onHelp: () => void;
};

export default function BridgeTelemetry({
  progress,
  currentSection,
  totalSections,
  readingTimeLeft,
  focus,
  railOpen,
  labProvider,
  labOpen,
  onToggleFocus,
  onToggleRail,
  onToggleLab,
  onDisable,
  onHelp,
}: Props): JSX.Element {
  const pct = Math.round(progress * 100);
  const sectionLabel =
    totalSections > 0
      ? `${Math.max(1, currentSection + 1).toString().padStart(2, '0')} / ${totalSections
          .toString()
          .padStart(2, '0')}`
      : '—';

  return (
    <div className={styles.telemetry} role="status" aria-label="Captain's Bridge telemetry">
      <div className={styles.telemetryInner}>
        <div className={styles.telemetryLeft}>
          <span className={styles.telemetryBadge} title="Captain's Bridge reading mode">
            ⚓ BRIDGE
          </span>
          <span className={styles.telemetryDivider} />
          <span className={styles.telemetryCell}>
            <span className={styles.telemetryLabel}>progress</span>
            <span className={styles.telemetryValue}>{pct}%</span>
          </span>
          <span className={styles.telemetryDivider} />
          <span className={styles.telemetryCell}>
            <span className={styles.telemetryLabel}>section</span>
            <span className={styles.telemetryValue}>{sectionLabel}</span>
          </span>
          <span className={styles.telemetryDivider} />
          <span className={styles.telemetryCell}>
            <span className={styles.telemetryLabel}>eta</span>
            <span className={styles.telemetryValue}>
              {readingTimeLeft > 0 ? `${readingTimeLeft} min` : 'done'}
            </span>
          </span>
        </div>
        <div className={styles.telemetryRight}>
          {labProvider && (
            <button
              type="button"
              className={`${styles.telemetryBtn} ${styles.telemetryBtnLab} ${
                labOpen ? styles.telemetryBtnOn : ''
              }`}
              onClick={onToggleLab}
              title={`Toggle live ${labProvider.shortLabel} lab (l)`}>
              ⚡ {labOpen ? 'close lab' : `open ${labProvider.shortLabel} lab`}
            </button>
          )}
          <button
            type="button"
            className={`${styles.telemetryBtn} ${focus ? styles.telemetryBtnOn : ''}`}
            onClick={onToggleFocus}
            title="Focus mode (f)">
            {focus ? '◉' : '○'} focus
          </button>
          <button
            type="button"
            className={`${styles.telemetryBtn} ${railOpen ? styles.telemetryBtnOn : ''}`}
            onClick={onToggleRail}
            title="Toggle right rail (t)">
            {railOpen ? '▤' : '▥'} rail
          </button>
          <button type="button" className={styles.telemetryBtn} onClick={onHelp} title="Keyboard help (?)">
            ?
          </button>
          <button
            type="button"
            className={styles.telemetryBtn}
            onClick={onDisable}
            title="Switch to Classic view">
            classic
          </button>
        </div>
      </div>
      <div className={styles.telemetryBar} aria-hidden="true">
        <div className={styles.telemetryBarFill} style={{width: `${pct}%`}} />
      </div>
    </div>
  );
}
