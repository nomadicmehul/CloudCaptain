import React, {useEffect, useState} from 'react';
import styles from './styles.module.css';

type Props = {
  message: string;
  onDone: () => void;
  durationMs?: number;
};

/**
 * CompletionToast — fires a brief celebratory toast when a section is marked
 * complete. Re-renders on new message (parent passes a fresh key via changing
 * message prop + re-mount via conditional render).
 */
export default function CompletionToast({
  message,
  onDone,
  durationMs = 2400,
}: Props): JSX.Element {
  const [phase, setPhase] = useState<'in' | 'hold' | 'out'>('in');

  useEffect(() => {
    const t1 = window.setTimeout(() => setPhase('hold'), 180);
    const t2 = window.setTimeout(() => setPhase('out'), durationMs - 280);
    const t3 = window.setTimeout(() => onDone(), durationMs);
    return () => {
      window.clearTimeout(t1);
      window.clearTimeout(t2);
      window.clearTimeout(t3);
    };
  }, [message, durationMs, onDone]);

  return (
    <div
      className={`${styles.toast} ${styles[`toast_${phase}`]}`}
      role="status"
      aria-live="polite">
      <div className={styles.toastInner}>
        <span className={styles.toastBurst} aria-hidden="true">
          <span />
          <span />
          <span />
          <span />
          <span />
        </span>
        <span className={styles.toastText}>{message}</span>
      </div>
    </div>
  );
}
