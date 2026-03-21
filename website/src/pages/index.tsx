import React from 'react';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';

const categories = [
  { icon: '🐳', title: 'Docker', desc: 'Containers, Dockerfile, Compose, networking & security.', link: '/docs/tools/docker/', count: '15+ resources', accent: '#2496ED' },
  { icon: '☸️', title: 'Kubernetes', desc: 'Orchestration, Helm, operators, service mesh & autoscaling.', link: '/docs/tools/kubernetes/', count: '40+ resources', accent: '#326CE5' },
  { icon: '🏗️', title: 'Terraform', desc: 'IaC, modules, state management & CI/CD integration.', link: '/docs/tools/terraform/', count: '20+ resources', accent: '#7B42BC' },
  { icon: '☁️', title: 'AWS', desc: 'EC2, S3, Lambda, EKS, CloudFormation & architecture.', link: '/docs/cloud/aws/', count: '25+ resources', accent: '#FF9900' },
  { icon: '🔷', title: 'Azure', desc: 'Azure DevOps, AKS, Functions & cloud architecture.', link: '/docs/cloud/azure/', count: '10+ resources', accent: '#0078D4' },
  { icon: '🌐', title: 'Google Cloud', desc: 'GKE, Cloud Functions, BigQuery & GCP practices.', link: '/docs/cloud/gcp/', count: '10+ resources', accent: '#4285F4' },
  { icon: '🔧', title: 'Ansible', desc: 'Playbooks, roles, Vault & automation patterns.', link: '/docs/tools/ansible/', count: '10+ resources', accent: '#EE0000' },
  { icon: '🚀', title: 'CI/CD', desc: 'Jenkins, GitHub Actions, CircleCI & GitOps pipelines.', link: '/docs/tools/cicd/', count: '15+ resources', accent: '#10B981' },
  { icon: '🐧', title: 'Linux', desc: 'System admin, shell scripting & performance tuning.', link: '/docs/tools/linux/', count: '10+ resources', accent: '#FCC624' },
  { icon: '🔒', title: 'DevSecOps', desc: 'SAST/DAST, compliance, auditing & shift-left security.', link: '/docs/tools/devsecops/', count: '8+ resources', accent: '#F43F5E' },
  { icon: '📊', title: 'Observability', desc: 'Prometheus, Grafana, SRE & monitoring strategies.', link: '/docs/tools/observability/', count: '8+ resources', accent: '#F59E0B' },
  { icon: '🤖', title: 'AI / ML Ops', desc: 'ML pipelines, model serving & AI infrastructure.', link: '/docs/learning-paths/ai-ml', count: 'New!', accent: '#8B5CF6' },
];

const learningPaths = [
  {
    level: 'beginner', color: '#10B981',
    title: 'Foundation Track',
    desc: 'Start your cloud-native journey with Linux, Git, networking, and basic scripting.',
    steps: ['Linux Fundamentals', 'Git & Version Control', 'Networking Basics', 'Bash Scripting', 'Cloud Concepts'],
    duration: '4-6 weeks',
    link: '/docs/learning-paths/devops',
  },
  {
    level: 'intermediate', color: '#F59E0B',
    title: 'DevOps Engineer Track',
    desc: 'Master CI/CD pipelines, containers, IaC, and configuration management.',
    steps: ['Docker & Containers', 'CI/CD Pipelines', 'Terraform / IaC', 'Ansible', 'Cloud Provider'],
    duration: '8-12 weeks',
    link: '/docs/learning-paths/devops',
  },
  {
    level: 'advanced', color: '#F43F5E',
    title: 'Cloud Architect Track',
    desc: 'Design scalable systems with Kubernetes, service mesh, GitOps, and SRE.',
    steps: ['Kubernetes & Helm', 'Service Mesh', 'GitOps & ArgoCD', 'SRE & Observability', 'Platform Eng'],
    duration: '12-16 weeks',
    link: '/docs/learning-paths/containers',
  },
];

/* ─── Floating orbs for hero background ─── */
function FloatingOrbs() {
  return (
    <div className="hero-orbs" aria-hidden="true">
      <div className="orb orb--1" />
      <div className="orb orb--2" />
      <div className="orb orb--3" />
      <div className="orb orb--4" />
    </div>
  );
}

