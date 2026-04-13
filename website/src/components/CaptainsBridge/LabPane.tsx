/**
 * LabPane — the right-pane live sandbox.
 *
 * Phase 1: single iframe to the KillerCoda playground for this doc's tool.
 * Lazy-mounts (iframe only appears after first user interaction / toggle on)
 * so pages without lab-mode don't pay the iframe cost.
 *
 * The iframe is throw-away state — we don't persist terminal history.
 * Reload button resets the lab by re-assigning `iframe.src`.
 */
import React, {useEffect, useRef, useState} from 'react';
import styles from './styles.module.css';
import type {LabProvider} from './useLabProvider';

type Props = {
  provider: LabProvider;
  onClose: () => void;
};

const FIRST_RUN_KEY = 'cloudcaptain.bridge.lab.seenIntro';

export default function LabPane({provider, onClose}: Props): JSX.Element {
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const [showIntro, setShowIntro] = useState(false);
  const [reloadNonce, setReloadNonce] = useState(0);

  useEffect(() => {
    try {
      if (!localStorage.getItem(FIRST_RUN_KEY)) setShowIntro(true);
    } catch {}
  }, [provider.id]);

  const dismissIntro = () => {
    setShowIntro(false);
    try {
      localStorage.setItem(FIRST_RUN_KEY, '1');
    } catch {}
  };

  const reload = () => setReloadNonce((n) => n + 1);

  return (
    <aside
      className={styles.lab}
      aria-label={`Live sandbox: ${provider.name}`}
      role="complementary">
      <header className={styles.labHeader}>
        <div className={styles.labHeaderLeft}>
          <span className={styles.labIcon} aria-hidden="true">
            ⚡
          </span>
          <div className={styles.labTitleWrap}>
            <span className={styles.labKicker}>LIVE LAB</span>
            <span className={styles.labTitle}>{provider.name}</span>
          </div>
        </div>
        <div className={styles.labHeaderActions}>
          <button
            type="button"
            className={styles.labIconBtn}
            onClick={reload}
            title="Reload lab session"
            aria-label="Reload lab">
            ↻
          </button>
          <a
            className={styles.labIconBtn}
            href={provider.url}
            target="_blank"
            rel="noopener noreferrer"
            title="Open lab in new tab"
            aria-label="Open lab in new tab">
            ↗
          </a>
          <button
            type="button"
            className={styles.labIconBtn}
            onClick={onClose}
            title="Close lab (l)"
            aria-label="Close lab">
            ✕
          </button>
        </div>
      </header>

      <div className={styles.labBody}>
        {showIntro && (
          <div className={styles.labIntro} role="alertdialog">
            <div className={styles.labIntroBody}>
              <h3 className={styles.labIntroTitle}>First time in the Lab? ⚡</h3>
              <p className={styles.labIntroText}>
                This pane embeds a live sandbox from <strong>KillerCoda</strong>, an
                independent third-party service. When you activate it:
              </p>
              <ul className={styles.labIntroList}>
                <li>A real Linux/Kubernetes/Docker environment spins up in your browser.</li>
                <li>KillerCoda's cookies & privacy policy apply inside the iframe.</li>
                <li>Your session is ephemeral — nothing is saved when you close the tab.</li>
                <li>Commands run on KillerCoda's servers, not CloudCaptain's.</li>
              </ul>
              <p className={styles.labIntroText}>
                <a
                  href="https://killercoda.com/privacy"
                  target="_blank"
                  rel="noopener noreferrer">
                  KillerCoda privacy policy ↗
                </a>
              </p>
              <div className={styles.labIntroActions}>
                <button
                  type="button"
                  className={styles.labIntroPrimary}
                  onClick={dismissIntro}>
                  Got it — launch lab
                </button>
                <button
                  type="button"
                  className={styles.labIntroSecondary}
                  onClick={onClose}>
                  Cancel
                </button>
              </div>
            </div>
          </div>
        )}

        {!showIntro && (
          <iframe
            key={reloadNonce}
            ref={iframeRef}
            src={provider.url}
            title={provider.name}
            className={styles.labFrame}
            /* Permissive sandbox: labs need same-origin + scripts to function */
            sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-modals allow-pointer-lock allow-top-navigation-by-user-activation"
            allow="clipboard-read; clipboard-write; fullscreen"
            referrerPolicy="no-referrer-when-downgrade"
            loading="eager"
          />
        )}
      </div>

      <footer className={styles.labFooter}>
        <span>
          {provider.attribution} ·{' '}
          <a
            href={provider.attributionUrl}
            target="_blank"
            rel="noopener noreferrer">
            killercoda.com
          </a>
        </span>
        <span className={styles.labFooterBeta}>PHASE 1 MVP</span>
      </footer>
    </aside>
  );
}
