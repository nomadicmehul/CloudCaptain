<div align="center">
  <img src="../website/static/img/cloudcaptain-logo.jpg" width="180" alt="CloudCaptain" />
  <h1>CloudCaptain</h1>
  <h3>Cloud, DevOps &amp; AIOps von Grund auf.<br/>Bau das Tool. Nutze das Tool. Liefere ein Artefakt aus.</h3>
  <p>
    <strong>104 Lektionen</strong> · <strong>15 Phasen live</strong> · <strong>151 Stunden</strong> · <strong>3.077 Tests</strong> · <strong>205 Skills, Prompts &amp; MCP-Server</strong>
  </p>
  <p>
    <a href="https://cloudcaptain.io/learn"><img src="https://img.shields.io/badge/Start_the_course-cloudcaptain.io%2Flearn-1E9BD7?style=for-the-badge" alt="Kurs starten" /></a>
  </p>
  <p>
    <a href="https://github.com/nomadicmehul/CloudCaptain/actions/workflows/lesson-smoke.yml"><img src="https://github.com/nomadicmehul/CloudCaptain/actions/workflows/lesson-smoke.yml/badge.svg" alt="Smoke-Tests der Lektionen" /></a>
    <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT-Lizenz" /></a>
    <img src="https://img.shields.io/badge/PRs-welcome-blue" alt="PRs willkommen" />
    <a href="https://github.com/nomadicmehul/CloudCaptain/stargazers"><img src="https://img.shields.io/github/stars/nomadicmehul/cloudcaptain?style=flat&logo=github" alt="GitHub-Sterne" /></a>
  </p>
  <p>
    <a href="../README.md">English</a> ·
    <a href="./README.hi.md">हिन्दी</a> ·
    <a href="./README.mr.md">मराठी</a> ·
    <a href="./README.gu.md">ગુજરાતી</a> ·
    <strong>Deutsch</strong> ·
    <a href="./README.es.md">Español</a> ·
    <a href="./README.pt.md">Português</a> ·
    <a href="./README.zh.md">中文</a> ·
    <a href="./README.ja.md">日本語</a>
  </p>
  <p><em>Dies ist eine Übersetzung. Maßgeblich ist die <a href="../README.md">englische README</a>; Korrekturen sind per Pull Request willkommen.</em></p>
</div>

```bash
npx skills add nomadicmehul/CloudCaptain   # installs the tutor into Claude Code, Cursor, Codex or any agent that reads SKILL.md
```

Dann rufst du in deinem Agenten `/captain-start` auf: ein kurzes Einstufungsquiz, ein Lernpfad und die erste Lektion.

<div align="center">
  <img src="../website/static/img/tutor-demo.gif" width="760" alt="The CloudCaptain tutor: npx skills add, then /captain-start asks your goal, level and time and starts the placement quiz" />
</div>

---

## Was du baust

Jede Lektion beginnt mit einem Problem aus der Produktion. Du baust den Mechanismus selbst in reinem Python oder Bash, mit Tests, und erledigst danach dieselbe Aufgabe mit dem echten Tool. So lässt sich jedes Flag und jede Fehlermeldung auf Code zurückführen, den du selbst geschrieben hast.

