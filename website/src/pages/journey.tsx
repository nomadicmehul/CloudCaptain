import React, { useEffect, useRef, useState } from 'react';
import Link from '@docusaurus/Link';
import Layout from '@theme/Layout';

/* ─── Scroll Reveal Hook ─── */
function useScrollReveal() {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) entry.target.classList.add('revealed');
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

/* ─── Count-up Hook ─── */
function useCountUp(target: number, suffix = '', duration = 1500) {
  const [count, setCount] = useState(0);
  const [started, setStarted] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && !started) setStarted(true);
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

/* ─── Terminal Intro ─── */
function TerminalIntro() {
  const lines = [
    '$ whoami',
    'mehul-patel :: docker-captain :: gde-cloud :: open-source-advocate',
    '',
    '$ cat /etc/journey.conf',
    'EXPERIENCE="12+ years"',
    'SESSIONS_DELIVERED="500+"',
    'COUNTRIES_REACHED="25+"',
    'DEVELOPERS_IMPACTED="50,000+"',
    'MENTEES="305+"',
    'RATING="4.9/5"',
    '',
    '$ uname -a',
    'Linux cloudcaptain 6.1.0-mehul #1 SMP OPEN_SOURCE x86_64 GNU/Linux',
  ];

  const [visibleLines, setVisibleLines] = useState(0);

  useEffect(() => {
    if (visibleLines < lines.length) {
      const delay = lines[visibleLines] === '' ? 100 : lines[visibleLines].startsWith('$') ? 400 : 80;
      const timer = setTimeout(() => setVisibleLines((v) => v + 1), delay);
      return () => clearTimeout(timer);
    }
  }, [visibleLines]);

  return (
    <div className="journey-terminal">
      <div className="terminal__header">
        <span className="terminal__dot terminal__dot--red" />
        <span className="terminal__dot terminal__dot--yellow" />
        <span className="terminal__dot terminal__dot--green" />
        <span className="terminal__title">mehul@cloudcaptain ~ journey</span>
      </div>
      <div className="terminal__body">
        {lines.slice(0, visibleLines).map((line, i) => {
          if (line === '') return <div key={i} className="terminal__spacer" />;
          if (line.startsWith('$')) {
            return (
              <div key={i} className="terminal__line">
                <span className="terminal__prompt">$ </span>
                <span className="terminal__cmd">{line.slice(2)}</span>
              </div>
            );
          }
          if (line.includes('=')) {
            const [key, val] = line.split('=');
            return (
              <div key={i} className="terminal__line terminal__config-line">
                <span className="terminal__config-key">{key}</span>
                <span className="terminal__config-eq">=</span>
                <span className="terminal__config-val">{val}</span>
              </div>
            );
          }
          return (
            <div key={i} className="terminal__output">
              <span>{line}</span>
            </div>
          );
        })}
        {visibleLines < lines.length && (
          <div className="terminal__line">
            <span className="terminal__cursor">|</span>
          </div>
        )}
      </div>
    </div>
  );
}

/* ─── Stat Card ─── */
function JourneyStat({ number, suffix, label, icon }: { number: number; suffix: string; label: string; icon: string }) {
  const { ref, display } = useCountUp(number, suffix);
  return (
    <div ref={ref} className="journey-stat-card">
      <div className="journey-stat-icon">{icon}</div>
      <div className="journey-stat-number">{display}</div>
      <div className="journey-stat-label">{label}</div>
    </div>
  );
}

