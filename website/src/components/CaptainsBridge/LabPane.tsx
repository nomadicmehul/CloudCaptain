/**
 * LabPane — the right-pane sandbox launcher.
 *
 * Phase 1 renders a LAUNCHER CARD for all providers (KillerCoda blocks
 * iframe embedding via X-Frame-Options, as do most modern sandbox
 * services). The card shows provider identity, what the lab gives you,
 * a primary "Launch in new tab" CTA, and attribution.
 *
 * Infrastructure for iframe mode is retained for Phase 3 when we add
 * truly embeddable providers (WebVM, Pyodide, StackBlitz).
 */
import React, {useRef, useState} from 'react';
import styles from './styles.module.css';
import type {LabProvider} from './useLabProvider';

type Props = {
  provider: LabProvider;
  onClose: () => void;
};

export default function LabPane({provider, onClose}: Props): JSX.Element {
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [launched, setLaunched] = useState(false);

  const reload = () => setReloadNonce((n) => n + 1);

  const launch = () => {
    const w = window.open(provider.url, '_blank', 'noopener,noreferrer');
    if (w) setLaunched(true);
  };

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
          {provider.mode === 'iframe' && (
            <button
              type="button"
              className={styles.labIconBtn}
              onClick={reload}
              title="Reload lab session"
              aria-label="Reload lab">
              ↻
            </button>
          )}
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
        {provider.mode === 'launcher' ? (
          <div className={styles.labLauncher}>
            <div className={styles.labLauncherInner}>
              <div className={styles.labLauncherTerminal} aria-hidden="true">
                <div className={styles.labLauncherTerminalBar}>
                  <span />
                  <span />
                  <span />
                  <span className={styles.labLauncherTerminalTitle}>
                    {provider.shortLabel.toLowerCase()}-lab ~ #
                  </span>
                </div>
                <div className={styles.labLauncherTerminalBody}>
                  <div>
                    <span className={styles.labLauncherPrompt}>$</span> cat welcome.md
                  </div>
                  <div className={styles.labLauncherDim}>
                    # {provider.name}
                  </div>
                  <div className={styles.labLauncherDim}>
                    # {provider.attribution.toLowerCase()}
                  </div>
                  <div>
                    <span className={styles.labLauncherPrompt}>$</span> launch --new-tab
                    <span className={styles.labLauncherCaret}>▍</span>
                  </div>
                </div>
              </div>

              <h3 className={styles.labLauncherHeading}>
                Your lab is ready to launch
              </h3>
              <p className={styles.labLauncherDesc}>{provider.description}</p>

              <button
                type="button"
                className={styles.labLauncherPrimary}
                onClick={launch}>
                🚀 Launch in new tab
              </button>

              {launched && (
                <p className={styles.labLauncherHint}>
                  ✓ Opened in a new tab. Come back here to read alongside.
                </p>
              )}

              <details className={styles.labLauncherDetails}>
                <summary>Why does this open in a new tab?</summary>
                <p>
                  {provider.attribution.replace('Powered by ', '')} doesn't allow
                  embedding as an iframe inside other sites (a common
                  anti-clickjacking policy). Launching in a new tab preserves
                  your reading progress here and gives you full terminal
                  capabilities over there. Use window snapping or a wide
                  display to read + practice side-by-side.
                </p>
              </details>
            </div>
          </div>
        ) : (
          <iframe
            key={reloadNonce}
            ref={iframeRef}
            src={provider.embedUrl ?? provider.url}
            title={provider.name}
            className={styles.labFrame}
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
            learn more ↗
          </a>
        </span>
        <span className={styles.labFooterBeta}>PHASE 1</span>
      </footer>
    </aside>
  );
}
