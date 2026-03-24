import React from 'react';
import Link from '@docusaurus/Link';
import Layout from '@theme/Layout';

/* ─── Data (loaded from JSON — edit the JSON file to update content) ─── */
import allPaths from '../data/careerPaths.json';

function PathCard({ path }: { path: typeof allPaths[0] }) {
  return (
    <Link className="career-card" to={path.link} style={{ '--path-color': path.color } as React.CSSProperties}>
      <div className="career-card__top">
        <span className={`path-card__level path-card__level--${path.level}`}>{path.levelLabel}</span>
        <span className="career-card__salary">{path.salary}</span>
      </div>
      <h3 className="career-card__title">{path.title}</h3>
      <p className="career-card__desc">{path.desc}</p>
      <div className="career-card__timeline">
        {path.steps.map((step, j) => (
          <div key={j} className="timeline-step">
            <div className="timeline-step__dot">{j + 1}</div>
            {j < path.steps.length - 1 && <div className="timeline-step__line" />}
            <span className="timeline-step__label">{step}</span>
          </div>
        ))}
      </div>
      <div className="career-card__certs">
        <span className="career-card__certs-label">Top Certifications</span>
        <div className="career-card__cert-tags">
          {path.certs.map((cert, j) => (
            <span key={j} className="career-card__cert-tag">{cert}</span>
          ))}
        </div>
      </div>
      <div className="path-card__cta">View Learning Path →</div>
    </Link>
  );
}

function HeroSection() {
  return (
    <div className="career-hero">
      <div className="container">
        <div className="career-hero__badge">&#x1F427; 8 Career Paths &middot; All FOSS</div>
        <h1 className="career-hero__title">
          <span className="section-prompt">$ whereis</span> Your<br />
          <span className="career-hero__accent">Cloud Career Path</span>
        </h1>
        <p className="career-hero__subtitle">
          From beginner to expert — structured roadmaps with skills, certifications, and salary benchmarks
          for the most in-demand roles in Cloud, DevOps, AI, and Platform Engineering.
        </p>
      </div>
    </div>
  );
}

function PickerSection() {
  const pickData = [
    { like: 'Automating everything', path: 'DevOps Engineer' },
    { like: 'Designing systems at scale', path: 'Cloud Architect' },
    { like: 'Building tools for devs', path: 'Platform Engineer' },
    { like: 'Working with GPUs & ML', path: 'AI Infra Engineer' },
    { like: 'Keeping uptime at 99.99%', path: 'SRE' },
    { like: 'Going deep on Linux/kernel', path: 'Linux Systems Master' },
    { like: 'Finding & fixing flaws', path: 'Security Engineer' },
    { like: 'Optimizing cloud spend', path: 'FinOps Practitioner' },
  ];
  return (
    <div className="career-picker">
      <div className="container">
        <h2 className="section__title"><span className="section-prompt">$ which</span> Path Is Right for You?</h2>
        <p className="section__subtitle">Match your interests to the right career — like piping commands to find the perfect match.</p>
        <div className="picker-grid">
          {pickData.map((item, i) => (
            <div key={i} className="picker-item">
              <span className="picker-item__like">{item.like}</span>
              <span className="picker-item__arrow">|</span>
              <span className="picker-item__path">{item.path}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export default function CareerPathsPage(): React.ReactElement {
  return (
    <Layout
      title="Career Paths — CloudCaptain"
      description="8 in-demand career paths in Cloud, DevOps, AI, and Platform Engineering with structured learning roadmaps.">
      <HeroSection />
      <div className="section section--dark career-paths-section">
        <div className="container">
          <h2 className="section__title"><span className="section-prompt">$ find</span> All Learning Paths</h2>
          <p className="section__subtitle">
            Each path includes a step-by-step roadmap, recommended certifications, and links to free resources on CloudCaptain.
          </p>
          <div className="career-grid">
            {allPaths.map((path, i) => (
              <PathCard key={i} path={path} />
            ))}
          </div>
        </div>
      </div>
      <PickerSection />
      <div className="section section--contribute">
        <div className="container" style={{ textAlign: 'center' }}>
          <h2 className="section__title"><span className="section-prompt">$ exec</span> Ready to Start?</h2>
          <p className="section__subtitle">
            Pick a path, dive into the resources, and build real projects. Every guide on CloudCaptain is free and open source.
          </p>
          <div className="hero-buttons" style={{ justifyContent: 'center' }}>
            <Link className="btn-primary" to="/docs/learning-paths/devops">
              ./start-devops.sh →
            </Link>
            <Link className="btn-secondary" to="/">
              cd ~/home
            </Link>
          </div>
        </div>
      </div>
    </Layout>
  );
}
