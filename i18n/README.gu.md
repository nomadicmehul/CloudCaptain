<div align="center">
  <img src="../website/static/img/cloudcaptain-logo.jpg" width="180" alt="CloudCaptain" />
  <h1>CloudCaptain</h1>
  <h3>Cloud, DevOps &amp; AIOps, શૂન્યથી.<br/>Tool જાતે બનાવો. Tool વાપરો. Artifact ship કરો.</h3>
  <p>
    <strong>63 lessons</strong> · <strong>9 phases લાઇવ</strong> · <strong>92 કલાક</strong> · <strong>1,725 tests</strong> · <strong>123 skills, prompts અને MCP servers</strong>
  </p>
  <p>
    <a href="https://cloudcaptain.io/learn"><img src="https://img.shields.io/badge/Start_the_course-cloudcaptain.io%2Flearn-1E9BD7?style=for-the-badge" alt="કોર્સ શરૂ કરો" /></a>
  </p>
  <p>
    <a href="https://github.com/nomadicmehul/CloudCaptain/actions/workflows/lesson-smoke.yml"><img src="https://github.com/nomadicmehul/CloudCaptain/actions/workflows/lesson-smoke.yml/badge.svg" alt="Lesson smoke tests" /></a>
    <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT લાઇસન્સ" /></a>
    <img src="https://img.shields.io/badge/PRs-welcome-blue" alt="PRs આવકાર્ય છે" />
    <a href="https://github.com/nomadicmehul/CloudCaptain/stargazers"><img src="https://img.shields.io/github/stars/nomadicmehul/cloudcaptain?style=flat&logo=github" alt="GitHub stars" /></a>
  </p>
  <p>
    <a href="../README.md">English</a> ·
    <a href="./README.hi.md">हिन्दी</a> ·
    <a href="./README.mr.md">मराठी</a> ·
    <strong>ગુજરાતી</strong> ·
    <a href="./README.de.md">Deutsch</a> ·
    <a href="./README.es.md">Español</a> ·
    <a href="./README.pt.md">Português</a> ·
    <a href="./README.zh.md">中文</a> ·
    <a href="./README.ja.md">日本語</a>
  </p>
  <p><sub>આ એક અનુવાદ છે; અધિકૃત સ્રોત <a href="../README.md">English README</a> જ છે. સુધારા pull request દ્વારા આવકાર્ય છે.</sub></p>
</div>

```bash
npx skills add nomadicmehul/CloudCaptain   # installs the tutor into Claude Code, Cursor, Codex or any agent that reads SKILL.md
```

પછી તમારા agent ને `/captain-start` કહો: એક ટૂંકી placement quiz, એક learning path અને પહેલો lesson.

<div align="center">
  <img src="../website/static/img/tutor-demo.gif" width="760" alt="The CloudCaptain tutor: npx skills add, then /captain-start asks your goal, level and time and starts the placement quiz" />
</div>

---

## તમે શું બનાવશો

દરેક lesson એક production સમસ્યાથી શરૂ થાય છે, તમારી પાસે એ mechanism સાદા Python કે Bash માં tests સાથે જાતે બનાવડાવે છે, અને પછી એ જ કામ અસલી tool થી કરાવે છે, જેથી દરેક flag અને દરેક error message તમે લખેલા code સાથે સીધો જોડાય.

| Phase | તમે બનાવો છો | પછી તમે ચલાવો છો |
|:--|:--|:--|
| **00 · Setup & Lab** | એક lab doctor જે તમારા machine ને તપાસે છે અને install plan છાપે છે | kind, Docker, તમારો coding agent |
| **01 · Linux & Systems** | એક PID 1 supervisor, એક fd leak detector, એક strace analyser, એક capability decoder, systemd units, હાથે બનાવેલા cgroups, એક mount table reader, એક USE-method dashboard | `strace`, `capsh`, `systemctl`, `findmnt`, PSI |
| **05 · Containers** | એક container runtime, એક image auditor, એક registry client, Dockerfile વગરનો image builder, એક veth/bridge network, Compose, rootless user namespaces, એક seccomp generator | Docker, Podman, OCI registries |
| **06 · Orchestration** | એક reconciler, એક scheduler, એક kubelet, kube-proxy rules અને cluster DNS, એક RBAC authorizer, Helm, એક operator, એક Gateway API router | kind પર Kubernetes, Helm, Envoy Gateway |
| **07 · Infrastructure as Code** | એક plan/apply engine, એક dependency graph, modules, locking વાળો state server, drift detection, એક policy engine, Pulumi અને Crossplane front ends, એક idempotent config manager | OpenTofu, conftest/OPA, Pulumi, Ansible |
| **08 · CI/CD & GitOps** | એક workflow runner, એક workflow security auditor, એક OIDC issuer અને verifier, એક SBOM generator, transparency log વાળો એક signer, એક GitOps reconciler, એક canary controller, એક release tool | GitHub Actions અને act, zizmor, cosign, syft, Argo CD, Argo Rollouts, git-cliff |
| **09 · Cloud (AWS પહેલા)** | એક IAM policy evaluator, એક VPC reachability analyser, SigV4 signing અને એક S3 server, Lambda runtime API, એક Auto Scaling group, એક FOCUS cost allocator, એક security posture scanner | moto સામે AWS CLI, Lambda emulator, Checkov |
| **13 · AI Infrastructure** | DRA સાથેનો GPU scheduler, એક LLM serving simulator, tokens ગણતો gateway, queue-આધારિત autoscaler, એક prefix-cache router, HNSW અને IVF indexes, એક model artifact inspector, એક token cost model | CPU પર llama.cpp, LiteLLM, pgvector, llm-d, ORAS |
| **14 · AIOps & Agentic Ops** | kubectl માટે એક MCP server, એક incident-triage agent, AI log analysis, LLM calls માટે OpenTelemetry, agent sandboxes, સુરક્ષિત write tools, agent evals | Claude Code, Claude API, OpenTelemetry, promptfoo |

