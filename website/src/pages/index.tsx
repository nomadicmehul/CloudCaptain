import React, { useEffect, useRef, useState, useCallback } from 'react';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';

/* ─── Data (loaded from JSON — edit the JSON files to update content) ─── */
import categories from '../data/categories.json';
import learningPaths from '../data/learningPaths.json';
import testimonials from '../data/testimonials.json';
import howItWorksSteps from '../data/howItWorks.json';
import sponsorTiers from '../data/sponsors.json';
import contributeActions from '../data/contributeActions.json';
import techBadges from '../data/techBadges.json';

/* ─── GitHub Stats Hook ─── */
function useGitHubStats() {
  const [stats, setStats] = useState({
    stars: 0,
    forks: 0,
    contributors: 0,
  });

  useEffect(() => {
    const fetchStats = async () => {
      try {
        const [repoRes, contribRes] = await Promise.all([
          fetch('https://api.github.com/repos/nomadicmehul/CloudCaptain'),
          fetch('https://api.github.com/repos/nomadicmehul/CloudCaptain/contributors?per_page=1&anon=true'),
        ]);

        if (repoRes.ok) {
          const repo = await repoRes.json();
          let contribCount = 0;
          if (contribRes.ok) {
            const linkHeader = contribRes.headers.get('Link') || '';
            const lastPageMatch = linkHeader.match(/page=(\d+)>;\s*rel="last"/);
            contribCount = lastPageMatch ? parseInt(lastPageMatch[1], 10) : 1;
          }

          setStats({
            stars: repo.stargazers_count || 0,
            forks: repo.forks_count || 0,
            contributors: contribCount,
          });
        }
      } catch {
        // Silently fail — will show fallback values
      }
    };
    fetchStats();
  }, []);

  return stats;
}

