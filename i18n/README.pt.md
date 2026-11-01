<div align="center">
  <img src="../website/static/img/cloudcaptain-logo.jpg" width="180" alt="CloudCaptain" />
  <h1>CloudCaptain</h1>
  <h3>Cloud, DevOps &amp; AIOps do zero.<br/>Construa a ferramenta. Use a ferramenta. Entregue um artefato.</h3>
  <p>
    <strong>55 lições</strong> · <strong>8 fases disponíveis</strong> · <strong>80 horas</strong> · <strong>1.429 testes</strong> · <strong>107 skills, prompts &amp; servidores MCP</strong>
  </p>
  <p>
    <a href="https://cloudcaptain.io/learn"><img src="https://img.shields.io/badge/Start_the_course-cloudcaptain.io%2Flearn-1E9BD7?style=for-the-badge" alt="Comece o curso" /></a>
  </p>
  <p>
    <a href="https://github.com/nomadicmehul/CloudCaptain/actions/workflows/lesson-smoke.yml"><img src="https://github.com/nomadicmehul/CloudCaptain/actions/workflows/lesson-smoke.yml/badge.svg" alt="Smoke tests das lições" /></a>
    <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="Licença MIT" /></a>
    <img src="https://img.shields.io/badge/PRs-welcome-blue" alt="PRs bem-vindos" />
    <a href="https://github.com/nomadicmehul/CloudCaptain/stargazers"><img src="https://img.shields.io/github/stars/nomadicmehul/cloudcaptain?style=flat&logo=github" alt="Estrelas no GitHub" /></a>
  </p>
  <p>
    <a href="../README.md">English</a> ·
    <a href="./README.hi.md">हिन्दी</a> ·
    <a href="./README.mr.md">मराठी</a> ·
    <a href="./README.gu.md">ગુજરાતી</a> ·
    <a href="./README.de.md">Deutsch</a> ·
    <a href="./README.es.md">Español</a> ·
    <strong>Português</strong> ·
    <a href="./README.zh.md">中文</a> ·
    <a href="./README.ja.md">日本語</a>
  </p>
  <p><em>Esta é uma tradução: o <a href="../README.md">README em inglês</a> é a fonte oficial. Correções são bem-vindas via pull request.</em></p>
</div>

```bash
npx skills add nomadicmehul/CloudCaptain   # installs the tutor into Claude Code, Cursor, Codex or any agent that reads SKILL.md
```

Depois, peça ao seu agente `/captain-start`: um quiz de nivelamento curto, uma trilha de aprendizado e a primeira lição.

---

## O que você constrói

Toda lição começa com um problema de produção, faz você construir o mecanismo por conta própria em Python ou Bash puro, com testes, e depois fazer o mesmo trabalho com a ferramenta real, de modo que cada flag e cada mensagem de erro corresponde a um código que você escreveu.