બીજા આઠ phases (networking, Git, scripting, observability, security, platform engineering, MLOps, capstones) [roadmap](../ROADMAP.md) પર છે.

## Lesson કેવી રીતે ચાલે છે

```
The Problem  →  The Concept  →  Build It  →  Use It  →  Ship It  →  Quiz
 a real          one diagram     standard     the real    a skill,    six questions,
 incident                        library +    tool, same  prompt or   before and
                                 tests        job         MCP server  after
```

- **ચલાવી શકાય તેવું અને tested.** દરેક lesson માં unit tests સાથેનો code છે જે ગમે ત્યાં ચાલે છે (root, Docker કે cloud account ની જરૂર નથી), અને એક `smoke.sh` છે જે Linux-only steps અને અસલી tools ને Docker, kind અને OpenTofu વાળા CI runner પર ચલાવે છે. એ pass થાય પછી જ lesson પર "last verified" દેખાય છે.
- **અપ-ટુ-ડેટ.** Tool versions દર અઠવાડિયે track થાય છે, retired technology (ingress-nginx, PodSecurityPolicy, Swarm, Bitnami images) જ્યાં પણ દેખાય ત્યાં flag કરવામાં આવે છે, અને AI lessons આજના models અને APIs વાપરે છે.
- **કાયમ તમારું.** દરેક lesson `outputs/` માં એક agent skill, એક prompt અથવા એક MCP server આપે છે, એટલે અંતે તમારી પાસે ફક્ત notes નહીં, પણ આખું toolbox હોય છે.

## Learning paths

