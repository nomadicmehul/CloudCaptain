import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'CloudCaptain',
  tagline: 'Your Open-Source Learning Hub for Cloud, DevOps, AI & Beyond',
  favicon: 'img/favicon.ico',
  url: 'https://cloudcaptain.io',
  baseUrl: '/',
  organizationName: 'nomadicmehul',
  projectName: 'CloudCaptain',
  trailingSlash: false,
  onBrokenLinks: 'warn',
  onBrokenMarkdownLinks: 'warn',
  markdown: {
    mermaid: true,
  },

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  headTags: [
    // Preconnect to Google Fonts for faster font loading
    {
      tagName: 'link',
      attributes: {
        rel: 'preconnect',
        href: 'https://fonts.googleapis.com',
      },
    },
    {
      tagName: 'link',
      attributes: {
        rel: 'preconnect',
        href: 'https://fonts.gstatic.com',
        crossorigin: 'anonymous',
      },
    },
    // Load fonts with display=swap to avoid render blocking
    {
      tagName: 'link',
      attributes: {
        rel: 'stylesheet',
        href: 'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600&display=swap',
      },
    },
    // JSON-LD structured data for Organization
    {
      tagName: 'script',
      attributes: {
        type: 'application/ld+json',
      },
      innerHTML: JSON.stringify({
        '@context': 'https://schema.org',
        '@type': 'Organization',
        name: 'CloudCaptain',
        url: 'https://cloudcaptain.io',
        logo: 'https://cloudcaptain.io/img/cloudcaptain-logo.jpg',
        description: 'Open-source community learning platform for Cloud, DevOps, AI, and Operations.',
        sameAs: [
          'https://github.com/nomadicmehul/CloudCaptain',
          'https://twitter.com/nomadicmehul',
        ],
      }),
    },
    // JSON-LD structured data for WebSite (enables Google Sitelinks Search Box)
    {
      tagName: 'script',
      attributes: {
        type: 'application/ld+json',
      },
      innerHTML: JSON.stringify({
        '@context': 'https://schema.org',
        '@type': 'WebSite',
        name: 'CloudCaptain',
        url: 'https://cloudcaptain.io',
        description: 'Free open-source learning hub for Cloud, DevOps, AI & Operations with curated paths, hands-on labs, and interview prep.',
        publisher: {
          '@type': 'Organization',
          name: 'CloudCaptain',
        },
      }),
    },
  ],

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          editUrl: 'https://github.com/nomadicmehul/CloudCaptain/tree/main/website/',
          showLastUpdateTime: true,
          showLastUpdateAuthor: false,
          breadcrumbs: true,
        },
        blog: {
          showReadingTime: true,
          editUrl: 'https://github.com/nomadicmehul/CloudCaptain/tree/main/website/',
          blogTitle: 'CloudCaptain Blog',
          blogDescription: 'Latest insights on Cloud, DevOps, AI, and Operations',
          postsPerPage: 10,
          feedOptions: {
            type: 'all',
            title: 'CloudCaptain Blog',
            description: 'Latest insights on Cloud, DevOps, AI, and Operations',
            copyright: `Copyright © ${new Date().getFullYear()} CloudCaptain Community`,
            language: 'en',
          },
        },
        sitemap: {
          changefreq: 'weekly',
          priority: 0.7,
          ignorePatterns: ['/tags/**'],
          filename: 'sitemap.xml',
        },
        // Google Analytics — add your Measurement ID to enable
        // gtag: { trackingID: 'G-XXXXXXXXXX', anonymizeIP: true },
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    image: 'img/cloudcaptain-social.jpg',
    metadata: [
      {name: 'keywords', content: 'cloud computing, devops, kubernetes, docker, aws, azure, gcp, terraform, ansible, linux, learning platform, open-source, interview prep, career paths, hands-on labs'},
      {name: 'description', content: 'CloudCaptain is a free, open-source learning platform for Cloud, DevOps, AI & Operations. Curated paths, hands-on labs, interview prep — 100% free, forever.'},
      {name: 'twitter:card', content: 'summary_large_image'},
      {name: 'twitter:site', content: '@nomadicmehul'},
      {name: 'twitter:creator', content: '@nomadicmehul'},
      {name: 'og:type', content: 'website'},
      {name: 'og:site_name', content: 'CloudCaptain'},
      {name: 'og:locale', content: 'en_US'},
      {name: 'robots', content: 'index, follow, max-image-preview:large, max-snippet:-1, max-video-preview:-1'},
      {name: 'author', content: 'Mehul Patel'},
      {name: 'theme-color', content: '#0A1628'},
    ],
    announcementBar: {
      id: 'contribute',
      content: '$ echo "We are FOSS!" &mdash; Give us a <a target="_blank" rel="noopener noreferrer" href="https://github.com/nomadicmehul/CloudCaptain">&#9733; star on GitHub</a> and join the open-source crew. &#x1F427;',
      backgroundColor: '#0A1628',
      textColor: '#10B981',
      isCloseable: true,
    },
    navbar: {
      title: 'CloudCaptain',
      logo: {
        alt: 'CloudCaptain Logo',
        src: 'img/cloudcaptain-logo.jpg',
        style: { borderRadius: '8px' },
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'learnSidebar',
          position: 'left',
          label: 'Learning Paths',
        },
        {
          type: 'docSidebar',
          sidebarId: 'toolsSidebar',
          position: 'left',
          label: 'Tools & Tech',
        },
        {
          type: 'docSidebar',
          sidebarId: 'cloudSidebar',
          position: 'left',
          label: 'Cloud Providers',
        },
        {to: '/journey', label: "Captain's Journey", position: 'left'},
        {to: '/progress', label: '📖 Progress', position: 'left'},
        {to: '/contribute', label: 'Contribute', position: 'left'},
        {
          href: 'https://buymeacoffee.com/nomadicmehul',
          label: '☕ Buy Me a Coffee',
          position: 'right',
          className: 'navbar__link--sponsor',
        },
        {
          href: 'https://github.com/sponsors/nomadicmehul',
          label: '❤️ Sponsor',
          position: 'right',
          className: 'navbar__link--sponsor',
        },
        {
          type: 'custom-githubStars',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Learn',
          items: [
            {label: 'Career Paths', to: '/career-paths'},
            {label: 'DevOps Path', to: '/docs/learning-paths/devops'},
            {label: 'Linux Master', to: '/docs/learning-paths/linux-master'},
            {label: 'AI/ML Ops', to: '/docs/learning-paths/ai-ml'},
          ],
        },
        {
          title: 'Tools & Tech',
          items: [
            {label: 'Docker', to: '/docs/tools/docker/'},
            {label: 'Kubernetes', to: '/docs/tools/kubernetes/'},
            {label: 'Terraform', to: '/docs/tools/terraform/'},
            {label: 'Ansible', to: '/docs/tools/ansible/'},
          ],
        },
        {
          title: 'Community',
          items: [
            {label: "Captain's Journey", to: '/journey'},
            {label: 'GitHub', href: 'https://github.com/nomadicmehul/CloudCaptain'},
            {label: 'Twitter', href: 'https://twitter.com/nomadicmehul'},
            {label: 'Contributing Guide', to: '/contribute'},
          ],
        },
        {
          title: 'More',
          items: [
            {label: 'Roadmap', to: '/docs/learning-paths/welcome'},
            {label: '📖 Your Progress', to: '/progress'},
            {label: 'Blog', to: '/blog'},
            {label: '☕ Buy Me a Coffee', href: 'https://buymeacoffee.com/nomadicmehul'},
            {label: '❤️ Sponsor on GitHub', href: 'https://github.com/sponsors/nomadicmehul'},
          ],
        },
      ],
      copyright: `\u00A9 ${new Date().getFullYear()} CloudCaptain Community &mdash; For the love of DevOps, Cloud Native & Open Source by <a href="https://nomadicmehul.dev/" target="_blank" rel="noopener noreferrer" style="color: #38BDF8;">Mehul Patel</a>`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['bash', 'yaml', 'json', 'docker', 'hcl', 'python'],
    },
    colorMode: {
      defaultMode: 'dark',
      disableSwitch: false,
      respectPrefersColorScheme: true,
    },
    tableOfContents: {
      minHeadingLevel: 2,
      maxHeadingLevel: 4,
    },
  } satisfies Preset.ThemeConfig,

  plugins: [
    // Zoom on images in docs/blog for cheatsheets and diagrams
    [
      'plugin-image-zoom',
      {
        selector: '.markdown img',
        options: {
          margin: 24,
          background: 'rgba(10, 22, 40, 0.9)',
          scrollOffset: 0,
        },
      },
    ],
  ],

  themes: [
    '@docusaurus/theme-mermaid',
    [
      require.resolve('@easyops-cn/docusaurus-search-local'),
      {
        hashed: true,
        language: ['en'],
        highlightSearchTermsOnTargetPage: true,
        explicitSearchResultPath: true,
        indexBlog: true,
        docsRouteBasePath: '/docs',
      },
    ],
  ],
};

export default config;
