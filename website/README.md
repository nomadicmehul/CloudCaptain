# CloudCaptain website

Docusaurus 3 site for [cloudcaptain.io](https://cloudcaptain.io). The repo uses **npm** (see `package-lock.json`).

```bash
npm ci            # install dependencies
npm start         # dev server at http://localhost:3000
npm run build     # production build into build/
npm run serve     # serve the production build locally
npm run typecheck # tsc
```

Deployment is automatic: `.github/workflows/deploy.yml` builds and publishes to GitHub Pages on every push to `main`; pull requests get a preview deployment.

Project conventions live in [`CLAUDE.md`](./CLAUDE.md) and the root [`CONTRIBUTING.md`](../CONTRIBUTING.md).
