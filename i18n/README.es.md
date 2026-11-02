<div align="center">
  <img src="../website/static/img/cloudcaptain-logo.jpg" width="180" alt="CloudCaptain" />
  <h1>CloudCaptain</h1>
  <h3>Cloud, DevOps y AIOps desde cero.<br/>Construye la herramienta. Usa la herramienta. Entrega un artefacto.</h3>
  <p>
    <strong>71 lecciones</strong> · <strong>10 fases publicadas</strong> · <strong>104 horas</strong> · <strong>2,001 pruebas</strong> · <strong>139 skills, prompts y servidores MCP</strong>
  </p>
  <p>
    <a href="https://cloudcaptain.io/learn"><img src="https://img.shields.io/badge/Start_the_course-cloudcaptain.io%2Flearn-1E9BD7?style=for-the-badge" alt="Empieza el curso" /></a>
  </p>
  <p>
    <a href="https://github.com/nomadicmehul/CloudCaptain/actions/workflows/lesson-smoke.yml"><img src="https://github.com/nomadicmehul/CloudCaptain/actions/workflows/lesson-smoke.yml/badge.svg" alt="Smoke tests de las lecciones" /></a>
    <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="Licencia MIT" /></a>
    <img src="https://img.shields.io/badge/PRs-welcome-blue" alt="PRs bienvenidos" />
    <a href="https://github.com/nomadicmehul/CloudCaptain/stargazers"><img src="https://img.shields.io/github/stars/nomadicmehul/cloudcaptain?style=flat&logo=github" alt="Estrellas en GitHub" /></a>
  </p>
  <p>
    <a href="../README.md">English</a> ·
    <a href="./README.hi.md">हिन्दी</a> ·
    <a href="./README.mr.md">मराठी</a> ·
    <a href="./README.gu.md">ગુજરાતી</a> ·
    <a href="./README.de.md">Deutsch</a> ·
    <strong>Español</strong> ·
    <a href="./README.pt.md">Português</a> ·
    <a href="./README.zh.md">中文</a> ·
    <a href="./README.ja.md">日本語</a>
  </p>
  <p><sub>Esta es una traducción: el <a href="../README.md">README en inglés</a> es la fuente de verdad. Las correcciones son bienvenidas mediante pull request.</sub></p>
</div>

```bash
npx skills add nomadicmehul/CloudCaptain   # installs the tutor into Claude Code, Cursor, Codex or any agent that reads SKILL.md
```

Luego pídele a tu agente `/captain-start`: un breve cuestionario de nivelación, una ruta de aprendizaje y la primera lección.

<div align="center">
  <img src="../website/static/img/tutor-demo.gif" width="760" alt="The CloudCaptain tutor: npx skills add, then /captain-start asks your goal, level and time and starts the placement quiz" />
</div>

---

## Lo que construyes

Cada lección parte de un problema de producción, te hace construir el mecanismo tú mismo en Python o Bash puro con pruebas y luego hacer el mismo trabajo con la herramienta real, de modo que cada flag y cada mensaje de error corresponde a código que escribiste tú.

