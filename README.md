<div align="center">
  <img src="./website/static/img/cloudcaptain-logo.jpg" width="200" />
  <h1>CloudCaptain</h1>
  <p><strong>The Open-Source Learning Hub for Cloud, DevOps, AI & Operations</strong></p>
  <p>
    <a href="https://cloudcaptain.io"><img src="https://img.shields.io/badge/Website-Live-1E9BD7?style=flat&logo=docusaurus" /></a>
    <img src="https://img.shields.io/badge/PRs-welcome-blue" />
    <img src="https://img.shields.io/github/last-commit/nomadicmehul/CloudCaptain" />
    <a href="https://twitter.com/intent/follow?screen_name=nomadicmehul"><img src="https://img.shields.io/twitter/follow/nomadicmehul?style=flat&logo=twitter" /></a>
    <a href="https://github.com/nomadicmehul"><img src="https://img.shields.io/github/stars/nomadicmehul/cloudcaptain?style=flat&logo=github" /></a>
  </p>
</div>

---

## What is CloudCaptain?

CloudCaptain is a community-driven, open-source learning platform with **150+ comprehensive documentation pages**, **49 architecture diagrams**, **8 career paths**, and **7 structured learning roadmaps** covering Cloud, DevOps, AI, and Operations — all free, forever.

Every page includes hands-on exercises, command cheat sheets, interview Q&A, and exam prep guides. Whether you're a complete beginner or a seasoned professional, CloudCaptain has something for you.

**[Visit the Website →](https://cloudcaptain.io)**

## What's Inside

### Tools & Technology (28 categories)

| Category | Topics Covered |
|:---------|:---------------|
| **Containers** | Docker (8 guides), Podman |
| **Orchestration** | Kubernetes (9 guides), Helm |
| **Infrastructure as Code** | Terraform (5 guides), Ansible (4 guides + playbook examples), CloudFormation, Packer, Vagrant |
| **CI/CD** | Jenkins (3 guides), CI/CD Pipelines (4 guides), GitHub Actions, CircleCI, GitOps |
| **Languages & Scripting** | Linux (6 guides), Git (4 guides), Bash (5 guides + 90 script examples), Python (4 guides), YAML |
| **Networking** | Fundamentals, Routing & Switching, Security, 100+ commands cheat sheet |
| **Security** | DevSecOps (3 guides) |
| **Cloud** | AWS (9 guides + CloudFormation examples), Azure (6 guides), GCP (5 guides) |
| **Web & Build** | Nginx, Chef, Gradle |

### Cloud Concepts

| Topic | Coverage |
|:------|:---------|
| **Cloud Computing** | Fundamentals, Architecture, Migration, Interview Q&A |
| **Multi-Cloud** | Strategy, Fundamentals, Interview Q&A |
| **Cloud Security** | Fundamentals, Best Practices, Interview Q&A |
| **FinOps** | Cost Optimization, Frameworks, Interview Q&A |

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

### Career Paths (AI Era)

Eight high-demand roles with salary ranges, skills roadmaps, and certification recommendations: DevOps Engineer, Cloud Architect, Platform Engineer, AI/ML Infrastructure Engineer, Site Reliability Engineer, Linux Systems Master, DevSecOps Engineer, FinOps Practitioner.

**[Explore Career Paths →](https://cloudcaptain.io/career-paths)**

### Interview Preparation

Dedicated interview prep with 40-50+ questions per topic: DevOps, Docker, Kubernetes, Terraform, AWS, Azure, GCP, Linux, Git, Python, Bash, Networking, CI/CD, Cloud Computing, Multi-Cloud, Cloud Security, FinOps, and more.

### Architecture Diagrams

49 Mermaid diagrams for visual learning — Docker architecture, Kubernetes cluster components, CI/CD pipeline flows, AWS VPC design, cloud service models, and more. All rendered natively in the browser.

## Getting Started

### Browse Online

Visit **[cloudcaptain.io](https://cloudcaptain.io)** to start learning immediately.

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
| Hosting | GitHub Pages + Custom Domain |
| CI/CD | GitHub Actions (auto-deploy on push to main) |
| Diagrams | Mermaid (rendered natively in browser) |
| Search | Local search plugin |
| Content | Markdown / MDX |
| Data | JSON files (edit content without touching code) |

## Contributing

We welcome contributions! See [CONTRIBUTING.md](./CONTRIBUTING.md) for full guidelines.

1. Fork the repository
2. Create a branch: `git checkout -b add/my-resource`
3. Make your changes
4. Run `npm run build` locally to verify
5. Submit a Pull Request

### Content Guidelines

- All documentation goes in `website/docs/` under the appropriate subdirectory
- Use Markdown with front matter (`title`, `description`, `sidebar_position`)
- Keep descriptions concise and include practical examples
- Add exercises, cheat sheets, or interview questions where relevant
- Use Mermaid diagrams for architecture visuals

### Updating Homepage Content (JSON Data)

Homepage sections are powered by JSON files in `website/src/data/`. You can update content by editing these files directly on GitHub — no React code changes needed:

| File | What it controls |
|:-----|:-----------------|
| `categories.json` | Technology cards (Docker, K8s, Terraform, etc.) |
| `learningPaths.json` | Learning path cards on homepage |
| `careerPaths.json` | Career paths page (8 roles) |
| `howItWorks.json` | "How It Works" 4-step process |
| `sponsors.json` | Sponsor cards (Buy Me a Coffee, GitHub Sponsors) |
| `contributeActions.json` | Contribution type tags |
| `techBadges.json` | Tech badge pills in the hero section |
| `testimonials.json` | Community testimonials (currently hidden) |

## Project Structure

```
CloudCaptain/
  website/                    # Docusaurus site
    docs/
      learning-paths/         # Structured learning roadmaps
      interview-prep/         # Interview preparation guides
      tools/                  # Tool-specific documentation (25+ categories)
        docker/               # 8 Docker guides
        kubernetes/           # 9 Kubernetes guides
        terraform/            # 5 Terraform guides
        bash/                 # 5 Bash guides + 90 script examples
        ansible/              # 4 Ansible guides + playbook examples
        ...
      cloud/                  # Cloud provider guides
        aws/                  # 9 AWS guides + CloudFormation examples
        azure/                # 6 Azure guides
        gcp/                  # 5 GCP guides
        ...                   # Cloud Computing, Multi-Cloud, Security, FinOps
    src/
      data/                   # JSON data files (edit to update homepage content)
      pages/                  # Custom pages (homepage, career paths, contribute)
      css/                    # Brand-specific styling
    static/img/               # Logo, favicon, social card
  .github/workflows/          # GitHub Actions deployment
```

## License

This project is licensed under the MIT License — see [LICENSE](./LICENSE) for details.

---

<div align="center">
  <p>Built with love by <a href="https://nomadicmehul.dev/">Mehul Patel</a> and the open-source community</p>
  <p><a href="https://cloudcaptain.io">cloudcaptain.io</a></p>
</div>