| Phase | Du baust | Danach nutzt du |
|:--|:--|:--|
| **00 · Setup & Lab** | einen Lab-Doctor, der deinen Rechner prüft und den Installationsplan ausgibt | kind, Docker, deinen Coding-Agenten |
| **01 · Linux & Systeme** | einen PID-1-Supervisor, einen Detektor für fd-Leaks, einen strace-Analyzer, einen Capability-Decoder, systemd-Units, cgroups von Hand, einen Leser für die Mount-Tabelle, ein Dashboard nach der USE-Methode | `strace`, `capsh`, `systemctl`, `findmnt`, PSI |
| **02 · Netzwerke** | eine Routing-Abfrage wie `ip route get`, einen iterativen DNS-Resolver, einen Analyzer für TCP-Socket-Zustände, einen pcap-Leser mit tcpdump-Filtern, einen Parser für TLS-ClientHello mit Prüfung von Zertifikatsketten, einen HTTP/1.1-Server mit Reverse Proxy, einen Load Balancer mit Health Checks, einen Caching-Proxy nach RFC 9111 | `ip`, `dig`, `ss`, tcpdump, `openssl`, curl, nginx, HAProxy, CoreDNS |
| **03 · Git & Zusammenarbeit** | einen inhaltsadressierten Objektspeicher, den `git fsck` akzeptiert, eine Diff- und Drei-Wege-Merge-Engine, einen Packfile- und Smart-HTTP-Clone-Client, eine Merge Queue mit CODEOWNERS und Rulesets, Hooks nur für gestagte Änderungen mit einer `pre-receive`-Policy, Bisect und Blame über den Commit-Graphen | `git`-Plumbing, `git merge-file`, `GIT_TRACE_PACKET`, Rulesets, die Merge Queue, pre-commit, `git bisect run` |
| **04 · Scripting & Automatisierung** | einen Bash-Linter mit Strict-Mode-Prüfungen, ein CLI-Framework, einen API-Client mit Retries und Rate Limits, Teilmengen von jq und awk, einen Scheduler für cron und OnCalendar, einen reproduzierbaren Wheel-Builder | ShellCheck, curl, urllib3, jq, awk, croniter, `systemd-analyze`, pip und `build` |
| **05 · Container** | eine Container-Runtime, einen Image-Auditor, einen Registry-Client, einen Image-Builder ohne Dockerfile, ein veth/Bridge-Netzwerk, Compose, rootless User-Namespaces, einen seccomp-Generator | Docker, Podman, OCI-Registries |
| **06 · Orchestrierung** | einen Reconciler, einen Scheduler, ein Kubelet, kube-proxy-Regeln und Cluster-DNS, einen RBAC-Authorizer, Helm, einen Operator, einen Gateway-API-Router | Kubernetes auf kind, Helm, Envoy Gateway |
| **07 · Infrastructure as Code** | eine Plan/Apply-Engine, einen Abhängigkeitsgraphen, Module, einen State-Server mit Locking, Drift-Erkennung, eine Policy-Engine, Frontends für Pulumi und Crossplane, einen idempotenten Konfigurationsmanager | OpenTofu, conftest/OPA, Pulumi, Ansible |
| **08 · CI/CD & GitOps** | einen Workflow-Runner, einen Auditor für Workflow-Sicherheit, einen OIDC-Issuer mit Verifier, einen SBOM-Generator, einen Signierer mit Transparency Log, einen GitOps-Reconciler, einen Canary-Controller, ein Release-Tool | GitHub Actions und act, zizmor, cosign, syft, Argo CD, Argo Rollouts, git-cliff |
| **09 · Cloud (AWS zuerst)** | einen Evaluator für IAM-Policies, einen Analyzer für VPC-Erreichbarkeit, SigV4-Signierung und einen S3-Server, die Lambda Runtime API, eine Auto Scaling Group, einen Kostenallokator nach FOCUS, einen Security-Posture-Scanner | die AWS CLI gegen moto, den Lambda-Emulator, Checkov |
| **10 · Observability & SRE** | eine TSDB mit Gorilla-Chunks, eine PromQL-Engine, einen Alert-Router, eine Log-Pipeline mit LogQL-Teilmenge, einen Tracer mit Tail Sampling, SLO-Burn-Rate-Regeln, einen Incident-Service, Chaos-Experimente | Prometheus, promtool, OpenTelemetry Collector, Jaeger, Sloth, Alertmanager, Loki, Grafana Alloy, Chaos Mesh |
| **11 · Security & DevSecOps** | einen Secrets-Dienst mit Leases und Shamir-Entsiegelung, einen Admission-Webhook mit CEL-Teilmenge, eine Regel-Engine im Stil von Falco, einen NetworkPolicy-Evaluator, einen Secret-Scanner, SPIFFE-Identitäten mit mTLS, eine CVE-Priorisierung mit CVSS, EPSS und VEX, einen CIS-Benchmark-Runner | OpenBao, ValidatingAdmissionPolicy, Kyverno, Falco, gitleaks, SPIRE, Trivy, Grype, kube-bench |
| **12 · Platform Engineering** | einen Scaffolder für Service-Templates mit sicheren Updates, einen Service-Katalog mit Ownern und Abhängigkeiten, DORA-Metriken aus Git, Deployments und Incidents, Preview-Umgebungen pro Pull Request, eine Plattform-API, die eine Spezifikation in Kubernetes-Objekte übersetzt | copier, das Katalogmodell von Backstage, DuckDB, `kubectl auth can-i`, score-k8s |
| **13 · KI-Infrastruktur** | einen GPU-Scheduler mit DRA, einen Simulator für LLM-Serving, ein Gateway, das Tokens zählt, einen queue-basierten Autoscaler, einen Prefix-Cache-Router, HNSW- und IVF-Indizes, einen Inspektor für Modellartefakte, ein Token-Kostenmodell | llama.cpp auf der CPU, LiteLLM, pgvector, llm-d, ORAS |
| **14 · AIOps & Agentic Ops** | einen MCP-Server für kubectl, einen Agenten für Incident-Triage, KI-gestützte Loganalyse, OpenTelemetry für LLM-Aufrufe, Agent-Sandboxes, sichere Write-Tools, Agent-Evals | Claude Code, die Claude API, OpenTelemetry, promptfoo |

