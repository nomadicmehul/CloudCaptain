/**
 * GitHub Pages serves this site with `trailingSlash: false`: every page is
 * built as `path.html`, so a request for `/path/` used to answer 404 (the
 * client router then rendered the page anyway, which hid the problem from
 * people but not from crawlers, link previews or anyone without JavaScript).
 *
 * After the build this copies every `path.html` to `path/index.html`, so
 * `/path/` serves the same page with a 200. A copy rather than a redirect
 * page, because servers disagree about which file answers `/path` when both
 * exist (`docusaurus serve` picks the folder index, GitHub Pages the .html
 * file); a redirect could loop, a copy cannot. Every page already declares its
 * canonical `/path` URL, so search engines index one address. Existing files
 * are never overwritten.
 */
import fs from 'node:fs';
import path from 'node:path';
import type {Plugin} from '@docusaurus/types';

export default function trailingSlashPages(): Plugin {
  return {
    name: 'trailing-slash-pages',
    async postBuild({outDir, routesPaths}) {
      let written = 0;
      for (const route of new Set(routesPaths)) {
        if (route === '/' || route.endsWith('/') || path.extname(route) !== '') continue;
        const flat = path.join(outDir, `${route}.html`);
        const nested = path.join(outDir, route, 'index.html');
        if (!fs.existsSync(flat) || fs.existsSync(nested)) continue;
        fs.mkdirSync(path.dirname(nested), {recursive: true});
        fs.copyFileSync(flat, nested);
        written += 1;
      }
      console.log(`[trailing-slash-pages] copied ${written} page(s) so "/path/" serves the same page as "/path"`);
    },
  };
}
