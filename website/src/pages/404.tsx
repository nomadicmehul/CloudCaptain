import React from 'react';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';

const popularDestinations = [
  { label: '🐧 Linux Master Path', to: '/docs/learning-paths/linux-master' },
  { label: '🚀 DevOps Path', to: '/docs/learning-paths/devops' },
  { label: '🤖 AI / ML Ops Path', to: '/docs/learning-paths/ai-ml' },
  { label: '🐳 Docker', to: '/docs/tools/docker/' },
  { label: '☸️  Kubernetes', to: '/docs/tools/kubernetes/' },
  { label: '🏗️  Terraform', to: '/docs/tools/terraform/' },
  { label: '☁️  AWS', to: '/docs/cloud/aws/' },
  { label: '🎯 Career Paths', to: '/career-paths' },
];

export default function NotFound(): JSX.Element {
  return (
    <Layout
      title="404 — Lost at Sea"
      description="Page not found. Chart a new course with CloudCaptain.">
      <main
        style={{
          minHeight: '70vh',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: '4rem 1.5rem',
        }}>
        <div style={{maxWidth: 720, width: '100%', textAlign: 'center'}}>
          <div
            style={{
              fontFamily: 'var(--ifm-font-family-monospace, JetBrains Mono, monospace)',
              fontSize: '0.9rem',
              color: 'var(--ifm-color-emphasis-600)',
              marginBottom: '1rem',
              textAlign: 'left',
              background: 'var(--ifm-color-emphasis-100)',
              borderRadius: 8,
              padding: '1rem 1.25rem',
              border: '1px solid var(--ifm-color-emphasis-300)',
            }}>
            <span style={{color: '#10B981'}}>$</span> cd /requested/page
            <br />
            <span style={{color: '#EF4444'}}>bash: cd: /requested/page: No such file or directory</span>
            <br />
            <span style={{color: '#10B981'}}>$</span> exit 404
          </div>

          <h1 style={{fontSize: '3rem', marginBottom: '0.5rem'}}>🧭 Lost at Sea</h1>
          <p style={{fontSize: '1.15rem', color: 'var(--ifm-color-emphasis-700)', marginBottom: '2rem'}}>
            The page you're looking for drifted off the map. Let's get you back on course.
          </p>

          <div
            style={{
              display: 'flex',
              gap: '0.75rem',
              justifyContent: 'center',
              flexWrap: 'wrap',
              marginBottom: '2.5rem',
            }}>
            <Link className="button button--primary button--lg" to="/">
              🏠 Back to Home
            </Link>
            <Link className="button button--secondary button--lg" to="/docs/learning-paths/welcome">
              📚 Browse Learning Paths
            </Link>
          </div>

          <h2 style={{fontSize: '1.25rem', marginBottom: '1rem'}}>Popular Destinations</h2>
          <ul
            style={{
              listStyle: 'none',
              padding: 0,
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))',
              gap: '0.5rem',
            }}>
            {popularDestinations.map((d) => (
              <li key={d.to}>
                <Link
                  to={d.to}
                  style={{
                    display: 'block',
                    padding: '0.75rem 1rem',
                    border: '1px solid var(--ifm-color-emphasis-300)',
                    borderRadius: 8,
                    textDecoration: 'none',
                    transition: 'all 0.15s ease',
                  }}>
                  {d.label}
                </Link>
              </li>
            ))}
          </ul>

          <p style={{marginTop: '2.5rem', fontSize: '0.9rem', color: 'var(--ifm-color-emphasis-600)'}}>
            Think this link should work?{' '}
            <Link to="https://github.com/nomadicmehul/CloudCaptain/issues/new?template=bug_report.yml">
              Report a broken link
            </Link>
            .
          </p>
        </div>
      </main>
    </Layout>
  );
}
