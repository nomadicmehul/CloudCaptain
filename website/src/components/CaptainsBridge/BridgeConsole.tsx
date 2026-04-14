import React from 'react';
import styles from './styles.module.css';

type Props = {
  onMarkRead: () => void;
  onPrev: () => void;
  onNext: () => void;
  onFocus: () => void;
  onHelp: () => void;
  readCount: number;
  totalSections: number;
};

export default function BridgeConsole({
  onMarkRead,
  onPrev,
  onNext,
  onFocus,
  onHelp,
  readCount,
  totalSections,
}: Props): JSX.Element {
  return (
    <div className={styles.console} role="toolbar" aria-label="Captain's Bridge console">
      <div className={styles.consoleInner}>
        <div className={styles.consoleGroup}>
          <Key label="j" desc="down" />
          <Key label="k" desc="up" />
          <Key label="gg" desc="top" />
          <Key label="G" desc="bottom" />
        </div>
        <div className={styles.consoleGroup}>
          <button type="button" className={styles.consoleBtn} onClick={onFocus} title="Focus mode (f)">
            <kbd className={styles.consoleKbd}>f</kbd>
            focus
          </button>
          <button type="button" className={styles.consoleBtn} onClick={onMarkRead} title="Mark current section read (m)">
            <kbd className={styles.consoleKbd}>m</kbd>
            mark read
          </button>
          <span className={styles.consoleRead} title="Sections marked as read on this page">
            {readCount} / {totalSections} read
          </span>
        </div>
        <div className={styles.consoleGroup}>
          <button type="button" className={styles.consoleBtn} onClick={onPrev} title="Previous doc (p)">
            <kbd className={styles.consoleKbd}>p</kbd>
            prev
          </button>
          <button type="button" className={styles.consoleBtn} onClick={onNext} title="Next doc (n)">
            <kbd className={styles.consoleKbd}>n</kbd>
            next
          </button>
          <button type="button" className={styles.consoleBtn} onClick={onHelp} title="Keyboard help (?)">
            <kbd className={styles.consoleKbd}>?</kbd>
            help
          </button>
        </div>
      </div>
    </div>
  );
}

function Key({label, desc}: {label: string; desc: string}) {
  return (
    <span className={styles.consoleKey} title={desc}>
      <kbd className={styles.consoleKbd}>{label}</kbd>
      <span className={styles.consoleKeyDesc}>{desc}</span>
    </span>
  );
}
