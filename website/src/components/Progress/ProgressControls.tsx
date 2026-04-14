import React, {useRef, useState} from 'react';
import styles from './styles.module.css';
import {
  clearAllReadEntries,
  exportReadEntries,
  importReadEntries,
} from './readProgress';
import {hasDemoSeed, seedDemoProgress, unseedDemoProgress} from './seedDemo';

type Props = {
  onChange: () => void;
  hasEntries: boolean;
};

export default function ProgressControls({onChange, hasEntries}: Props): JSX.Element {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [confirmReset, setConfirmReset] = useState(false);
  const [confirmText, setConfirmText] = useState('');
  const [seeded, setSeeded] = useState<boolean>(() => hasDemoSeed());

  const flash = (m: string) => {
    setMessage(m);
    window.setTimeout(() => setMessage(null), 4000);
  };

  const doExport = () => {
    const payload = exportReadEntries();
    const blob = new Blob([JSON.stringify(payload, null, 2)], {
      type: 'application/json',
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const today = new Date().toISOString().slice(0, 10);
    a.download = `cloudcaptain-progress-${today}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    flash(`Exported ${Object.keys(payload.entries).length} entries.`);
  };

  const doImport = (mode: 'merge' | 'replace') => {
    const input = fileInputRef.current;
    if (!input) return;
    input.onchange = () => {
      const file = input.files?.[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        try {
          const parsed = JSON.parse(String(reader.result));
          const {imported, skipped, errors} = importReadEntries(parsed, mode);
          if (errors.length) flash(`Imported ${imported}, skipped ${skipped}. Errors: ${errors[0]}`);
          else flash(`Imported ${imported} entries (skipped ${skipped}).`);
          onChange();
        } catch (e) {
          flash(`Import failed: ${(e as Error).message}`);
        }
      };
      reader.readAsText(file);
      input.value = '';
    };
    input.click();
  };

  const doReset = () => {
    if (confirmText !== 'DELETE') {
      flash('Type DELETE in the confirmation box to proceed.');
      return;
    }
    const n = clearAllReadEntries();
    setConfirmReset(false);
    setConfirmText('');
    setSeeded(false);
    flash(`Removed ${n} read entries.`);
    onChange();
  };

  const doSeed = () => {
    const {created, skipped} = seedDemoProgress();
    setSeeded(true);
    flash(`Seeded ${created} demo entries${skipped ? ` (skipped ${skipped} existing)` : ''}.`);
    onChange();
  };

  const doUnseed = () => {
    const n = unseedDemoProgress();
    setSeeded(false);
    flash(`Removed ${n} demo entries.`);
    onChange();
  };

  return (
    <section className={styles.controls} aria-label="Progress controls">
      <h2 className={styles.sectionHeading}>Manage</h2>

      <div className={styles.controlsGrid}>
        <div className={styles.controlCard}>
          <h3 className={styles.controlCardTitle}>⬇ Export</h3>
          <p className={styles.controlCardBody}>
            Download your read-progress as JSON. Safe to share or back up.
          </p>
          <button
            type="button"
            className={styles.controlBtn}
            disabled={!hasEntries}
            onClick={doExport}>
            Export as JSON
          </button>
        </div>

        <div className={styles.controlCard}>
          <h3 className={styles.controlCardTitle}>⬆ Import</h3>
          <p className={styles.controlCardBody}>
            Merge or replace read-progress from a previously exported JSON file.
          </p>
          <div className={styles.controlBtnRow}>
            <button type="button" className={styles.controlBtn} onClick={() => doImport('merge')}>
              Merge
            </button>
            <button
              type="button"
              className={`${styles.controlBtn} ${styles.controlBtnGhost}`}
              onClick={() => doImport('replace')}>
              Replace all
            </button>
          </div>
          <input
            ref={fileInputRef}
            type="file"
            accept="application/json,.json"
            hidden
            aria-hidden="true"
          />
        </div>

        <div className={styles.controlCard}>
          <h3 className={styles.controlCardTitle}>🪄 Demo data</h3>
          <p className={styles.controlCardBody}>
            Pre-fill the dashboard with sample progress so you can see what it looks like. Skips any
            pages where you already have real progress.
          </p>
          {seeded ? (
            <button type="button" className={styles.controlBtn} onClick={doUnseed}>
              Remove demo data
            </button>
          ) : (
            <button type="button" className={styles.controlBtn} onClick={doSeed}>
              Seed demo data
            </button>
          )}
        </div>

        <div className={`${styles.controlCard} ${styles.controlCardDanger}`}>
          <h3 className={styles.controlCardTitle}>↻ Reset</h3>
          <p className={styles.controlCardBody}>
            Deletes every read-progress entry from this browser. Irreversible.
          </p>
          {confirmReset ? (
            <div className={styles.controlConfirm}>
              <input
                type="text"
                className={styles.controlInput}
                placeholder="Type DELETE to confirm"
                value={confirmText}
                onChange={(e) => setConfirmText(e.target.value)}
                autoFocus
              />
              <div className={styles.controlBtnRow}>
                <button
                  type="button"
                  className={`${styles.controlBtn} ${styles.controlBtnDanger}`}
                  disabled={confirmText !== 'DELETE'}
                  onClick={doReset}>
                  Delete all
                </button>
                <button
                  type="button"
                  className={`${styles.controlBtn} ${styles.controlBtnGhost}`}
                  onClick={() => {
                    setConfirmReset(false);
                    setConfirmText('');
                  }}>
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <button
              type="button"
              className={`${styles.controlBtn} ${styles.controlBtnDanger}`}
              disabled={!hasEntries}
              onClick={() => setConfirmReset(true)}>
              Reset everything
            </button>
          )}
        </div>
      </div>

      {message && (
        <div className={styles.controlsMessage} role="status" aria-live="polite">
          {message}
        </div>
      )}

      <p className={styles.privacyNote}>
        🔒 Progress is stored in your browser's localStorage only. CloudCaptain's servers never
        receive it. Clearing your browser data erases it.
      </p>
    </section>
  );
}
