/**
 * Rewrites absolute links to cloudcaptain.io (used in curriculum markdown so
 * it stays valid on GitHub) into internal site links, so Docusaurus renders
 * them as <Link> and the build fails when a target is missing.
 */
const SITE = 'https://cloudcaptain.io';

type Node = {type?: string; url?: string; children?: Node[]};

function visit(node: Node, fn: (n: Node) => void): void {
  fn(node);
  if (Array.isArray(node.children)) {
    for (const child of node.children) visit(child, fn);
  }
}

export default function remarkSiteLinks() {
  return (tree: Node) => {
    visit(tree, (node) => {
      if ((node.type === 'link' || node.type === 'definition') && typeof node.url === 'string') {
        if (node.url === SITE || node.url === `${SITE}/`) node.url = '/';
        else if (node.url.startsWith(`${SITE}/`)) node.url = node.url.slice(SITE.length);
      }
    });
  };
}