/* ─── Timeline Entry ─── */
function TimelineEntry({
  year,
  title,
  desc,
  icon,
  tags,
}: {
  year: string;
  title: string;
  desc: string;
  icon: string;
  tags?: string[];
}) {
  return (
    <div className="journey-timeline-entry reveal">
      <div className="journey-timeline-marker">
        <div className="journey-timeline-icon">{icon}</div>
        <div className="journey-timeline-year">{year}</div>
      </div>
      <div className="journey-timeline-content">
        <h3 className="journey-timeline-title">{title}</h3>
        <p className="journey-timeline-desc">{desc}</p>
        {tags && (
          <div className="journey-timeline-tags">
            {tags.map((tag) => (
              <span key={tag} className="journey-tag">{tag}</span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/* ─── Badge Card ─── */
function BadgeCard({ title, org, icon, color }: { title: string; org: string; icon: string; color: string }) {
  return (
    <div className="journey-badge-card reveal" style={{ '--badge-accent': color } as React.CSSProperties}>
      <div className="journey-badge-icon">{icon}</div>
      <div className="journey-badge-title">{title}</div>
      <div className="journey-badge-org">{org}</div>
    </div>
  );
}

/* ─── Open Source Project Card ─── */
function ProjectCard({ name, desc, icon, tags }: { name: string; desc: string; icon: string; tags: string[] }) {
  return (
    <div className="journey-project-card reveal">
      <div className="journey-project-icon">{icon}</div>
      <h3 className="journey-project-name">{name}</h3>
      <p className="journey-project-desc">{desc}</p>
      <div className="journey-project-tags">
        {tags.map((t) => (
          <span key={t} className="journey-tag journey-tag--sm">{t}</span>
        ))}
      </div>
    </div>
  );
}

/* ─── Main Page ─── */
export default function JourneyPage(): React.ReactElement {
  const heroRef = useScrollReveal();
  const statsRef = useScrollReveal();
  const timelineRef = useScrollReveal();
  const badgesRef = useScrollReveal();
  const projectsRef = useScrollReveal();
  const ctaRef = useScrollReveal();

  return (
    <Layout
      title="The Captain's Journey"
      description="Follow Mehul Patel's 12+ year journey through DevOps, Cloud, and Open Source — from community builder to Docker Captain and Google Developer Expert.">

      {/* ═══ HERO ═══ */}
      <div className="journey-hero" ref={heroRef}>
        <div className="journey-hero-bg" aria-hidden="true">
          <div className="journey-matrix-rain" />
        </div>
        <div className="container journey-hero-container">
          <div className="journey-hero-left reveal">
            <div className="journey-hero-badge">
              <span className="journey-penguin">&#x1F427;</span> Open Source Journey
            </div>
            <h1 className="journey-hero-title">
              <span className="journey-prompt">~/</span>The Captain's
              <br />
              <span className="journey-hero-accent">Journey</span>
            </h1>
            <p className="journey-hero-subtitle">
              12+ years navigating the open-source seas. From writing my first
              <code> bash</code> script to becoming a Docker Captain, Google Developer
              Expert, and mentoring 50,000+ developers across 25+ countries.
            </p>
            <div className="journey-hero-cta">
              <a className="btn-primary" href="https://nomadicmehul.dev/" target="_blank" rel="noopener noreferrer">
                Full Profile
              </a>
              <Link className="btn-secondary" to="/">
                Back to CloudCaptain
              </Link>
            </div>
          </div>
          <div className="journey-hero-right reveal">
            <TerminalIntro />
          </div>
        </div>
      </div>

      {/* ═══ STATS ═══ */}
      <div className="journey-stats-section" ref={statsRef}>
        <div className="container">
          <div className="journey-stats-grid reveal">
            <JourneyStat number={12} suffix="+" label="Years Experience" icon="&#x1F4BB;" />
            <JourneyStat number={500} suffix="+" label="Sessions Delivered" icon="&#x1F399;" />
            <JourneyStat number={25} suffix="+" label="Countries" icon="&#x1F30D;" />
            <JourneyStat number={50} suffix="K+" label="Developers Reached" icon="&#x1F465;" />
            <JourneyStat number={305} suffix="+" label="Mentees" icon="&#x1F393;" />
            <JourneyStat number={49} suffix="/5" label="Avg. Rating" icon="&#x2B50;" />
          </div>
        </div>
      </div>

      {/* ═══ CERTIFICATIONS & RECOGNITIONS ═══ */}
      <div className="journey-section journey-section--badges" ref={badgesRef}>
        <div className="container">
          <h2 className="section__title reveal">
            <span className="journey-prompt">$ </span>Recognitions & Titles
          </h2>
          <p className="section__subtitle reveal">
            Earned through years of community leadership and open-source contributions.
          </p>
          <div className="journey-badges-grid">
            <BadgeCard title="Docker Captain" org="Docker Inc." icon="&#x1F433;" color="#2496ED" />
            <BadgeCard title="Google Developer Expert" org="Google Cloud Platform" icon="&#x2601;" color="#4285F4" />
            <BadgeCard title="AWS Community Builder" org="Amazon Web Services" icon="&#x1F527;" color="#FF9900" />
            <BadgeCard title="Auth0 Ambassador" org="Auth0 by Okta" icon="&#x1F510;" color="#EB5424" />
            <BadgeCard title="Mozilla Reps Council" org="Mozilla Foundation" icon="&#x1F525;" color="#FF7139" />
            <BadgeCard title="Open Source Advocate" org="OSW & OSCF Founder" icon="&#x1F427;" color="#10B981" />
          </div>
        </div>
      </div>

      {/* ═══ TIMELINE ═══ */}
      <div className="journey-section journey-section--timeline" ref={timelineRef}>
        <div className="container">
          <h2 className="section__title reveal">
            <span className="journey-prompt">$ </span>git log --oneline
          </h2>
          <p className="section__subtitle reveal">
            Key commits in the journey from engineer to community captain.
          </p>
          <div className="journey-timeline">
            <div className="journey-timeline-line" aria-hidden="true" />
            <TimelineEntry
              year="2012"
              icon="&#x1F4BB;"
              title="The First Commit"
              desc="Started my tech career with a passion for Linux and automation. Wrote my first shell scripts and fell in love with the terminal."
              tags={['Linux', 'Bash', 'Automation']}
            />
            <TimelineEntry
              year="2015"
              icon="&#x2601;"
              title="Entered the Cloud"
              desc="Dove deep into AWS and cloud-native architecture. Started evangelizing DevOps practices and containerization within my team."
              tags={['AWS', 'DevOps', 'Docker']}
            />
            <TimelineEntry
              year="2017"
              icon="&#x1F399;"
              title="Speaking & Community Building"
              desc="Delivered my first public talks. Founded local tech meetups and began organizing community events around cloud and DevOps."
              tags={['Public Speaking', 'Meetups', 'GDG']}
            />
            <TimelineEntry
              year="2019"
              icon="&#x1F30D;"
              title="Going Global"
              desc="Crossed 100+ sessions across multiple countries. Started mentoring developers worldwide and evangelizing cloud-native tech on the global stage."
              tags={['Public Speaking', 'Mentoring', 'Global']}
            />
            <TimelineEntry
              year="2021"
              icon="&#x1F527;"
              title="AWS Community Builder"
              desc="Recognized as an AWS Community Builder for Containers. Deepened expertise in container orchestration, cloud-native patterns, and ECS/EKS architectures."
              tags={['AWS Community Builder', 'Containers', 'Cloud Architecture']}
            />
            <TimelineEntry
              year="2022"
              icon="&#x1F3DB;"
              title="Building Local Communities"
              desc="Started organizing local tech communities — bringing developers together through meetups, workshops, and hackathons to learn and build in the open."
              tags={['Community', 'Meetups', 'Workshops', 'Hackathons']}
            />
            <TimelineEntry
              year="2023"
              icon="&#x2693;"
              title="CloudCaptain & Community Chapters"
              desc="Launched CloudCaptain as a free, open-source learning platform. Founded CNCF Gandhinagar, HashiCorp Gandhinagar, and GDG Cloud Gandhinagar chapters — scaling community impact."
              tags={['CloudCaptain', 'CNCF', 'HashiCorp', 'GDG Cloud']}
            />
            <TimelineEntry
              year="2024"
              icon="&#x1F433;"
              title="Docker Captain"
              desc="Appointed as an official Docker Captain — recognized for deep container expertise, community leadership, and evangelizing Docker across 25+ countries."
              tags={['Docker Captain', 'Containers', 'Community Leader']}
            />
            <TimelineEntry
              year="2025"
              icon="&#x1F30D;"
              title="Germany, GDE & Grafana"
              desc="Moved to Germany and became a Google Developer Expert for Cloud. Took on the role of Grafana and Friends Berlin organizer — bridging observability and community."
              tags={['GDE', 'Google Cloud', 'Grafana', 'Berlin']}
            />
            <TimelineEntry
              year="2026"
              icon="&#x1F916;"
              title="Shipping AI-Native Infrastructure"
              desc="Now building at the frontier where AI meets DevOps — crafting intelligent automation, MCP-powered cloud ops, and Docker Sandboxes that let AI agents ship code safely."
              tags={['AI Agents', 'MCP', 'Claude', 'Docker Sandboxes']}
            />
          </div>
        </div>
      </div>

      {/* ═══ OPEN SOURCE PROJECTS ═══ */}
      <div className="journey-section journey-section--projects" ref={projectsRef}>
        <div className="container">
          <h2 className="section__title reveal">
            <span className="journey-prompt">$ </span>ls ~/open-source/
          </h2>
          <p className="section__subtitle reveal">
            Some of the open-source projects I've built for the community.
          </p>
          <div className="journey-projects-grid">
            <ProjectCard
              name="CloudCaptain"
              desc="Free, open-source learning platform for Cloud, DevOps, AI & Operations with curated paths and hands-on exercises."
              icon="&#x2693;"
              tags={['Docusaurus', 'TypeScript', 'DevOps', 'Community']}
            />
            <ProjectCard
              name="Dispatch"
              desc="AI-powered CLI that uses Claude to automatically resolve GitHub issues with intelligent code analysis."
              icon="&#x1F916;"
              tags={['AI', 'Claude', 'GitHub', 'Python']}
            />
            <ProjectCard
              name="Azure MCP Server"
              desc="Model Context Protocol implementation for Azure services, enabling AI agents to interact with cloud resources."
              icon="&#x2601;"
              tags={['Azure', 'MCP', 'TypeScript', 'AI']}
            />
            <ProjectCard
              name="Hello GenAI"
              desc="Educational collection of generative AI applications to help developers get started with AI technologies."
              icon="&#x1F9E0;"
              tags={['GenAI', 'Education', 'Python', 'LLMs']}
            />
          </div>
        </div>
      </div>

      {/* ═══ CONNECT ═══ */}
      <div className="journey-section journey-section--cta" ref={ctaRef}>
        <div className="container" style={{ textAlign: 'center' }}>
          <h2 className="section__title reveal">
            <span className="journey-prompt">$ </span>Let's Connect
          </h2>
          <p className="section__subtitle reveal">
            Whether you want to collaborate on open source, invite me to speak, or just say hello.
          </p>
          <div className="journey-connect-grid reveal">
            <a href="https://github.com/nomadicmehul" target="_blank" rel="noopener noreferrer" className="journey-connect-card">
              <span className="journey-connect-icon">&#x1F4BB;</span>
              <span className="journey-connect-label">GitHub</span>
            </a>
            <a href="https://twitter.com/nomadicmehul" target="_blank" rel="noopener noreferrer" className="journey-connect-card">
              <span className="journey-connect-icon">&#x1F426;</span>
              <span className="journey-connect-label">Twitter</span>
            </a>
            <a href="https://nomadicmehul.dev/" target="_blank" rel="noopener noreferrer" className="journey-connect-card">
              <span className="journey-connect-icon">&#x1F310;</span>
              <span className="journey-connect-label">Website</span>
            </a>
            <a href="https://buymeacoffee.com/nomadicmehul" target="_blank" rel="noopener noreferrer" className="journey-connect-card">
              <span className="journey-connect-icon">&#x2615;</span>
              <span className="journey-connect-label">Buy Me a Coffee</span>
            </a>
          </div>
          <div className="journey-cta-buttons reveal" style={{ marginTop: '2rem' }}>
            <Link className="btn-primary" to="/career-paths">
              Explore Career Paths
            </Link>
            <Link className="btn-secondary" to="/docs/learning-paths/devops">
              Start Learning DevOps
            </Link>
          </div>
        </div>
      </div>
    </Layout>
  );
}
