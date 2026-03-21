import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  learnSidebar: [
    {
      type: 'category',
      label: 'Getting Started',
      collapsed: false,
      items: ['learning-paths/welcome', 'learning-paths/how-to-use'],
    },
    {
      type: 'category',
      label: 'Learning Paths',
      collapsed: false,
      items: [
        'learning-paths/devops',
        'learning-paths/cloud',
        'learning-paths/containers',
        'learning-paths/ai-ml',
        'learning-paths/sre',
        'learning-paths/platform-engineering',
        'learning-paths/linux-master',
      ],
    },
    {
      type: 'category',
      label: 'Career Paths',
      collapsed: false,
      items: [
        'learning-paths/career-paths',
      ],
    },
    {
      type: 'category',
      label: 'Interview Prep',
      items: [
        'interview-prep/devops',
        'interview-prep/docker',
        'interview-prep/kubernetes',
        'interview-prep/terraform',
        'interview-prep/aws',
        'interview-prep/linux',
      ],
    },
  ],
  toolsSidebar: [
    {
      type: 'category',
      label: 'Containers',
      collapsed: false,
      items: [
        'tools/docker/index',
        'tools/docker/commands',
        'tools/docker/notes',
        'tools/docker/exercises',
        'tools/podman/index',
      ],
    },
    {
      type: 'category',
      label: 'Orchestration',
      collapsed: false,
      items: [
        'tools/kubernetes/index',
        'tools/helm/index',
      ],
    },
    {
      type: 'category',
      label: 'Infrastructure as Code',
      items: [
        'tools/terraform/index',
        'tools/terraform/commands',
        'tools/ansible/index',
        'tools/cloudformation/index',
        'tools/packer/index',
        'tools/vagrant/index',
      ],
    },
    {
      type: 'category',
      label: 'CI/CD',
      items: [
        'tools/cicd/index',
        'tools/jenkins/index',
        'tools/github-actions/index',
        'tools/circleci/index',
      ],
    },
    {
      type: 'category',
      label: 'Fundamentals',
      items: [
        'tools/linux/index',
        'tools/git/index',
        'tools/bash/index',
        'tools/python/index',
        'tools/networking/index',
        'tools/yaml/index',
      ],
    },
    {
      type: 'category',
      label: 'Security & Observability',
      items: [
        'tools/devsecops/index',
        'tools/observability/index',
      ],
    },
    {
      type: 'category',
      label: 'Other Tools',
      items: [
        'tools/nginx/index',
        'tools/chef/index',
        'tools/puppet/index',
        'tools/gradle/index',
      ],
    },
  ],
  cloudSidebar: [
    {
      type: 'category',
      label: 'Cloud Providers',
      collapsed: false,
      items: [
        'cloud/aws/index',
        'cloud/azure/index',
        'cloud/gcp/index',
        'cloud/digitalocean/index',
      ],
    },
    {
      type: 'category',
      label: 'Cloud Concepts',
      items: [
        'cloud/cloud-computing/index',
        'cloud/multi-cloud/index',
        'cloud/cloud-security/index',
        'cloud/finops/index',
      ],
    },
  ],
};

export default sidebars;