function formatNumber(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1).replace(/\.0$/, '')}K`;
  return String(n);
}

/* ─── Hooks ─── */
function useScrollReveal() {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add('revealed');
          }
        });
      },
      { threshold: 0.1, rootMargin: '0px 0px -50px 0px' }
    );
    const targets = el.querySelectorAll('.reveal');
    targets.forEach((t) => observer.observe(t));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return ref;
}

function useCountUp(target: number, suffix = '', duration = 1500) {
  const [count, setCount] = useState(0);
  const [started, setStarted] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && !started) {
          setStarted(true);
        }
      },
      { threshold: 0.5 }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [started]);

  useEffect(() => {
    if (!started) return;
    const startTime = performance.now();
    const step = (now: number) => {
      const elapsed = now - startTime;
      const progress = Math.min(elapsed / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      setCount(Math.round(eased * target));
      if (progress < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }, [started, target, duration]);

  return { ref, display: `${count}${suffix}` };
}

/* ─── Typing Terminal ─── */
function TypingTerminal() {
  const lines = [
    { type: 'cmd', text: 'kubectl get pods' },
    {
      type: 'output',
      text: 'NAME              READY  STATUS\nweb-app-7d4f     1/1    Running\napi-server-3b    1/1    Running\nredis-cache-9    1/1    Running',
    },
    { type: 'cmd', text: 'docker build -t app:v2 .' },
    { type: 'output', text: 'Successfully built 4a2f8e3c1d' },
  ];

  // Phase: 'idle' (waiting before next line), 'typing' (typing a cmd), 'done' (all lines shown)
  const [completedLines, setCompletedLines] = useState<number>(0);
  const [typedChars, setTypedChars] = useState(0);
  const [phase, setPhase] = useState<'idle' | 'typing' | 'done'>('idle');
  const [showPrompt, setShowPrompt] = useState(false);

  useEffect(() => {
    if (completedLines >= lines.length) {
      // All lines done — show idle cursor, then reset
      setPhase('done');
      setShowPrompt(true);
      const timer = setTimeout(() => {
        setCompletedLines(0);
        setTypedChars(0);
        setPhase('idle');
        setShowPrompt(false);
      }, 4000);
      return () => clearTimeout(timer);
    }

    const currentLine = lines[completedLines];

    if (phase === 'idle') {
      if (currentLine.type === 'cmd') {
        // Show the $ prompt first, then start typing after a short pause
        setShowPrompt(true);
        const timer = setTimeout(() => {
          setPhase('typing');
          setTypedChars(0);
        }, 400);
        return () => clearTimeout(timer);
      } else {
        // Output line — show it after a brief delay
        const timer = setTimeout(() => {
          setCompletedLines((c) => c + 1);
          setShowPrompt(false);
        }, 300);
        return () => clearTimeout(timer);
      }
    }

    if (phase === 'typing' && currentLine.type === 'cmd') {
      if (typedChars < currentLine.text.length) {
        const timer = setTimeout(
          () => setTypedChars((c) => c + 1),
          45 + Math.random() * 35
        );
        return () => clearTimeout(timer);
      } else {
        // Finished typing this command — brief pause then move on
        const timer = setTimeout(() => {
          setCompletedLines((c) => c + 1);
          setPhase('idle');
          setShowPrompt(false);
          setTypedChars(0);
        }, 500);
        return () => clearTimeout(timer);
      }
    }
  }, [completedLines, typedChars, phase]);

  const renderOutput = useCallback((text: string) => {
    return text.split('\n').map((line, i) => {
      const highlighted = line.replace(/Running/g, '<span style="color:#10B981">Running</span>');
      const successHighlighted = highlighted.replace(/Successfully/g, '<span style="color:#38BDF8">Successfully</span>');
      return <div key={i} dangerouslySetInnerHTML={{ __html: successHighlighted }} />;
    });
  }, []);

  return (
    <div className="hero-terminal animate-float">
      <div className="terminal__header">
        <span className="terminal__dot terminal__dot--red" />
        <span className="terminal__dot terminal__dot--yellow" />
        <span className="terminal__dot terminal__dot--green" />
        <span className="terminal__title">cloudcaptain ~ terminal</span>
      </div>
      <div className="terminal__body">
        {/* Completed lines */}
        {lines.slice(0, completedLines).map((line, i) => {
          if (line.type === 'cmd') {
            return (
              <div key={i} className="terminal__line">
                <span className="terminal__prompt">$ </span>
                <span className="terminal__cmd">{line.text}</span>
              </div>
            );
          }
          return (
            <div key={i} className="terminal__output">
              {renderOutput(line.text)}
            </div>
          );
        })}

        {/* Currently typing line — only show when prompt is ready */}
        {completedLines < lines.length && lines[completedLines].type === 'cmd' && showPrompt && (
          <div className="terminal__line">
            <span className="terminal__prompt">$ </span>
            <span className="terminal__cmd">
              {phase === 'typing' ? (
                <>
                  {lines[completedLines].text.slice(0, typedChars)}
                  <span className="terminal__cursor">|</span>
                </>
              ) : (
                <span className="terminal__cursor">|</span>
              )}
            </span>
          </div>
        )}

        {/* Idle cursor after all lines done */}
        {phase === 'done' && showPrompt && (
          <div className="terminal__line">
            <span className="terminal__prompt">$ </span>
            <span className="terminal__cursor">|</span>
          </div>
        )}
      </div>
    </div>
  );
}

/* ─── Floating orbs ─── */
function FloatingOrbs() {
  return (
    <div className="hero-orbs" aria-hidden="true">
      <div className="orb orb--1" />
      <div className="orb orb--2" />
      <div className="orb orb--3" />
      <div className="orb orb--4" />
      <div className="orb orb--5" />
    </div>
  );
}

/* ─── HERO ─── */
function HeroSection() {
  return (
    <div className="hero-cloudcaptain">
      <div className="hero-scanlines" aria-hidden="true" />
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
            Curated paths, hands-on exercises, interview prep — <strong style={{color:'#38BDF8'}}>all free, forever</strong>.
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
            {techBadges.map(tech => (
              <span key={tech} className="tech-badge">{tech}</span>
            ))}
          </div>
        </div>
        <div className="hero-right">
          <TypingTerminal />
        </div>
      </div>
    </div>
  );
}

/* ─── STATS with count-up ─── */
function StatCard({ number, suffix, label, icon }: { number: number; suffix: string; label: string; icon: string }) {
  const { ref, display } = useCountUp(number, suffix);
  return (
    <div ref={ref} className="stat-card">
      <div className="stat-icon">{icon}</div>
      <div className="stat-number">{display}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

function StatsSection() {
  const sectionRef = useScrollReveal();
  return (
    <div className="stats-wrapper" ref={sectionRef}>
      <div className="container">
        <div className="stats-section reveal">
          <StatCard number={150} suffix="+" label="Guides & Docs" icon="📚" />
          <StatCard number={49} suffix="" label="Architecture Diagrams" icon="📐" />
          <StatCard number={8} suffix="" label="Career Paths" icon="🗺️" />
          <StatCard number={100} suffix="%" label="Free & Open" icon="💙" />
        </div>
      </div>
    </div>
  );
}

/* ─── CATEGORIES with stagger ─── */
function CategoriesSection() {
  const sectionRef = useScrollReveal();
  return (
    <div className="section section--dark" ref={sectionRef}>
      <div className="container">
        <h2 className="section__title reveal"><span className="section-prompt">$</span>Explore by Technology</h2>
        <p className="section__subtitle reveal">
          Deep dives with curated resources, cheatsheets, interview prep, and hands-on exercises.
        </p>
        <div className="category-grid">
          {categories.map((cat, i) => (
            <Link key={i} className="category-card reveal" to={cat.link} style={{ '--card-accent': cat.accent } as React.CSSProperties}>
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
  const sectionRef = useScrollReveal();
  return (
    <div className="section section--paths" ref={sectionRef}>
      <div className="container">
        <h2 className="section__title reveal"><span className="section-prompt">$</span>Structured Learning Paths</h2>
        <p className="section__subtitle reveal">
          Don't know where to start? Follow our curated roadmaps from beginner to expert.
        </p>
        <div className="paths-grid">
          {learningPaths.map((path, i) => (
            <Link key={i} className="path-card reveal" to={path.link} style={{ '--path-color': path.color } as React.CSSProperties}>
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
        <div style={{ textAlign: 'center', marginTop: '2.5rem' }}>
          <Link className="btn-secondary reveal" to="/career-paths">
            Explore All 8 Career Paths →
          </Link>
        </div>
      </div>
    </div>
  );
}

/* ─── CAPTAIN'S CALL ─── */
function CaptainSection({ ghStats }: { ghStats: { stars: number; forks: number; contributors: number } }) {
  const sectionRef = useScrollReveal();
  return (
    <div className="section section--captain" ref={sectionRef}>
      <div className="container">
        <div className="captain-content reveal">
          <div className="captain-image-wrapper">
            <div className="captain-image-glow" />
            <img
              src="img/mehul-founder.jpg"
              alt="Mehul Patel — Founder of CloudCaptain"
              className="captain-image"
            />
            <div className="captain-badge-tag">Founder & Captain</div>
            <Link to="/journey" className="captain-journey-link">
              ~/my-journey &rarr;
            </Link>
          </div>
          <div className="captain-text">
            <h2 className="captain-heading">
              Want to Become a<br />
              <span className="captain-heading--accent">Cloud Captain?</span>
            </h2>
            <p className="captain-tagline">
              Then follow this path. I've spent 3+ years curating the best resources,
              building hands-on exercises, and charting learning roadmaps — so you don't have to
              navigate the cloud alone.
            </p>
            <div className="captain-cta">
              <Link className="btn-primary" to="/career-paths">
                Explore Career Paths →
              </Link>
              <Link className="btn-secondary" to="/docs/learning-paths/devops">
                Start the DevOps Path
              </Link>
            </div>
            <div className="captain-social-proof">
              <div className="captain-social-stat">
                <div className="captain-social-stat__number">{ghStats.stars ? `${formatNumber(ghStats.stars)}+` : '2K+'}</div>
                <div className="captain-social-stat__label">GitHub Stars</div>
              </div>
              <div className="captain-social-stat">
                <div className="captain-social-stat__number">{ghStats.contributors ? `${ghStats.contributors}+` : '100+'}</div>
                <div className="captain-social-stat__label">Contributors</div>
              </div>
              <div className="captain-social-stat">
                <div className="captain-social-stat__number">{ghStats.forks ? `${ghStats.forks}+` : '500+'}</div>
                <div className="captain-social-stat__label">Forks</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ─── HOW IT WORKS — Enhanced ─── */
function HowItWorksSection() {
  const sectionRef = useScrollReveal();
  return (
    <div className="section section--how" ref={sectionRef}>
      <div className="container">
        <h2 className="section__title reveal"><span className="section-prompt">$</span>How It Works</h2>
        <p className="section__subtitle reveal">Four steps from zero to cloud-native expert.</p>
        <div className="how-grid">
          {howItWorksSteps.map((step, i) => (
            <div key={i} className="how-step reveal">
              <div className="how-step__icon">{step.icon}</div>
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

/* ─── TESTIMONIALS ─── */
function TestimonialsSection({ ghStats }: { ghStats: { stars: number; forks: number; contributors: number } }) {
  const sectionRef = useScrollReveal();
  return (
    <div className="section section--testimonials" ref={sectionRef}>
      <div className="container">
        <h2 className="section__title reveal">Community Voices</h2>
        <p className="section__subtitle reveal">
          Hear from engineers who used CloudCaptain to level up their careers.
        </p>
        <div className="testimonials-grid">
          {testimonials.map((t, i) => (
            <div key={i} className="testimonial-card reveal">
              <p className="testimonial-card__text">{t.text}</p>
              <div className="testimonial-card__author">
                <div className="testimonial-card__avatar">{t.initial}</div>
                <div>
                  <div className="testimonial-card__name">{t.name}</div>
                  <div className="testimonial-card__role">{t.role}</div>
                </div>
              </div>
            </div>
          ))}
        </div>
        <div className="community-banner reveal">
          <div className="community-stat">
            <div className="community-stat__number">{ghStats.stars ? `${formatNumber(ghStats.stars)}+` : '2K+'}</div>
            <div className="community-stat__label">GitHub Stars</div>
          </div>
          <div className="community-stat">
            <div className="community-stat__number">{ghStats.contributors ? `${ghStats.contributors}+` : '100+'}</div>
            <div className="community-stat__label">Contributors</div>
          </div>
          <div className="community-stat">
            <div className="community-stat__number">{ghStats.forks ? `${formatNumber(ghStats.forks)}+` : '500+'}</div>
            <div className="community-stat__label">Forks</div>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ─── CONTRIBUTE ─── */
function ContributeSection() {
  const sectionRef = useScrollReveal();
  return (
    <div className="section section--contribute" ref={sectionRef}>
      <div className="container" style={{ textAlign: 'center' }}>
        <h2 className="section__title reveal"><span className="section-prompt">$</span>Join the Crew</h2>
        <p className="section__subtitle reveal">
          CloudCaptain is built by the community, for the community.
          Every contribution matters — from fixing a typo to adding a learning path.
        </p>
        <div className="hero-buttons reveal" style={{ justifyContent: 'center' }}>
          <Link className="btn-primary" href="https://github.com/nomadicmehul/CloudCaptain/pulls">
            Submit a PR →
          </Link>
          <Link className="btn-secondary" to="/contribute">
            Contribution Guide
          </Link>
        </div>
        <div className="contribute-actions reveal">
          {contributeActions.map((item, i) => (
            <div key={i} className="contribute-tag">
              <span>{item.icon}</span> {item.text}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ─── SPONSOR ─── */
function SponsorSection() {
  const sectionRef = useScrollReveal();
  return (
    <div className="section section--sponsor" ref={sectionRef}>
      <div className="container">
        <h2 className="section__title reveal"><span className="section-prompt">$</span>Support CloudCaptain</h2>
        <p className="section__subtitle reveal">
          CloudCaptain is 100% free and open source. If it helped you learn, land a job, or ace an interview,
          consider supporting the project so we can keep building.
        </p>
        <div className="sponsor-grid">
          {sponsorTiers.map((tier, i) => (
            <a key={i} className="sponsor-card reveal" href={tier.link} target="_blank" rel="noopener noreferrer">
              <div className="sponsor-card__emoji">{tier.emoji}</div>
              <h3 className="sponsor-card__title">{tier.title}</h3>
              <p className="sponsor-card__desc">{tier.desc}</p>
              <span className={`sponsor-card__btn ${tier.btnClass}`}>{tier.btnText} →</span>
            </a>
          ))}
        </div>
      </div>
    </div>
  );
}

/* ─── MAIN ─── */
export default function Home(): React.ReactElement {
  const { siteConfig } = useDocusaurusContext();
  const ghStats = useGitHubStats();
  return (
    <Layout
      title={`${siteConfig.title} — Learn Cloud, DevOps & AI`}
      description="Open-source community learning platform for Cloud, DevOps, AI, and Operations. Free forever.">
      <HeroSection />
      <StatsSection />
      <CategoriesSection />
      <LearningPathsSection />
      <CaptainSection ghStats={ghStats} />
      <HowItWorksSection />
      {/* Testimonials hidden for now — uncomment when real testimonials are added to testimonials.json */}
      {/* <TestimonialsSection ghStats={ghStats} /> */}
      <ContributeSection />
      <SponsorSection />
    </Layout>
  );
}