| Path | કોના માટે | Lessons |
|:--|:--|:--|
| [DevOps Engineer](https://cloudcaptain.io/learn/paths/devops) | એક Linux process થી લઈને Kubernetes, IaC, cloud અને AIOps સુધી | 55 |
| [AI Platform Engineer](https://cloudcaptain.io/learn/paths/ai-platform) | જે engineers ને models ચલાવવાનું કામ સોંપાયું છે: GPUs, serving, gateways, routing, cost | 23 |
| [AIOps Engineer](https://cloudcaptain.io/learn/paths/aiops) | infrastructure પર agents ને સુરક્ષિત રીતે કામે લગાડવા | 9 |

## તમારા agent માં tutor

`npx skills add nomadicmehul/CloudCaptain` ચાર skills install કરે છે: `captain-start` (placement quiz અને `LEARNING.md` માં તમારો plan), `captain-learn` (દરેક session માં એક lesson, Build It → Use It → Ship It, quiz અને tests સાથે), `captain-guide` (X ક્યાં શીખવું, role Y માટે શું જોઈએ) અને `captain-check` (એક phase નું Socratic review). આ skills Claude Code, Cursor, Codex અને `SKILL.md` વાંચતા કોઈ પણ agent માં કામ કરે છે.

## Reference library

Curriculum ની સાથે, [cloudcaptain.io/docs](https://cloudcaptain.io/docs) પર એ long-form guides છે જેનાથી CloudCaptain ની શરૂઆત થઈ હતી: 150+ pages નું મૌલિક લખાણ, 21 command cheat sheets, 19 interview question sets, 5 certification exam-prep guides (CKA/CKAD/CKS, Terraform, AWS, Azure, GCP), 7 learning paths અને 8 career paths. Lessons એ સામગ્રી ફરીથી લખવાને બદલે તેની link આપે છે, અને જે reference pages પર કોઈ lesson આધારિત છે તે "Build this from scratch" card દ્વારા પાછી એ lesson ની link આપે છે.

Site નો **Captain's Bridge** reading mode દરેક section ની progress track કરે છે, અને [/progress](https://cloudcaptain.io/progress) તમારા browser માં જ lessons, quiz scores અને વાંચન યાદ રાખે છે. કોઈ account નહીં, કોઈ tracking નહીં.

<div align="center">
  <img src="../website/static/img/captains-bridge-demo.gif" width="760" alt="Captain's Bridge: તમે વાંચો તેમ sections પર tick થતું જાય છે, અને /progress યાદ રાખે છે કે તમે શું વાંચ્યું છે" />
</div>

## CloudCaptain શા માટે

| | CloudCaptain | roadmap.sh | devops-exercises | awesome-* lists |
|:--|:--|:--|:--|:--|
| દરેક tool શૂન્યથી, tests સાથે બનાવો | ✅ 63 lessons | ❌ | ❌ | ❌ |
| CI માં અસલી tools સામે verified | ✅ દરેક lesson | ❌ | ❌ | ❌ |
| AI infrastructure અને AIOps | ✅ 15 lessons | આંશિક | ❌ | links |
| Install કરી શકાય તેવો agent tutor | ✅ `npx skills add` | ❌ | ❌ | ❌ |
| Long-form guides, cheat sheets, interview Q&A, exam prep | ✅ | roadmap nodes બહારની links આપે છે | Q&A | links |
| વાંચન અને quiz progress | ✅ તમારા browser માં | ✅ | ❌ | ❌ |

## Local machine પર ચલાવો

```bash
git clone https://github.com/nomadicmehul/CloudCaptain.git
cd CloudCaptain/website
npm install
npm start                                  # http://localhost:3000, lessons at /learn
npm run curriculum:test -- linux/pid1-supervisor   # one lesson's tests
```

દરેક lesson એક folder છે: `curriculum/phases/NN-phase/NN-lesson/`, જેમાં `docs/en.md`, `code/` (tests અને `smoke.sh` સાથે), `quiz.json` અને `outputs/` હોય છે. Contract [LESSON_TEMPLATE.md](../LESSON_TEMPLATE.md) માં છે; repo map [AGENTS.md](../AGENTS.md) માં છે.

## યોગદાન

Lessons, fixes અને translations, બધું જ આવકાર્ય છે; જુઓ [CONTRIBUTING.md](../CONTRIBUTING.md). નવો lesson `npm run curriculum:scaffold` થી શરૂ થાય છે, તેણે validator અને પોતાના tests pass કરવા જરૂરી છે, અને તેના pull request પર smoke run દ્વારા તે verify થાય છે. જૂની પડી ગયેલી સામગ્રી માટે અલગ issue template છે, અને દરેક pull request ને preview deployment મળે છે.

Bootcamp, university કે company માં CloudCaptain થી ભણાવો છો? [FORKING.md](../FORKING.md) સમજાવે છે કે તેને fork કરીને તમારી જરૂરિયાત મુજબ કેવી રીતે ઢાળવું. આ કામને support કરવા માટે [SPONSORS.md](../SPONSORS.md) જુઓ.

## Site traffic

<div align="center">
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-total.json" alt="કુલ visits" />
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-week.json" alt="આ અઠવાડિયાની visits" />
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-today.json" alt="આજની visits" />

  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../.github/stats/chart-dark.svg">
    <img src="../.github/stats/chart-light.svg" width="800" alt="છેલ્લા 90 દિવસનો visitor traffic" />
  </picture>
  <p><em><a href="https://www.goatcounter.com">GoatCounter</a> દ્વારા દરરોજ update થાય છે: privacy-friendly, cookies નહીં, કોઈ વ્યક્તિનું tracking નહીં.</em></p>
</div>

## License (લાઇસન્સ)

MIT, જુઓ [LICENSE](../LICENSE). વાપરો, fork કરો, તેનાથી ભણાવો.

---

<div align="center">
  <p><a href="https://nomadicmehul.dev/">Mehul Patel</a> અને open-source community દ્વારા બનાવેલું · <a href="https://cloudcaptain.io">cloudcaptain.io</a></p>
  <p>જો CloudCaptain તમને ઉપયોગી થયું હોય, તો <a href="https://github.com/nomadicmehul/CloudCaptain">repo ને star આપો</a>: તેનાથી બીજા engineers ને paid courses નો મફત વિકલ્પ શોધવામાં મદદ મળે છે.</p>
</div>
