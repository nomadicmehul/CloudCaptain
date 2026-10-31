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
import {useDoc} from '@docusaurus/plugin-content-docs/client';
import FromScratchCard from '@site/src/components/Curriculum/FromScratchCard';
import BridgeLayout from '@site/src/components/CaptainsBridge/BridgeLayout';
import ClassicLayout from '@site/src/components/CaptainsBridge/ClassicLayout';

export default function DocItemLayout({children}: Props): ReactNode {
  // Curriculum landing and learning-path pages are overview pages, not reading
  // material: render them in the classic layout without the Bridge chrome.
  const {frontMatter} = useDoc();
  const kind = (frontMatter as {cc_kind?: string}).cc_kind;
  if (kind === 'landing' || kind === 'path') {
    return <ClassicLayout>{children}</ClassicLayout>;
  }
  // Reference docs that a lesson builds on get a "Build this from scratch" card.
  const content = (
    <>
      <FromScratchCard />
      {children}
    </>
  );
  // SSR renders classic layout so there's no hydration flash and search
  // engines see stable markup. BrowserOnly swaps to BridgeLayout client-side.
  return (
    <BrowserOnly fallback={<ClassicLayout>{content}</ClassicLayout>}>
      {() => <BridgeLayout>{content}</BridgeLayout>}
    </BrowserOnly>
  );
}
