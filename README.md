<div align="center">
  <img src="./website/static/img/cloudcaptain-logo.jpg" width="200" />
  <h1>CloudCaptain</h1>
  <p><strong>The Open-Source Learning Hub for Cloud, DevOps, AI & Operations</strong></p>
  <p>
    <a href="https://nomadicmehul.github.io/CloudCaptain/"><img src="https://img.shields.io/badge/Website-Live-1E9BD7?style=flat&logo=docusaurus" /></a>
    <img src="https://img.shields.io/badge/PRs-welcome-blue" />
    <img src="https://img.shields.io/github/last-commit/nomadicmehul/CloudCaptain" />
    <a href="https://twitter.com/intent/follow?screen_name=nomadicmehul"><img src="https://img.shields.io/twitter/follow/nomadicmehul?style=flat&logo=twitter" /></a>
    <a href="https://github.com/nomadicmehul"><img src="https://img.shields.io/github/stars/nomadicmehul/cloudcaptain?style=flat&logo=github" /></a>
  </p>
</div>

---

## What is CloudCaptain?

CloudCaptain is a community-driven, open-source learning platform with **130+ comprehensive documentation pages**, **49 architecture diagrams**, **8 career paths**, and **7 structured learning roadmaps** covering Cloud, DevOps, AI, and Operations — all free, forever.

Every page includes hands-on exercises, command cheat sheets, interview Q&A, and exam prep guides. Whether you're a complete beginner or a seasoned professional, CloudCaptain has something for you.

**[Visit the Website](https://nomadicmehul.github.io/CloudCaptain/)**

## What's Inside

### Tools & Technology (28 categories)

| Category | Topics Covered |
|:---------|:---------------|
| **Containers** | Docker (8 guides), Podman |
| **Orchestration** | Kubernetes (9 guides), Helm |
| **Infrastructure as Code** | Terraform (5 guides), Ansible (3 guides), CloudFormation, Packer, Vagrant |
| **CI/CD** | Jenkins (3 guides), CI/CD Pipelines (4 guides), GitHub Actions, CircleCI, GitOps |
| **Languages & Scripting** | Linux (6 guides), Git (4 guides), Bash (4 guides), Python (4 guides), YAML |
| **Networking** | Fundamentals, Routing & Switching, Security, 100+ commands cheat sheet |
| **Security** | DevSecOps (3 guides) |
| **Web & Build** | Nginx, Chef, Gradle |

### Cloud Providers

| Provider | Topics Covered |
|:---------|:---------------|
| **AWS** | Fundamentals, Compute & Networking, Security & IAM, DevOps & CI/CD, Databases & Storage, Exam Prep, Cheat Sheet, Interview Q&A |
| **Azure** | Fundamentals, Security & Identity, DevOps, Exam Prep (AZ-900, AZ-104), Cheat Sheet, Interview Q&A |
| **GCP** | Fundamentals, Architecture & DevOps, Exam Prep, Cheat Sheet, Interview Q&A |
| **Cloud Concepts** | Cloud Computing (4 guides), Multi-Cloud, Cloud Security, FinOps |

### Learning Paths

| Path | Description |
|:-----|:------------|
| DevOps | Culture, practices, tools landscape, DORA metrics |
| Cloud | Multi-provider cloud fundamentals |
| Containers | Docker to Kubernetes journey |
| AI/ML Ops | Machine learning infrastructure |
| SRE | Reliability engineering practices |
| Platform Engineering | Internal developer platforms |
| Linux Master | Deep systems expertise |

### Interview Preparation

Dedicated interview prep with 40-50+ questions per topic: DevOps, Docker, Kubernetes, Terraform, AWS, Azure, GCP, Linux, Git, Python, Bash, Networking, CI/CD, Cloud Computing, Multi-Cloud, Cloud Security, FinOps, and more.

### Career Paths (AI Era)

Eight high-demand roles with salary ranges and skills roadmaps: DevOps Engineer, Cloud Architect, Platform Engineer, AI/ML Infrastructure Engineer, Site Reliability Engineer, Linux Systems Master, DevSecOps Engineer, FinOps Practitioner.

## Architecture Diagrams

All documentation includes Mermaid diagrams for visual learning — Docker architecture, Kubernetes cluster components, CI/CD pipeline flows, AWS VPC design, cloud service models, and more (49 diagrams total).

## Script Examples

The repo includes practical script examples you can run immediately:

- `Bash/basic-scripts/` — 42 ready-to-use Bash scripts (system checks, installers, utilities)
- `AWS/architecting with aws/` — Sample application with CodeDeploy, CloudFormation templates
- `Ansible/Examples/` — Playbook examples for Nginx, Docker, Vagrant, and more

## Getting Started

### Browse Online

Visit **[nomadicmehul.github.io/CloudCaptain](https://nomadicmehul.github.io/CloudCaptain/)** to start learning immediately.

### Run Locally

```bash
git clone https://github.com/nomadicmehul/CloudCaptain.git
cd CloudCaptain/website
npm install
npm start         # Dev server at localhost:3000
```

### Build for Production

```bash
npm run build     # Production build
npm run serve     # Preview production build
```

## Tech Stack

| Component | Technology |
|:----------|:-----------|
| Framework | Docusaurus 3 (TypeScript) |
| Hosting | GitHub Pages |
| CI/CD | GitHub Actions (auto-deploy on push to main) |
| Diagrams | Mermaid (rendered natively in browser) |
| Search | Local search plugin |
| Content | Markdown / MDX |

## Contributing

We welcome contributions! See [CONTRIBUTING.md](./CONTRIBUTING.md) for guidelines.

1. Fork the repository
2. Create a branch: `git checkout -b add/my-resource`
3. Make your changes in the `website/docs/` directory
4. Run `npm run build` locally to verify
5. Submit a Pull Request

### Content Guidelines

- All content goes in `website/docs/` under the appropriate subdirectory
- Use Markdown with front matter (`title`, `description`, `sidebar_position`)
- Keep descriptions concise and include practical examples
- Add exercises, cheat sheets, or interview questions where relevant
- Use Mermaid diagrams for architecture visuals

## Project Structure

```
CloudCaptain/
  website/                    # Docusaurus site
    docs/
      learning-paths/         # Structured learning roadmaps
      interview-prep/         # Interview preparation guides
      tools/                  # Tool-specific documentation
        docker/               # 8 comprehensive Docker guides
        kubernetes/           # 9 Kubernetes guides
        terraform/            # 5 Terraform guides
        ...                   # 25+ more tool categories
      cloud/                  # Cloud provider guides
        aws/                  # 8 AWS guides
        azure/                # 6 Azure guides
        gcp/                  # 5 GCP guides
        ...                   # Cloud Computing, Multi-Cloud, Security, FinOps
    src/pages/                # Custom pages (homepage, contribute)
    static/img/               # Logo, favicon, images
  Bash/basic-scripts/         # 42 ready-to-use Bash scripts
  Ansible/Examples/           # Ansible playbook examples
  AWS/                        # AWS sample apps & CloudFormation templates
  .github/workflows/          # GitHub Actions deployment
```

## License

This project is licensed under the MIT License — see [LICENSE](./LICENSE) for details.

---

<div align="center">
  <p>Built with love by the open-source community</p>
  <p>Founded by <a href="https://github.com/nomadicmehul">@nomadicmehul</a></p>
</div>
