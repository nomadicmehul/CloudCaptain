/**
 * SidebarSpine — annotates every doc link in the left sidebar with a
 * progress dot based on localStorage read-state:
 *
 *   ○  no progress
 *   ◐  some sections read, not complete
 *   ●  all sections read (complete)
 *
 * Works across ALL docs the user has visited, giving a "cross-page progress
 * trail" along the sidebar. Re-queries when:
 *   - the sidebar DOM changes (route change, sidebar expand/collapse)
 *   - read-progress changes (dispatched custom event from useReadProgress)
 *   - the tab becomes visible again
 */
import {useEffect} from 'react';
import {useLocation} from '@docusaurus/router';
import {getAllReadEntries} from './useReadProgress';

const BADGE_CLASS = 'cc-bridge-spine-badge';

function dotFor(readCount: number, total: number | undefined): {char: string; cls: string} {
  if (!total || total <= 0) return {char: '◐', cls: 'partial'};
  if (readCount >= total) return {char: '●', cls: 'complete'};
  return {char: '◐', cls: 'partial'};
}

function paintSpine(): void {
  const entries = getAllReadEntries();
  if (!entries.size) {
    // Remove any stale badges
    document.querySelectorAll(`.${BADGE_CLASS}`).forEach((n) => n.remove());
    return;
  }
  // Consider both classic sidebar menu links and mobile drawer links
  const links = Array.from(
    document.querySelectorAll<HTMLAnchorElement>(
      'nav.menu .menu__link[href^="/docs/"], nav.theme-doc-sidebar-menu .menu__link[href^="/docs/"]'
    )
  );
  const seen = new WeakSet<Element>();
  for (const link of links) {
    const href = link.getAttribute('href');
    if (!href) continue;
    // Normalize trailing slash
    const normalized = href.replace(/\/$/, '') || '/';
    const candidates = [href, normalized, `${normalized}/`];
    let entry;
    for (const c of candidates) {
      entry = entries.get(c);
      if (entry) break;
    }
    let existing = link.querySelector(`.${BADGE_CLASS}`) as HTMLElement | null;
    if (!entry || entry.readCount === 0) {
      if (existing) existing.remove();
      continue;
    }
    const {char, cls} = dotFor(entry.readCount, entry.totalSections);
    if (!existing) {
      existing = document.createElement('span');
      existing.className = BADGE_CLASS;
      link.appendChild(existing);
    }
    existing.textContent = char;
    existing.dataset.state = cls;
    existing.title = entry.totalSections
      ? `${entry.readCount} / ${entry.totalSections} sections read`
      : `${entry.readCount} sections read`;
    seen.add(existing);
  }
  // Remove badges on links that no longer have an entry (e.g. after clear)
  document
    .querySelectorAll<HTMLElement>(`.${BADGE_CLASS}`)
    .forEach((el) => {
      if (!seen.has(el)) el.remove();
    });
}

export default function SidebarSpine(): null {
  const location = useLocation();
  useEffect(() => {
    let rafId = 0;
    const schedule = () => {
      cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(paintSpine);
    };
    // Initial paint after route settles
    const initialTimer = window.setTimeout(schedule, 120);
    // Observe sidebar mutations (items render/collapse/expand)
    const sidebar = document.querySelector('nav.theme-doc-sidebar-menu, nav.menu');
    const mo = sidebar
      ? new MutationObserver(() => schedule())
      : null;
    if (sidebar && mo) mo.observe(sidebar, {childList: true, subtree: true});

    // Listen for read-state updates from this tab (custom) or other tabs (storage)
    const onChange = () => schedule();
    window.addEventListener('cc-bridge-read-change', onChange);
    window.addEventListener('storage', onChange);
    const onVisibility = () => {
      if (!document.hidden) schedule();
    };
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      window.clearTimeout(initialTimer);
      cancelAnimationFrame(rafId);
      if (mo) mo.disconnect();
      window.removeEventListener('cc-bridge-read-change', onChange);
      window.removeEventListener('storage', onChange);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [location.pathname]);
  return null;
}
