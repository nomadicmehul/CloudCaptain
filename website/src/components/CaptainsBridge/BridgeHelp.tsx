import React, {useEffect} from 'react';
import styles from './styles.module.css';

type Props = {onClose: () => void};

const SHORTCUTS: Array<{keys: string; desc: string}> = [
  {keys: 'j', desc: 'Scroll down half a page'},
  {keys: 'k', desc: 'Scroll up half a page'},
  {keys: 'gg', desc: 'Jump to top'},
  {keys: 'G', desc: 'Jump to bottom'},
  {keys: 'f', desc: 'Toggle focus mode (dims other paragraphs)'},
  {keys: 't', desc: 'Toggle right rail (Contents / Commands / Concepts)'},
  {keys: 'm', desc: 'Mark current section as read'},
  {keys: 'n', desc: 'Next doc in path'},
  {keys: 'p', desc: 'Previous doc in path'},
  {keys: '/', desc: 'Focus search'},
  {keys: '?', desc: 'Toggle this help'},
  {keys: 'Esc', desc: 'Close this help / exit focus mode'},
];

export default function BridgeHelp({onClose}: Props): JSX.Element {
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  return (
    <div className={styles.helpBackdrop} role="dialog" aria-modal="true" aria-labelledby="cc-bridge-help-title" onClick={onClose}>
      <div className={styles.helpModal} onClick={(e) => e.stopPropagation()}>
        <header className={styles.helpHeader}>
          <h2 id="cc-bridge-help-title" className={styles.helpTitle}>
            ⚓ Captain's Bridge — Keyboard Shortcuts
          </h2>
          <button type="button" className={styles.helpClose} onClick={onClose} aria-label="Close help">
            ✕
          </button>
        </header>
        <div className={styles.helpGrid}>
          {SHORTCUTS.map((s) => (
            <div key={s.keys} className={styles.helpRow}>
              <kbd className={styles.helpKbd}>{s.keys}</kbd>
              <span className={styles.helpDesc}>{s.desc}</span>
            </div>
          ))}
        </div>
        <footer className={styles.helpFooter}>
          Tip: right-rail tabs auto-extract <strong>Commands</strong> and <strong>Concepts</strong> from the page.
          Read-progress is saved per page in your browser.
        </footer>
      </div>
    </div>
  );
}