| Fase | Você constrói | Depois você roda |
|:--|:--|:--|
| **00 · Setup e Laboratório** | um lab doctor que examina sua máquina e mostra o plano de instalação | kind, Docker, seu agente de código |
| **01 · Linux e Sistemas** | um supervisor de PID 1, um detector de vazamento de fd, um analisador de strace, um decodificador de capabilities, units do systemd, cgroups configurados à mão, um leitor da tabela de montagem, um dashboard com o método USE | `strace`, `capsh`, `systemctl`, `findmnt`, PSI |
| **05 · Containers** | um runtime de containers, um auditor de imagens, um cliente de registry, um builder de imagens sem Dockerfile, uma rede veth/bridge, Compose, user namespaces rootless, um gerador de perfis seccomp | Docker, Podman, registries OCI |
| **06 · Orquestração** | um reconciliador, um scheduler, um kubelet, regras do kube-proxy e DNS do cluster, um autorizador RBAC, Helm, um operator, um roteador de Gateway API | Kubernetes no kind, Helm, Envoy Gateway |
| **07 · Infraestrutura como Código** | um motor de plan/apply, um grafo de dependências, módulos, um servidor de state com locking, detecção de drift, um motor de políticas, front-ends em Pulumi e Crossplane, um gerenciador de configuração idempotente | OpenTofu, conftest/OPA, Pulumi, Ansible |
| **09 · Cloud (AWS primeiro)** | um avaliador de políticas IAM, um analisador de alcançabilidade de VPC, assinatura SigV4 e um servidor S3, a Runtime API do Lambda, um Auto Scaling group, um alocador de custos FOCUS, um scanner de postura de segurança | a AWS CLI contra o moto, o emulador do Lambda, Checkov |
| **13 · Infraestrutura de IA** | um scheduler de GPU com DRA, um simulador de serving de LLM, um gateway que conta tokens, um autoscaler baseado em fila, um roteador com prefix cache, índices HNSW e IVF, um inspetor de artefatos de modelo, um modelo de custo por token | llama.cpp em CPU, LiteLLM, pgvector, llm-d, ORAS |
| **14 · AIOps e Agentic Ops** | um servidor MCP para kubectl, um agente de triagem de incidentes, análise de logs com IA, OpenTelemetry para chamadas de LLM, sandboxes para agentes, ferramentas de escrita seguras, evals de agentes | Claude Code, a Claude API, OpenTelemetry, promptfoo |

Mais nove fases (redes, Git, scripting, CI/CD, observabilidade, segurança, platform engineering, MLOps, projetos finais) estão no [roadmap](../ROADMAP.md).

## Como funciona uma lição

```
The Problem  →  The Concept  →  Build It  →  Use It  →  Ship It  →  Quiz
 a real          one diagram     standard     the real    a skill,    six questions,
 incident                        library +    tool, same  prompt or   before and
                                 tests        job         MCP server  after
```

- **Executável e testada.** Toda lição tem código com testes unitários que rodam em qualquer lugar (sem root, sem Docker, sem conta de cloud) e um `smoke.sh` que executa as etapas exclusivas de Linux e as ferramentas reais em um runner de CI com Docker, kind e OpenTofu. Uma lição só exibe "last verified" depois que isso passa.
- **Atual.** As versões das ferramentas são acompanhadas semanalmente, tecnologias aposentadas (ingress-nginx, PodSecurityPolicy, Swarm, imagens Bitnami) são sinalizadas onde quer que apareçam, e as lições de IA usam os modelos e APIs de hoje.
- **Fica com você.** Cada lição entrega uma skill de agente, um prompt ou um servidor MCP em `outputs/`, então você termina com uma caixa de ferramentas, não apenas anotações.

## Trilhas de aprendizado