| Fase | Construyes | Luego ejecutas |
|:--|:--|:--|
| **00 · Configuración y laboratorio** | un lab doctor que revisa tu máquina e imprime el plan de instalación | kind, Docker, tu agente de programación |
| **01 · Linux y sistemas** | un supervisor de PID 1, un detector de fugas de fd, un analizador de strace, un decodificador de capabilities, unidades de systemd, cgroups a mano, un lector de la tabla de montajes, un dashboard con el método USE | `strace`, `capsh`, `systemctl`, `findmnt`, PSI |
| **02 · Redes** | una consulta de rutas como `ip route get`, un resolvedor DNS iterativo, un analizador de estados de sockets TCP, un lector de pcap con filtros de tcpdump, un parser de ClientHello de TLS y un verificador de cadenas de certificados, un servidor HTTP/1.1 con proxy inverso, un balanceador de carga con health checks, un proxy de caché según la RFC 9111 | `ip`, `dig`, `ss`, tcpdump, `openssl`, curl, nginx, HAProxy, CoreDNS |
| **05 · Contenedores** | un runtime de contenedores, un auditor de imágenes, un cliente de registry, un constructor de imágenes sin Dockerfile, una red veth/bridge, Compose, user namespaces rootless, un generador de seccomp | Docker, Podman, registries OCI |
| **06 · Orquestación** | un reconciliador, un scheduler, un kubelet, reglas de kube-proxy y DNS del cluster, un autorizador RBAC, Helm, un operador, un router de Gateway API | Kubernetes sobre kind, Helm, Envoy Gateway |
| **07 · Infraestructura como código** | un motor de plan/apply, un grafo de dependencias, módulos, un servidor de estado con bloqueo, detección de drift, un motor de políticas, front ends de Pulumi y Crossplane, un gestor de configuración idempotente | OpenTofu, conftest/OPA, Pulumi, Ansible |
| **08 · CI/CD y GitOps** | un ejecutor de workflows, un auditor de seguridad de workflows, un emisor y verificador OIDC, un generador de SBOM, un firmador con registro de transparencia, un reconciliador GitOps, un controlador canary, una herramienta de releases | GitHub Actions y act, zizmor, cosign, syft, Argo CD, Argo Rollouts, git-cliff |
| **09 · Cloud (AWS primero)** | un evaluador de políticas IAM, un analizador de alcanzabilidad de VPC, firma SigV4 y un servidor S3, la runtime API de Lambda, un grupo de Auto Scaling, un asignador de costos FOCUS, un escáner de postura de seguridad | la AWS CLI contra moto, el emulador de Lambda, Checkov |
| **13 · Infraestructura de IA** | un scheduler de GPU con DRA, un simulador de serving de LLM, un gateway que cuenta tokens, un autoscaler basado en colas, un router con caché de prefijos, índices HNSW e IVF, un inspector de artefactos de modelos, un modelo de costos por token | llama.cpp en CPU, LiteLLM, pgvector, llm-d, ORAS |
| **14 · AIOps y operaciones agénticas** | un servidor MCP para kubectl, un agente de triaje de incidentes, análisis de logs con IA, OpenTelemetry para llamadas a LLM, sandboxes para agentes, herramientas de escritura seguras, evals de agentes | Claude Code, la API de Claude, OpenTelemetry, promptfoo |

Otras siete fases (Git, scripting, observabilidad, seguridad, ingeniería de plataformas, MLOps, proyectos finales) están en la [hoja de ruta](../ROADMAP.md).

## Cómo funciona una lección

```
The Problem  →  The Concept  →  Build It  →  Use It  →  Ship It  →  Quiz
 a real          one diagram     standard     the real    a skill,    six questions,
 incident                        library +    tool, same  prompt or   before and
                                 tests        job         MCP server  after
```

- **Ejecutable y con pruebas.** Cada lección tiene código con pruebas unitarias que corren en cualquier lugar (sin root, sin Docker, sin cuenta en la nube) y un `smoke.sh` que ejecuta los pasos exclusivos de Linux y las herramientas reales en un runner de CI con Docker, kind y OpenTofu. Una lección muestra "last verified" (última verificación) solo después de que eso pasa.
- **Actualizado.** Las versiones de las herramientas se revisan cada semana, la tecnología retirada (ingress-nginx, PodSecurityPolicy, Swarm, imágenes de Bitnami) se señala dondequiera que aparezca y las lecciones de IA usan los modelos y las APIs actuales.
- **Para quedártelo.** Cada lección entrega una skill para agentes, un prompt o un servidor MCP en `outputs/`, así que terminas con una caja de herramientas, no solo con notas.

## Rutas de aprendizaje