Zwei weitere Phasen (MLOps, Abschlussprojekte) stehen auf der [Roadmap](../ROADMAP.md).

## So funktioniert eine Lektion

```
The Problem  →  The Concept  →  Build It  →  Use It  →  Ship It  →  Quiz
 a real          one diagram     standard     the real    a skill,    six questions,
 incident                        library +    tool, same  prompt or   before and
                                 tests        job         MCP server  after
```

- **Lauffähig und getestet.** Jede Lektion enthält Code mit Unit-Tests, die überall laufen (kein root, kein Docker, kein Cloud-Account), und ein `smoke.sh`, das die reinen Linux-Schritte und die echten Tools auf einem CI-Runner mit Docker, kind und OpenTofu ausführt. Erst wenn das durchläuft, zeigt eine Lektion „last verified“ an.
- **Aktuell.** Tool-Versionen werden wöchentlich nachverfolgt, eingestellte Technologien (ingress-nginx, PodSecurityPolicy, Swarm, Bitnami-Images) werden überall gekennzeichnet, wo sie auftauchen, und die KI-Lektionen nutzen aktuelle Modelle und APIs.
- **Gehört dir.** Jede Lektion legt einen Agent-Skill, einen Prompt oder einen MCP-Server in `outputs/` ab. Am Ende hast du also einen Werkzeugkasten, nicht nur Notizen.

## Lernpfade

