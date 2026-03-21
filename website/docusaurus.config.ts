import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';

const config: Config = {
  title: 'CloudCaptain',
  tagline: 'Your Open-Source Learning Hub for Cloud, DevOps, AI & Beyond',
  favicon: 'img/favicon.ico',
  url: 'https://nomadicmehul.github.io',
  baseUrl: '/CloudCaptain/',
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

  presets: [
    [
      'classic',
      {
        docs: {
          sidebarPath: './sidebars.ts',
          editUrl: 'https://github.com/nomadicmehul/CloudCaptain/tree/main/website/',
          showLastUpdateTime: false,
          showLastUpdateAuthor: false,
          breadcrumbs: true,
        },
        blog: {
          showReadingTime: true,
          editUrl: 'https://github.com/nomadicmehul/CloudCaptain/tree/main/website/',
          blogTitle: 'CloudCaptain Blog',
          blogDescription: 'Latest insights on Cloud, DevOps, AI, and Operations',
          postsPerPage: 10,
        },
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    image: 'img/cloudcaptain-social.jpg',
    metadata: [
      {name: 'keywords', content: 'cloud, devops, kubernetes, docker, aws, azure, gcp, learning, open-source'},
      {name: 'twitter:card', content: 'summary_large_image'},
      {name: 'twitter:site', content: '@nomadicmehul'},
    ],
    announcementBar: {
      id: 'contribute',
      content: 'We are open source! Give us a <a target="_blank" rel="noopener noreferrer" href="https://github.com/nomadicmehul/CloudCaptain">star on GitHub</a> and contribute to the community.',
      backgroundColor: '#0A1628',
      textColor: '#38BDF8',
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
        {to: '/blog', label: 'Blog', position: 'left'},
        {to: '/contribute', label: 'Contribute', position: 'left'},
        {
          href: 'https://github.com/nomadicmehul/CloudCaptain',
          label: 'GitHub',
          position: 'right',
        },
        {
          href: 'https://twitter.com/nomadicmehul',
          label: 'Twitter',
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
            {label: 'Career Paths', to: '/docs/learning-paths/career-paths'},
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
            {label: 'GitHub', href: 'https://github.com/nomadicmehul/CloudCaptain'},
            {label: 'Twitter', href: 'https://twitter.com/nomadicmehul'},
            {label: 'Contributing Guide', to: '/contribute'},
          ],
        },
        {
          title: 'More',
          items: [
            {label: 'Blog', to: '/blog'},
            {label: 'Roadmap', to: '/docs/learning-paths/devops'},
            {label: 'Credits', href: 'https://github.com/nomadicmehul/CloudCaptain/blob/main/credits.md'},
          ],
        },
      ],
      copyright: `Copyright \u00A9 ${new Date().getFullYear()} CloudCaptain Community. Built with love by the open-source community.`,
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