| Ruta | Para | Lecciones |
|:--|:--|:--|
| [DevOps Engineer](https://cloudcaptain.io/learn/paths/devops) | de un solo proceso de Linux a Kubernetes, IaC, cloud y AIOps | 63 |
| [AI Platform Engineer](https://cloudcaptain.io/learn/paths/ai-platform) | ingenieros a quienes les piden operar modelos: GPUs, serving, gateways, enrutamiento, costos | 23 |
| [AIOps Engineer](https://cloudcaptain.io/learn/paths/aiops) | poner agentes a trabajar en la infraestructura de forma segura | 9 |

## El tutor en tu agente

`npx skills add nomadicmehul/CloudCaptain` instala cuatro skills: `captain-start` (cuestionario de nivelación y un plan en `LEARNING.md`), `captain-learn` (una lección por sesión, Build It → Use It → Ship It, con el cuestionario y las pruebas), `captain-guide` (dónde aprendo X, qué necesita el rol Y) y `captain-check` (un repaso socrático de una fase). Funcionan en Claude Code, Cursor, Codex y cualquier agente que lea `SKILL.md`.

## La biblioteca de referencia

Junto al currículo, [cloudcaptain.io/docs](https://cloudcaptain.io/docs) conserva las guías extensas con las que empezó CloudCaptain: más de 150 páginas de contenido original, 21 cheat sheets de comandos, 19 bancos de preguntas de entrevista, 5 guías de preparación para exámenes de certificación (CKA/CKAD/CKS, Terraform, AWS, Azure, GCP), 7 rutas de aprendizaje y 8 rutas profesionales. Las lecciones enlazan a ella en lugar de repetirla, y las páginas de referencia en las que se basa una lección enlazan de vuelta con una tarjeta "Build this from scratch" (constrúyelo desde cero).

El modo de lectura **Captain's Bridge** del sitio registra tu avance por sección, y [/progress](https://cloudcaptain.io/progress) recuerda en tu navegador las lecciones, las puntuaciones de los cuestionarios y lo que has leído. Sin cuenta, sin rastreo.

<div align="center">
  <img src="../website/static/img/captains-bridge-demo.gif" width="760" alt="Captain's Bridge: las secciones se marcan a medida que lees y /progress recuerda lo que has leído" />
</div>

## Por qué CloudCaptain

| | CloudCaptain | roadmap.sh | devops-exercises | listas awesome-* |
|:--|:--|:--|:--|:--|
| Construir cada herramienta desde cero, con pruebas | ✅ 71 lecciones | ❌ | ❌ | ❌ |
| Verificado contra las herramientas reales en CI | ✅ todas las lecciones | ❌ | ❌ | ❌ |
| Infraestructura de IA y AIOps | ✅ 15 lecciones | parcial | ❌ | enlaces |
| Un tutor que instalas en tu agente | ✅ `npx skills add` | ❌ | ❌ | ❌ |
| Guías extensas, cheat sheets, preguntas de entrevista, preparación de exámenes | ✅ | los nodos del roadmap enlazan fuera | preguntas y respuestas | enlaces |
| Progreso de lectura y de cuestionarios | ✅ en tu navegador | ✅ | ❌ | ❌ |

## Ejecútalo en local

```bash
git clone https://github.com/nomadicmehul/CloudCaptain.git
cd CloudCaptain/website
npm install
npm start                                  # http://localhost:3000, lessons at /learn
npm run curriculum:test -- linux/pid1-supervisor   # one lesson's tests
```

Cada lección es una carpeta: `curriculum/phases/NN-phase/NN-lesson/` con `docs/en.md`, `code/` (con pruebas y `smoke.sh`), `quiz.json` y `outputs/`. El contrato es [LESSON_TEMPLATE.md](../LESSON_TEMPLATE.md); el mapa del repo es [AGENTS.md](../AGENTS.md).

## Contribuir

Lecciones, correcciones y traducciones son bienvenidas; consulta [CONTRIBUTING.md](../CONTRIBUTING.md). Una lección nueva empieza con `npm run curriculum:scaffold`, debe pasar el validador y sus pruebas, y la verifica la ejecución de smoke tests en su pull request. El contenido desactualizado tiene su propia plantilla de issue, y cada pull request obtiene un despliegue de vista previa.

¿Enseñas con CloudCaptain en un bootcamp, una universidad o una empresa? [FORKING.md](../FORKING.md) explica cómo hacer un fork y adaptarlo. Para apoyar el proyecto, consulta [SPONSORS.md](../SPONSORS.md).

## Tráfico del sitio

<div align="center">
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-total.json" alt="Visitas totales" />
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-week.json" alt="Visitas esta semana" />
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-today.json" alt="Visitas hoy" />

  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../.github/stats/chart-dark.svg">
    <img src="../.github/stats/chart-light.svg" width="800" alt="Tráfico de visitantes en los últimos 90 días" />
  </picture>
  <p><em>Actualizado a diario con <a href="https://www.goatcounter.com">GoatCounter</a>: respetuoso con la privacidad, sin cookies, sin rastreo de personas.</em></p>
</div>

## Licencia

MIT, consulta [LICENSE](../LICENSE). Úsalo, haz un fork, enseña con él.

---

<div align="center">
  <p>Creado por <a href="https://nomadicmehul.dev/">Mehul Patel</a> y la comunidad open source · <a href="https://cloudcaptain.io">cloudcaptain.io</a></p>
  <p>Si CloudCaptain te ayudó, <a href="https://github.com/nomadicmehul/CloudCaptain">dale una estrella al repo</a>: así otros ingenieros encuentran una alternativa gratuita a los cursos de pago.</p>
</div>