| Pfad | Für | Lektionen |
|:--|:--|:--|
| [DevOps Engineer](https://cloudcaptain.io/learn/paths/devops) | vom einzelnen Linux-Prozess bis zu Kubernetes, IaC, Cloud und AIOps | 96 |
| [Site Reliability Engineer](https://cloudcaptain.io/learn/paths/sre) | Metriken, Alerts, Logs, Traces, SLOs, Incidents und Chaos auf Linux und Kubernetes | 38 |
| [DevSecOps Engineer](https://cloudcaptain.io/learn/paths/devsecops) | Secrets, Admission, Laufzeiterkennung, Network Policies, Supply Chain und Cloud-IAM | 42 |
| [AI Platform Engineer](https://cloudcaptain.io/learn/paths/ai-platform) | Engineers, die Modelle betreiben sollen: GPUs, Serving, Gateways, Routing, Kosten | 23 |
| [AIOps Engineer](https://cloudcaptain.io/learn/paths/aiops) | Agenten sicher für Infrastrukturaufgaben einsetzen | 9 |

## Der Tutor in deinem Agenten

`npx skills add nomadicmehul/CloudCaptain` installiert vier Skills: `captain-start` (Einstufungsquiz und ein Plan in `LEARNING.md`), `captain-learn` (eine Lektion pro Session, Build It → Use It → Ship It, mit Quiz und Tests), `captain-guide` (wo lerne ich X, was braucht Rolle Y) und `captain-check` (ein sokratisches Review einer Phase). Sie funktionieren in Claude Code, Cursor, Codex und jedem Agenten, der `SKILL.md` liest.

## Die Referenzbibliothek

Neben dem Curriculum bietet [cloudcaptain.io/docs](https://cloudcaptain.io/docs) die ausführlichen Guides, mit denen CloudCaptain angefangen hat: über 150 Seiten eigene Texte, 21 Cheat Sheets für Befehle, 19 Sammlungen von Interviewfragen, 5 Leitfäden zur Prüfungsvorbereitung für Zertifizierungen (CKA/CKAD/CKS, Terraform, AWS, Azure, GCP), 7 Lernpfade und 8 Karrierepfade. Die Lektionen verlinken dorthin, statt die Inhalte zu wiederholen, und die Referenzseiten, auf denen eine Lektion aufbaut, verlinken mit einer „Build this from scratch“-Karte zurück.

Der Lesemodus **Captain's Bridge** auf der Website verfolgt deinen Fortschritt pro Abschnitt, und [/progress](https://cloudcaptain.io/progress) speichert Lektionen, Quiz-Ergebnisse und Gelesenes in deinem Browser. Kein Account, kein Tracking.

<div align="center">
  <img src="../website/static/img/captains-bridge-demo.gif" width="760" alt="Captain's Bridge: Abschnitte werden beim Lesen abgehakt, und /progress merkt sich, was du gelesen hast" />
</div>

## Warum CloudCaptain

| | CloudCaptain | roadmap.sh | devops-exercises | awesome-*-Listen |
|:--|:--|:--|:--|:--|
| Jedes Tool von Grund auf selbst bauen, mit Tests | ✅ 104 Lektionen | ❌ | ❌ | ❌ |
| In CI gegen die echten Tools verifiziert | ✅ jede Lektion | ❌ | ❌ | ❌ |
| KI-Infrastruktur und AIOps | ✅ 15 Lektionen | teilweise | ❌ | Links |
| Ein Agent-Tutor zum Installieren | ✅ `npx skills add` | ❌ | ❌ | ❌ |
| Ausführliche Guides, Cheat Sheets, Interview-Q&A, Prüfungsvorbereitung | ✅ | Roadmap-Knoten verlinken nach außen | Q&A | Links |
| Lese- und Quiz-Fortschritt | ✅ in deinem Browser | ✅ | ❌ | ❌ |

## Lokal ausführen

```bash
git clone https://github.com/nomadicmehul/CloudCaptain.git
cd CloudCaptain/website
npm install
npm start                                  # http://localhost:3000, lessons at /learn
npm run curriculum:test -- linux/pid1-supervisor   # one lesson's tests
```

Jede Lektion ist ein Ordner: `curriculum/phases/NN-phase/NN-lesson/` mit `docs/en.md`, `code/` (mit Tests und `smoke.sh`), `quiz.json` und `outputs/`. Das Format legt [LESSON_TEMPLATE.md](../LESSON_TEMPLATE.md) fest; die Übersicht über das Repo steht in [AGENTS.md](../AGENTS.md).

## Mitwirken

Lektionen, Fixes und Übersetzungen sind gleichermaßen willkommen; siehe [CONTRIBUTING.md](../CONTRIBUTING.md). Eine neue Lektion beginnt mit `npm run curriculum:scaffold`, muss den Validator und ihre eigenen Tests bestehen und wird in ihrem Pull Request durch den Smoke-Run verifiziert. Für veraltete Inhalte gibt es ein eigenes Issue-Template, und jeder Pull Request bekommt ein Preview-Deployment.

Du unterrichtest mit CloudCaptain an einem Bootcamp, einer Hochschule oder in einem Unternehmen? [FORKING.md](../FORKING.md) erklärt, wie du es forkst und anpasst. Wenn du die Arbeit unterstützen möchtest, sieh dir [SPONSORS.md](../SPONSORS.md) an.

## Besucherzahlen

<div align="center">
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-total.json" alt="Besuche insgesamt" />
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-week.json" alt="Besuche diese Woche" />
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-today.json" alt="Besuche heute" />

  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../.github/stats/chart-dark.svg">
    <img src="../.github/stats/chart-light.svg" width="800" alt="Besucherzahlen der letzten 90 Tage" />
  </picture>
  <p><em>Täglich aktualisiert über <a href="https://www.goatcounter.com">GoatCounter</a>: datenschutzfreundlich, keine Cookies, kein Tracking einzelner Personen.</em></p>
</div>

## Lizenz

MIT, siehe [LICENSE](../LICENSE). Nutze es, forke es, unterrichte damit.

---

<div align="center">
  <p>Entwickelt von <a href="https://nomadicmehul.dev/">Mehul Patel</a> und der Open-Source-Community · <a href="https://cloudcaptain.io">cloudcaptain.io</a></p>
  <p>Wenn dir CloudCaptain geholfen hat, <a href="https://github.com/nomadicmehul/CloudCaptain">gib dem Repo einen Stern</a>: So finden andere Engineers eine kostenlose Alternative zu kostenpflichtigen Kursen.</p>
</div>