/* ─── Animated terminal graphic ─── */
function TerminalGraphic() {
  return (
    <div className="hero-terminal animate-float">
      <div className="terminal__header">
        <span className="terminal__dot terminal__dot--red" />
        <span className="terminal__dot terminal__dot--yellow" />
        <span className="terminal__dot terminal__dot--green" />
        <span className="terminal__title">cloudcaptain ~ terminal</span>
      </div>
      <div className="terminal__body">
        <div className="terminal__line">
          <span className="terminal__prompt">$</span>
          <span className="terminal__cmd"> kubectl get pods</span>
        </div>
        <div className="terminal__output">
          NAME{'              '}READY{'  '}STATUS<br/>
          web-app-7d4f{'     '}1/1{'    '}<span style={{color:'#10B981'}}>Running</span><br/>
          api-server-3b{'    '}1/1{'    '}<span style={{color:'#10B981'}}>Running</span><br/>
          redis-cache-9{'    '}1/1{'    '}<span style={{color:'#10B981'}}>Running</span>
        </div>
        <div className="terminal__line">
          <span className="terminal__prompt">$</span>
          <span className="terminal__cmd"> docker build -t app:v2 .</span>
        </div>
        <div className="terminal__output">
          <span style={{color:'#38BDF8'}}>Successfully</span> built 4a2f8e3c1d
        </div>
        <div className="terminal__line">
          <span className="terminal__prompt">$</span>
          <span className="terminal__cursor">|</span>
        </div>
      </div>
    </div>
  );
}

/* ─── HERO ─── */
function HeroSection() {
  return (
    <div className="hero-cloudcaptain">
      <FloatingOrbs />
      <div className="container hero-container">
        <div className="hero-left">
          <div className="hero-badge-row">
            <img src="img/cloudcaptain-logo.jpg" alt="CloudCaptain" className="hero-logo" />
            <span className="hero-badge">Open Source Community Project</span>
          </div>
          <h1 className="hero__title">
            Navigate the Cloud<br />Like a Captain
          </h1>
          <p className="hero__subtitle">
            The open-source, community-driven learning platform for Cloud, DevOps, AI & Operations.
            Curated paths, hands-on labs, interview prep — <strong style={{color:'#38BDF8'}}>all free, forever</strong>.
          </p>
          <div className="hero-buttons">
            <Link className="btn-primary" to="/docs/learning-paths/devops">
              Start Learning →
            </Link>
            <Link className="btn-secondary" href="https://github.com/nomadicmehul/CloudCaptain">
              ★ Star on GitHub
            </Link>
          </div>
          <div className="hero-tech-badges">
            {['Docker', 'Kubernetes', 'Terraform', 'AWS', 'Azure', 'GCP', 'Ansible', 'Jenkins', 'Git', 'Linux', 'Python'].map(tech => (
              <span key={tech} className="tech-badge">{tech}</span>
            ))}
          </div>
        </div>
        <div className="hero-right">
          <TerminalGraphic />
        </div>
      </div>
    </div>
  );
}

