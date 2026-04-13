import React, { useEffect, useRef, useState } from 'react';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Layout from '@theme/Layout';
import BrowserOnly from '@docusaurus/BrowserOnly';
import ResumeReadingWidget from '@site/src/components/Progress/ResumeReadingWidget';

/* ─── Data (loaded from JSON — edit the JSON files to update content) ─── */
import categories from '../data/categories.json';
import learningPaths from '../data/learningPaths.json';
import testimonials from '../data/testimonials.json';
import howItWorksSteps from '../data/howItWorks.json';
import sponsorTiers from '../data/sponsors.json';
import contributeActions from '../data/contributeActions.json';
import techBadges from '../data/techBadges.json';

/* ─── GitHub Stats Hook (cached in localStorage for 1 hour) ─── */
const GH_STATS_CACHE_KEY = 'cc_gh_stats';
const GH_STATS_TTL = 60 * 60 * 1000; // 1 hour

function useGitHubStats() {
  const [stats, setStats] = useState(() => {
    if (typeof window !== 'undefined') {
      try {
        const cached = localStorage.getItem(GH_STATS_CACHE_KEY);
        if (cached) {
          const { data, timestamp } = JSON.parse(cached);
          if (Date.now() - timestamp < GH_STATS_TTL) return data;
        }
      } catch { /* ignore parse errors */ }
    }
    return { stars: 0, forks: 0, contributors: 0 };
  });

  useEffect(() => {
    // Skip fetch if cache is fresh
    try {
      const cached = localStorage.getItem(GH_STATS_CACHE_KEY);
      if (cached) {
        const { timestamp } = JSON.parse(cached);
        if (Date.now() - timestamp < GH_STATS_TTL) return;
      }
    } catch { /* proceed to fetch */ }

    const controller = new AbortController();
    const fetchStats = async () => {
      try {
        const [repoRes, contribRes] = await Promise.all([
          fetch('https://api.github.com/repos/nomadicmehul/CloudCaptain', { signal: controller.signal }),
          fetch('https://api.github.com/repos/nomadicmehul/CloudCaptain/contributors?per_page=1&anon=true', { signal: controller.signal }),
        ]);

        if (repoRes.ok) {
          const repo = await repoRes.json();
          let contribCount = 0;
          if (contribRes.ok) {
            const linkHeader = contribRes.headers.get('Link') || '';
            const lastPageMatch = linkHeader.match(/page=(\d+)>;\s*rel="last"/);
            contribCount = lastPageMatch ? parseInt(lastPageMatch[1], 10) : 1;
          }

          const newStats = {
            stars: repo.stargazers_count || 0,
            forks: repo.forks_count || 0,
            contributors: contribCount,
          };
          setStats(newStats);
          try {
            localStorage.setItem(GH_STATS_CACHE_KEY, JSON.stringify({ data: newStats, timestamp: Date.now() }));
          } catch { /* storage full — ignore */ }
        }
      } catch {
        // Silently fail — will show cached or fallback values
      }
    };
    fetchStats();
    return () => controller.abort();
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

/* ─── Neofetch-style Terminal ─── */
function NeofetchTerminal() {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const timer = setTimeout(() => setVisible(true), 300);
    return () => clearTimeout(timer);
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
        <div className="terminal__line">
          <span className="terminal__prompt">$ </span>
          <span className="terminal__cmd">neofetch</span>
        </div>
        {visible && (
          <div className="neofetch-output">
            <div className="neofetch-grid">
              <pre className="neofetch-ascii" aria-hidden="true">{
`    .----.
   / .  . \\
  |  /\\  |
  |__\\/__|
 /\\______/\\
|  ______  |
 \\________/`
              }</pre>
              <div className="neofetch-info">
                <div className="neofetch-title">
                  <span className="nf-user">captain</span>
                  <span className="nf-at">@</span>
                  <span className="nf-host">cloudcaptain</span>
                </div>
                <div className="neofetch-sep">-----------------</div>
                <div className="nf-row"><span className="nf-key">OS</span>: CloudCaptain Linux x86_64</div>
                <div className="nf-row"><span className="nf-key">Kernel</span>: 6.1.0-oss</div>
                <div className="nf-row"><span className="nf-key">Uptime</span>: 3+ years</div>
                <div className="nf-row"><span className="nf-key">Packages</span>: 150+ (docs)</div>
                <div className="nf-row"><span className="nf-key">Shell</span>: devops/cloud/ai</div>
                <div className="nf-row"><span className="nf-key">License</span>: MIT (100% Free)</div>
                <div className="nf-row"><span className="nf-key">Contributors</span>: 100+</div>
                <div className="neofetch-colors">
                  <span className="nf-color" style={{background:'#F43F5E'}} />
                  <span className="nf-color" style={{background:'#F59E0B'}} />
                  <span className="nf-color" style={{background:'#10B981'}} />
                  <span className="nf-color" style={{background:'#38BDF8'}} />
                  <span className="nf-color" style={{background:'#8B5CF6'}} />
                  <span className="nf-color" style={{background:'#06B6D4'}} />
                </div>
              </div>
            </div>
          </div>
        )}
        <div className="terminal__line" style={{marginTop: '0.5rem'}}>
          <span className="terminal__prompt">$ </span>
          <span className="terminal__cursor">|</span>
        </div>
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
            <img src="img/cloudcaptain-logo.jpg" alt="CloudCaptain" className="hero-logo" width={56} height={56} />
            <span className="hero-badge">&#x1F427; Open Source &middot; Free as in Freedom</span>
          </div>
          <div className="hero-motd">
            <span className="motd-prefix">[ MOTD ]</span> Welcome to CloudCaptain — <em>your ship, your rules.</em>
          </div>
          <h1 className="hero__title">
            Navigate the Cloud<br />Like a Captain
          </h1>
          <p className="hero__subtitle">
            The open-source, community-driven learning platform for Cloud, DevOps, AI & Operations.
            Curated paths, hands-on labs, interview prep — <strong style={{color:'#10B981'}}>100% free, forever.</strong>
          </p>
          <div className="hero-buttons">
            <Link className="btn-primary" to="/docs/learning-paths/devops">
              ./start-learning.sh →
            </Link>
            <Link className="btn-secondary" href="https://github.com/nomadicmehul/CloudCaptain">
              git clone &amp;&amp; star ★
            </Link>
          </div>
          <div className="hero-tech-badges">
            {techBadges.map(tech => (
              <span key={tech} className="tech-badge">{tech}</span>
            ))}
          </div>
        </div>
        <div className="hero-right">
          <NeofetchTerminal />
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
          <StatCard number={150} suffix="+" label="/usr/share/docs" icon="📖" />
          <StatCard number={49} suffix="" label="/etc/diagrams" icon="📐" />
          <StatCard number={8} suffix="" label="/opt/career-paths" icon="🗺️" />
          <StatCard number={100} suffix="%" label="FOSS Licensed" icon="🐧" />
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
        <h2 className="section__title reveal"><span className="section-prompt">$ ls</span> Explore by Technology</h2>
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
        <h2 className="section__title reveal"><span className="section-prompt">$ cat</span> Structured Learning Paths</h2>
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
              loading="lazy"
              width={280}
              height={280}
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
        <h2 className="section__title reveal"><span className="section-prompt">$ man</span> How It Works</h2>
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
        <h2 className="section__title reveal"><span className="section-prompt">$ git</span> Join the Crew</h2>
        <p className="section__subtitle reveal">
          CloudCaptain is built by the community, for the community.
          Every contribution matters — from fixing a typo to adding a learning path.
          <br /><code className="contribute-license-tag">LICENSE: MIT | COST: $0 | FREEDOM: Maximum</code>
        </p>
        <div className="hero-buttons reveal" style={{ justifyContent: 'center' }}>
          <Link className="btn-primary" href="https://github.com/nomadicmehul/CloudCaptain/pulls">
            git push origin my-feature →
          </Link>
          <Link className="btn-secondary" to="/contribute">
            cat CONTRIBUTING.md
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
        <h2 className="section__title reveal"><span className="section-prompt">$ sudo</span> Support CloudCaptain</h2>
        <p className="section__subtitle reveal">
          CloudCaptain is 100% free and open source — no paywalls, no premium tiers.
          If it helped you learn, land a job, or ace an interview,
          consider fueling the project so we can keep shipping.
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
      <BrowserOnly>{() => <ResumeReadingWidget />}</BrowserOnly>
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
