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
        'learning-paths/devops-fundamentals',
        'learning-paths/devops-practices',
        'learning-paths/devops-tools-overview',
        'learning-paths/cloud',
        'learning-paths/containers',
        'learning-paths/ai-ml',
        'learning-paths/sre',
        'learning-paths/sre-practices',
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
        {
          type: 'category',
          label: 'Docker',
          collapsed: true,
          link: { type: 'doc', id: 'tools/docker/index' },
          items: [
            'tools/docker/fundamentals',
            'tools/docker/dockerfile-guide',
            'tools/docker/networking-storage',
            'tools/docker/compose-swarm',
            'tools/docker/security-production',
            'tools/docker/docker-kubernetes',
            'tools/docker/cheatsheet',
            'tools/docker/interview-questions',
          ],
        },
        {
          type: 'category',
          label: 'Podman',
          collapsed: true,
          link: { type: 'doc', id: 'tools/podman/index' },
          items: [
            'tools/podman/fundamentals',
            'tools/podman/cheatsheet',
          ],
        },
      ],
    },
    {
      type: 'category',
      label: 'Orchestration',
      collapsed: false,
      items: [
        {
          type: 'category',
          label: 'Kubernetes',
          collapsed: true,
          link: { type: 'doc', id: 'tools/kubernetes/index' },
          items: [
            'tools/kubernetes/fundamentals',
            'tools/kubernetes/workloads',
            'tools/kubernetes/networking-services',
            'tools/kubernetes/storage',
            'tools/kubernetes/security',
            'tools/kubernetes/production-operations',
            'tools/kubernetes/exam-prep',
            'tools/kubernetes/cheatsheet',
            'tools/kubernetes/interview-questions',
          ],
        },
        'tools/helm/index',
      ],
    },
    {
      type: 'category',
      label: 'Infrastructure as Code',
      items: [
        {
          type: 'category',
          label: 'Terraform',
          collapsed: true,
          link: { type: 'doc', id: 'tools/terraform/index' },
          items: [
            'tools/terraform/fundamentals',
            'tools/terraform/advanced',
            'tools/terraform/cheatsheet',
            'tools/terraform/exam-prep',
            'tools/terraform/interview-questions',
          ],
        },
        {
          type: 'category',
          label: 'Ansible',
          collapsed: true,
          link: { type: 'doc', id: 'tools/ansible/index' },
          items: [
            'tools/ansible/fundamentals',
            'tools/ansible/cheatsheet',
            'tools/ansible/interview-questions',
          ],
        },
        'tools/cloudformation/index',
        'tools/packer/index',
        'tools/vagrant/index',
      ],
    },
    {
      type: 'category',
      label: 'CI/CD',
      items: [
        {
          type: 'category',
          label: 'Jenkins',
          collapsed: true,
          link: { type: 'doc', id: 'tools/jenkins/index' },
          items: [
            'tools/jenkins/fundamentals',
            'tools/jenkins/cheatsheet',
            'tools/jenkins/interview-questions',
          ],
        },
        {
          type: 'category',
          label: 'CI/CD',
          collapsed: true,
          link: { type: 'doc', id: 'tools/cicd/index' },
          items: [
            'tools/cicd/fundamentals',
            'tools/cicd/gitops',
            'tools/cicd/cheatsheet',
            'tools/cicd/interview-questions',
          ],
        },
        {
          type: 'category',
          label: 'GitHub Actions',
          collapsed: true,
          link: { type: 'doc', id: 'tools/github-actions/index' },
          items: [
            'tools/github-actions/fundamentals',
            'tools/github-actions/cheatsheet',
          ],
        },
        'tools/circleci/index',
      ],
    },
    {
      type: 'category',
      label: 'Fundamentals',
      items: [
        {
          type: 'category',
          label: 'Linux',
          collapsed: true,
          link: { type: 'doc', id: 'tools/linux/index' },
          items: [
            'tools/linux/fundamentals',
            'tools/linux/administration',
            'tools/linux/networking',
            'tools/linux/security',
            'tools/linux/cheatsheet',
            'tools/linux/interview-questions',
          ],
        },
        {
          type: 'category',
          label: 'Git',
          collapsed: true,
          link: { type: 'doc', id: 'tools/git/index' },
          items: [
            'tools/git/fundamentals',
            'tools/git/advanced',
            'tools/git/cheatsheet',
            'tools/git/interview-questions',
          ],
        },
        {
          type: 'category',
          label: 'Bash',
          collapsed: true,
          link: { type: 'doc', id: 'tools/bash/index' },
          items: [
            'tools/bash/fundamentals',
            'tools/bash/advanced',
            'tools/bash/cheatsheet',
            'tools/bash/interview-questions',
          ],
        },
        {
          type: 'category',
          label: 'Python',
          collapsed: true,
          link: { type: 'doc', id: 'tools/python/index' },
          items: [
            'tools/python/fundamentals',
            'tools/python/devops-scripting',
            'tools/python/cheatsheet',
            'tools/python/interview-questions',
          ],
        },
        {
          type: 'category',
          label: 'Networking',
          collapsed: true,
          link: { type: 'doc', id: 'tools/networking/index' },
          items: [
            'tools/networking/fundamentals',
            'tools/networking/routing-switching',
            'tools/networking/security',
            'tools/networking/cheatsheet',
            'tools/networking/interview-questions',
          ],
        },
        {
          type: 'category',
          label: 'YAML',
          collapsed: true,
          link: { type: 'doc', id: 'tools/yaml/index' },
          items: [
            'tools/yaml/fundamentals',
            'tools/yaml/cheatsheet',
          ],
        },
      ],
    },
    {
      type: 'category',
      label: 'Security & Observability',
      items: [
        {
          type: 'category',
          label: 'DevSecOps',
          collapsed: true,
          link: { type: 'doc', id: 'tools/devsecops/index' },
          items: [
            'tools/devsecops/fundamentals',
            'tools/devsecops/cheatsheet',
            'tools/devsecops/interview-questions',
          ],
        },
        'tools/observability/index',
      ],
    },
    {
      type: 'category',
      label: 'Other Tools',
      items: [
        {
          type: 'category',
          label: 'Nginx',
          collapsed: true,
          link: { type: 'doc', id: 'tools/nginx/index' },
          items: [
            'tools/nginx/fundamentals',
            'tools/nginx/cheatsheet',
          ],
        },
        {
          type: 'category',
          label: 'Chef',
          collapsed: true,
          link: { type: 'doc', id: 'tools/chef/index' },
          items: [
            'tools/chef/fundamentals',
            'tools/chef/cheatsheet',
          ],
        },
        'tools/puppet/index',
        {
          type: 'category',
          label: 'Gradle',
          collapsed: true,
          link: { type: 'doc', id: 'tools/gradle/index' },
          items: [
            'tools/gradle/fundamentals',
            'tools/gradle/cheatsheet',
          ],
        },
      ],
    },
  ],
  cloudSidebar: [
    {
      type: 'category',
      label: 'Cloud Providers',
      collapsed: false,
      items: [
        {
          type: 'category',
          label: 'AWS',
          collapsed: true,
          link: { type: 'doc', id: 'cloud/aws/index' },
          items: [
            'cloud/aws/fundamentals',
            'cloud/aws/compute-networking',
            'cloud/aws/security-iam',
            'cloud/aws/devops-cicd',
            'cloud/aws/databases-storage',
            'cloud/aws/exam-prep',
            'cloud/aws/cheatsheet',
            'cloud/aws/interview-questions',
          ],
        },
        {
          type: 'category',
          label: 'Azure',
          collapsed: true,
          link: { type: 'doc', id: 'cloud/azure/index' },
          items: [
            'cloud/azure/fundamentals',
            'cloud/azure/security-identity',
            'cloud/azure/devops',
            'cloud/azure/exam-prep',
            'cloud/azure/cheatsheet',
            'cloud/azure/interview-questions',
          ],
        },
        {
          type: 'category',
          label: 'GCP',
          collapsed: true,
          link: { type: 'doc', id: 'cloud/gcp/index' },
          items: [
            'cloud/gcp/fundamentals',
            'cloud/gcp/architecture-devops',
            'cloud/gcp/exam-prep',
            'cloud/gcp/cheatsheet',
            'cloud/gcp/interview-questions',
          ],
        },
        'cloud/digitalocean/index',
      ],
    },
    {
      type: 'category',
      label: 'Cloud Concepts',
      items: [
        {
          type: 'category',
          label: 'Cloud Computing',
          collapsed: true,
          link: { type: 'doc', id: 'cloud/cloud-computing/index' },
          items: [
            'cloud/cloud-computing/fundamentals',
            'cloud/cloud-computing/architecture',
            'cloud/cloud-computing/migration',
            'cloud/cloud-computing/interview-questions',
          ],
        },
        {
          type: 'category',
          label: 'Multi-Cloud',
          collapsed: true,
          link: { type: 'doc', id: 'cloud/multi-cloud/index' },
          items: [
            'cloud/multi-cloud/fundamentals',
            'cloud/multi-cloud/interview-questions',
          ],
        },
        {
          type: 'category',
          label: 'Cloud Security',
          collapsed: true,
          link: { type: 'doc', id: 'cloud/cloud-security/index' },
          items: [
            'cloud/cloud-security/fundamentals',
            'cloud/cloud-security/interview-questions',
          ],
        },
        {
          type: 'category',
          label: 'FinOps',
          collapsed: true,
          link: { type: 'doc', id: 'cloud/finops/index' },
          items: [
            'cloud/finops/fundamentals',
            'cloud/finops/interview-questions',
          ],
        },
      ],
    },
  ],
};

export default sidebars;
