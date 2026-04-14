/**
 * Captain's Bridge — Full DocItem layout replacement.
 *
 * Ejected from @docusaurus/theme-classic/src/theme/DocItem/Layout so we can
 * fully own the doc page structure instead of layering a floating UI on top.
 *
 * When Bridge is enabled (default, user-toggleable via localStorage) this
 * renders a bespoke CSS Grid: telemetry header, left reading gutter, main
 * article, right rail (Contents / Commands / Concepts), and bottom console.
 *
 * When Bridge is disabled we fall back to the stock Docusaurus row layout so
 * classic-mode readers get the vanilla experience. Upstream DocItem updates
 * affect only that fallback branch — the custom branch renders its own cells
 * from unswizzled sub-components (DocItemContent, DocItemFooter, etc.) so
 * markdown, MDX, admonitions, and all plugins continue to work unchanged.
 */
import React, {type ReactNode} from 'react';
import BrowserOnly from '@docusaurus/BrowserOnly';
import type {Props} from '@theme/DocItem/Layout';
import BridgeLayout from '@site/src/components/CaptainsBridge/BridgeLayout';
import ClassicLayout from '@site/src/components/CaptainsBridge/ClassicLayout';

export default function DocItemLayout({children}: Props): ReactNode {
  // SSR renders classic layout so there's no hydration flash and search
  // engines see stable markup. BrowserOnly swaps to BridgeLayout client-side.
  return (
    <BrowserOnly fallback={<ClassicLayout>{children}</ClassicLayout>}>
      {() => <BridgeLayout>{children}</BridgeLayout>}
    </BrowserOnly>
  );
}