| Trilha | Para | Lições |
|:--|:--|:--|
| [DevOps Engineer](https://cloudcaptain.io/learn/paths/devops) | de um único processo Linux a Kubernetes, IaC, cloud e AIOps | 47 |
| [AI Platform Engineer](https://cloudcaptain.io/learn/paths/ai-platform) | engenheiros que precisam rodar modelos: GPUs, serving, gateways, roteamento, custo | 17 |
| [AIOps Engineer](https://cloudcaptain.io/learn/paths/aiops) | colocar agentes para trabalhar na infraestrutura com segurança | 9 |

## O tutor no seu agente

`npx skills add nomadicmehul/CloudCaptain` instala quatro skills: `captain-start` (quiz de nivelamento e um plano em `LEARNING.md`), `captain-learn` (uma lição por sessão, Build It → Use It → Ship It, com o quiz e os testes), `captain-guide` (onde aprendo X, do que o cargo Y precisa) e `captain-check` (uma revisão socrática de uma fase). Elas funcionam no Claude Code, Cursor, Codex e em qualquer agente que leia `SKILL.md`.

## A biblioteca de referência

Junto com o currículo, [cloudcaptain.io/docs](https://cloudcaptain.io/docs) mantém os guias detalhados com que o CloudCaptain começou: 150+ páginas de texto original, 21 cheat sheets de comandos, 19 conjuntos de perguntas de entrevista, 5 guias de preparação para exames de certificação (CKA/CKAD/CKS, Terraform, AWS, Azure, GCP), 7 trilhas de aprendizado e 8 trilhas de carreira. As lições apontam para ela em vez de repeti-la, e as páginas de referência em que uma lição se baseia apontam de volta com um card "Build this from scratch".

O modo de leitura **Captain's Bridge** do site acompanha o progresso por seção, e o [/progress](https://cloudcaptain.io/progress) guarda as lições, as notas dos quizzes e o que você leu no seu navegador. Sem conta, sem rastreamento.

<div align="center">
  <img src="../website/static/img/captains-bridge-demo.gif" width="760" alt="Captain's Bridge: as seções vão sendo marcadas conforme você lê, e o /progress lembra o que você já leu" />
</div>

## Por que o CloudCaptain

| | CloudCaptain | roadmap.sh | devops-exercises | listas awesome-* |
|:--|:--|:--|:--|:--|
| Construa cada ferramenta do zero, com testes | ✅ 55 lições | ❌ | ❌ | ❌ |
| Verificado contra as ferramentas reais em CI | ✅ todas as lições | ❌ | ❌ | ❌ |
| Infraestrutura de IA e AIOps | ✅ 15 lições | parcial | ❌ | links |
| Um tutor em forma de agente que você instala | ✅ `npx skills add` | ❌ | ❌ | ❌ |
| Guias detalhados, cheat sheets, perguntas e respostas de entrevista, preparação para exames | ✅ | os nós do roadmap apontam para fora | perguntas e respostas | links |
| Progresso de leitura e dos quizzes | ✅ no seu navegador | ✅ | ❌ | ❌ |

## Rode localmente

```bash
git clone https://github.com/nomadicmehul/CloudCaptain.git
cd CloudCaptain/website
npm install
npm start                                  # http://localhost:3000, lessons at /learn
npm run curriculum:test -- linux/pid1-supervisor   # one lesson's tests
```

Cada lição é uma pasta: `curriculum/phases/NN-phase/NN-lesson/` com `docs/en.md`, `code/` (com testes e `smoke.sh`), `quiz.json` e `outputs/`. O contrato é o [LESSON_TEMPLATE.md](../LESSON_TEMPLATE.md); o mapa do repositório é o [AGENTS.md](../AGENTS.md).

## Como contribuir

Lições, correções e traduções são todas bem-vindas; veja o [CONTRIBUTING.md](../CONTRIBUTING.md). Uma nova lição começa com `npm run curriculum:scaffold`, precisa passar no validador e nos próprios testes, e é verificada pelo smoke run no seu pull request. Conteúdo desatualizado tem seu próprio template de issue, e todo pull request ganha um deploy de preview.

Vai ensinar com o CloudCaptain em um bootcamp, universidade ou empresa? O [FORKING.md](../FORKING.md) explica como fazer fork e adaptá-lo. Para apoiar o trabalho, veja o [SPONSORS.md](../SPONSORS.md).

## Tráfego do site

<div align="center">
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-total.json" alt="Total de visitas" />
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-week.json" alt="Visitas nesta semana" />
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-today.json" alt="Visitas hoje" />

  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../.github/stats/chart-dark.svg">
    <img src="../.github/stats/chart-light.svg" width="800" alt="Tráfego de visitantes nos últimos 90 dias" />
  </picture>
  <p><em>Atualizado diariamente via <a href="https://www.goatcounter.com">GoatCounter</a>: respeita a privacidade, sem cookies, sem rastreamento de indivíduos.</em></p>
</div>

## Licença

MIT, veja o [LICENSE](../LICENSE). Use, faça fork, ensine com ele.

---

<div align="center">
  <p>Feito por <a href="https://nomadicmehul.dev/">Mehul Patel</a> e pela comunidade open source · <a href="https://cloudcaptain.io">cloudcaptain.io</a></p>
  <p>Se o CloudCaptain ajudou você, <a href="https://github.com/nomadicmehul/CloudCaptain">dê uma estrela no repositório</a>: isso ajuda outros engenheiros a encontrar uma alternativa gratuita aos cursos pagos.</p>
</div>