/* ─── STATS ─── */
function StatsSection() {
  const stats = [
    { number: '40+', label: 'Topics Covered', icon: '📚' },
    { number: '476+', label: 'PDFs & Books', icon: '📖' },
    { number: '8', label: 'Career Paths', icon: '🗺️' },
    { number: '100%', label: 'Free & Open', icon: '💙' },
  ];
  return (
    <div className="stats-wrapper">
      <div className="container">
        <div className="stats-section">
          {stats.map((stat, i) => (
            <div key={i} className="stat-card">
              <div className="stat-icon">{stat.icon}</div>
              <div className="stat-number">{stat.number}</div>
              <div className="stat-label">{stat.label}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ─── CATEGORIES ─── */
function CategoriesSection() {
  return (
    <div className="section section--dark">
      <div className="container">
        <h2 className="section__title">Explore by Technology</h2>
        <p className="section__subtitle">
          Deep dives with curated resources, cheatsheets, interview prep, and hands-on exercises.
        </p>
        <div className="category-grid">
          {categories.map((cat, i) => (
            <Link key={i} className="category-card" to={cat.link} style={{ '--card-accent': cat.accent } as React.CSSProperties}>
              <div className="category-card__header">
                <span className="category-card__icon">{cat.icon}</span>
                <span className="category-card__count">{cat.count}</span>
              </div>
              <div className="category-card__title">{cat.title}</div>
              <div className="category-card__description">{cat.desc}</div>
            </Link>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ─── LEARNING PATHS ─── */
function LearningPathsSection() {
  return (
    <div className="section section--paths">
      <div className="container">
        <h2 className="section__title">Structured Learning Paths</h2>
        <p className="section__subtitle">
          Don't know where to start? Follow our curated roadmaps from beginner to expert.
        </p>
        <div className="paths-grid">
          {learningPaths.map((path, i) => (
            <Link key={i} className="path-card" to={path.link} style={{ '--path-color': path.color } as React.CSSProperties}>
              <div className="path-card__top">
                <span className={`path-card__level path-card__level--${path.level}`}>{path.level}</span>
                <span className="path-card__duration">{path.duration}</span>
              </div>
              <h3 className="path-card__title">{path.title}</h3>
              <p className="path-card__desc">{path.desc}</p>
              <div className="path-card__timeline">
                {path.steps.map((step, j) => (
                  <div key={j} className="timeline-step">
                    <div className="timeline-step__dot">{j + 1}</div>
                    {j < path.steps.length - 1 && <div className="timeline-step__line" />}
                    <span className="timeline-step__label">{step}</span>
                  </div>
                ))}
              </div>
              <div className="path-card__cta">
                View Full Path →
              </div>
            </Link>
          ))}
        </div>
        <div style={{ textAlign: 'center', marginTop: '2rem' }}>
          <Link className="btn-secondary" to="/docs/learning-paths/career-paths">
            Explore All 8 Career Paths →
          </Link>
        </div>
      </div>
    </div>
  );
}

/* ─── CAPTAIN'S CALL ─── */
function CaptainSection() {
  return (
    <div className="section section--captain">
      <div className="container">
        <div className="captain-content">
          <div className="captain-image-wrapper">
            <div className="captain-image-glow" />
            <img
              src="img/mehul-founder.jpg"
              alt="Mehul Patel — Founder of CloudCaptain"
              className="captain-image"
            />
            <div className="captain-badge-tag">Founder & Captain</div>
          </div>
          <div className="captain-text">
            <h2 className="captain-heading">
              Want to Become a<br />
              <span className="captain-heading--accent">Cloud Captain?</span>
            </h2>
            <p className="captain-tagline">
              Then follow this path. I've spent 3+ years curating the best resources,
              building hands-on labs, and charting learning roadmaps — so you don't have to
              navigate the cloud alone.
            </p>
            <div className="captain-cta">
              <Link className="btn-primary" to="/docs/learning-paths/career-paths">
                Explore Career Paths →
              </Link>
              <Link className="btn-secondary" to="/docs/learning-paths/devops">
                Start the DevOps Path
              </Link>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ─── HOW IT WORKS ─── */
function HowItWorksSection() {
  const steps = [
    { num: '01', title: 'Pick a Path', desc: 'Choose a learning path based on your experience level and goals.' },
    { num: '02', title: 'Learn & Practice', desc: 'Follow structured resources, run hands-on labs, and build real projects.' },
    { num: '03', title: 'Level Up', desc: 'Prepare for interviews, earn certifications, and advance your career.' },
    { num: '04', title: 'Give Back', desc: 'Share your knowledge — contribute resources and help fellow learners.' },
  ];
  return (
    <div className="section section--how">
      <div className="container">
        <h2 className="section__title">How It Works</h2>
        <p className="section__subtitle">Four steps from zero to cloud-native expert.</p>
        <div className="how-grid">
          {steps.map((step, i) => (
            <div key={i} className="how-step">
              <div className="how-step__num">{step.num}</div>
              <h3 className="how-step__title">{step.title}</h3>
              <p className="how-step__desc">{step.desc}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ─── CONTRIBUTE ─── */
function ContributeSection() {
  return (
    <div className="section section--contribute">
      <div className="container" style={{ textAlign: 'center' }}>
        <h2 className="section__title">Join the Crew</h2>
        <p className="section__subtitle">
          CloudCaptain is built by the community, for the community.
          Every contribution matters — from fixing a typo to adding a learning path.
        </p>
        <div className="hero-buttons" style={{ justifyContent: 'center' }}>
          <Link className="btn-primary" href="https://github.com/nomadicmehul/CloudCaptain/pulls">
            Submit a PR →
          </Link>
          <Link className="btn-secondary" to="/contribute">
            Contribution Guide
          </Link>
        </div>
        <div className="contribute-actions">
          {[
            { icon: '📝', text: 'Add Resources' },
            { icon: '🐛', text: 'Fix Issues' },
            { icon: '📚', text: 'Write Guides' },
            { icon: '🎨', text: 'Improve Design' },
            { icon: '🌍', text: 'Translate' },
          ].map((item, i) => (
            <div key={i} className="contribute-tag">
              <span>{item.icon}</span> {item.text}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ─── MAIN ─── */
export default function Home(): React.ReactElement {
  const { siteConfig } = useDocusaurusContext();
  return (
    <Layout
      title={`${siteConfig.title} — Learn Cloud, DevOps & AI`}
      description="Open-source community learning platform for Cloud, DevOps, AI, and Operations. Free forever.">
      <HeroSection />
      <StatsSection />
      <CategoriesSection />
      <LearningPathsSection />
      <CaptainSection />
      <HowItWorksSection />
      <ContributeSection />
    </Layout>
  );
}
