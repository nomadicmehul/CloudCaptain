<div align="center">
  <img src="../website/static/img/cloudcaptain-logo.jpg" width="180" alt="CloudCaptain" />
  <h1>CloudCaptain</h1>
  <h3>Cloud, DevOps &amp; AIOps, बिल्कुल शुरुआत से।<br/>Tool बनाएँ। Tool इस्तेमाल करें। Artifact ship करें।</h3>
  <p>
    <strong>104 lessons</strong> · <strong>15 phases लाइव</strong> · <strong>151 घंटे</strong> · <strong>3,077 tests</strong> · <strong>205 skills, prompts और MCP servers</strong>
  </p>
  <p>
    <a href="https://cloudcaptain.io/learn"><img src="https://img.shields.io/badge/Start_the_course-cloudcaptain.io%2Flearn-1E9BD7?style=for-the-badge" alt="कोर्स शुरू करें" /></a>
  </p>
  <p>
    <a href="https://github.com/nomadicmehul/CloudCaptain/actions/workflows/lesson-smoke.yml"><img src="https://github.com/nomadicmehul/CloudCaptain/actions/workflows/lesson-smoke.yml/badge.svg" alt="Lesson smoke tests" /></a>
    <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT license" /></a>
    <img src="https://img.shields.io/badge/PRs-welcome-blue" alt="PRs का स्वागत है" />
    <a href="https://github.com/nomadicmehul/CloudCaptain/stargazers"><img src="https://img.shields.io/github/stars/nomadicmehul/cloudcaptain?style=flat&logo=github" alt="GitHub stars" /></a>
  </p>
  <p>
    <a href="../README.md">English</a> ·
    <strong>हिन्दी</strong> ·
    <a href="./README.mr.md">मराठी</a> ·
    <a href="./README.gu.md">ગુજરાતી</a> ·
    <a href="./README.de.md">Deutsch</a> ·
    <a href="./README.es.md">Español</a> ·
    <a href="./README.pt.md">Português</a> ·
    <a href="./README.zh.md">中文</a> ·
    <a href="./README.ja.md">日本語</a>
  </p>
  <p><em>यह एक अनुवाद है; प्रामाणिक स्रोत <a href="../README.md">English README</a> ही है। कोई सुधार हो तो pull request भेजें, उसका स्वागत है।</em></p>
</div>

```bash
npx skills add nomadicmehul/CloudCaptain   # installs the tutor into Claude Code, Cursor, Codex or any agent that reads SKILL.md
```

फिर अपने agent से `/captain-start` चलाने को कहें: एक छोटा placement quiz, एक learning path, और पहला lesson।

<div align="center">
  <img src="../website/static/img/tutor-demo.gif" width="760" alt="The CloudCaptain tutor: npx skills add, then /captain-start asks your goal, level and time and starts the placement quiz" />
</div>

---

## आप क्या बनाएँगे

हर lesson एक production problem से शुरू होता है। पहले आप उस mechanism को सादे Python या Bash में tests के साथ खुद बनाते हैं, फिर वही काम असली tool से करते हैं, ताकि हर flag और हर error message आपके अपने लिखे code से जुड़ जाए।

| Phase | आप बनाते हैं | फिर आप चलाते हैं |
|:--|:--|:--|
| **00 · Setup & Lab** | एक lab doctor जो आपकी machine की जाँच करके install plan print करता है | kind, Docker, आपका coding agent |
| **01 · Linux & Systems** | एक PID 1 supervisor, एक fd leak detector, एक strace analyser, एक capability decoder, systemd units, हाथ से cgroups, एक mount table reader, एक USE-method dashboard | `strace`, `capsh`, `systemctl`, `findmnt`, PSI |
| **02 · Networking** | `ip route get` जैसा एक route lookup, एक iterative DNS resolver, एक TCP socket-state analyser, tcpdump filters वाला एक pcap reader, एक TLS ClientHello parser और certificate-chain verifier, एक HTTP/1.1 server और reverse proxy, health checks वाला एक load balancer, एक RFC 9111 caching proxy | `ip`, `dig`, `ss`, tcpdump, `openssl`, curl, nginx, HAProxy, CoreDNS |
| **03 · Git & Collaboration** | एक content-addressed object store जिसे `git fsck` स्वीकार करता है, एक diff और three-way merge engine, packfile और smart-HTTP वाला एक clone client, CODEOWNERS और rulesets वाली एक merge queue, `pre-receive` policy के साथ staged-only hooks, commit graph पर bisect और blame | `git` plumbing, `git merge-file`, `GIT_TRACE_PACKET`, rulesets, merge queue, pre-commit, `git bisect run` |
| **04 · Scripting & Automation** | strict-mode checks वाला एक Bash linter, एक CLI framework, retries और rate limits वाला एक API client, jq और awk subsets, एक cron और OnCalendar scheduler, एक reproducible wheel builder | ShellCheck, curl, urllib3, jq, awk, croniter, `systemd-analyze`, pip और `build` |
| **05 · Containers** | एक container runtime, एक image auditor, एक registry client, बिना Dockerfile का image builder, एक veth/bridge network, Compose, rootless user namespaces, एक seccomp generator | Docker, Podman, OCI registries |
| **06 · Orchestration** | एक reconciler, एक scheduler, एक kubelet, kube-proxy rules और cluster DNS, एक RBAC authorizer, Helm, एक operator, एक Gateway API router | kind पर Kubernetes, Helm, Envoy Gateway |
| **07 · Infrastructure as Code** | एक plan/apply engine, एक dependency graph, modules, एक locking state server, drift detection, एक policy engine, Pulumi और Crossplane front ends, एक idempotent config manager | OpenTofu, conftest/OPA, Pulumi, Ansible |
| **08 · CI/CD & GitOps** | एक workflow runner, एक workflow security auditor, एक OIDC issuer और verifier, एक SBOM generator, transparency log वाला एक signer, एक GitOps reconciler, एक canary controller, एक release tool | GitHub Actions और act, zizmor, cosign, syft, Argo CD, Argo Rollouts, git-cliff |
| **09 · Cloud (AWS first)** | एक IAM policy evaluator, एक VPC reachability analyser, SigV4 signing और एक S3 server, Lambda runtime API, एक Auto Scaling group, एक FOCUS cost allocator, एक security posture scanner | moto पर AWS CLI, Lambda emulator, Checkov |
| **10 · Observability & SRE** | Gorilla chunks वाला एक TSDB, एक PromQL engine, एक alert router, LogQL subset वाली एक log pipeline, tail sampling वाला एक tracer, SLO burn-rate rules, एक incident service, chaos experiments | Prometheus, promtool, OpenTelemetry Collector, Jaeger, Sloth, Alertmanager, Loki, Grafana Alloy, Chaos Mesh |
| **11 · Security & DevSecOps** | leases और Shamir unsealing वाली एक secrets service, CEL subset वाला एक admission webhook, Falco जैसा एक rule engine, एक NetworkPolicy evaluator, एक secret scanner, mTLS के साथ SPIFFE identities, CVSS, EPSS और VEX वाला एक CVE prioritiser, एक CIS benchmark runner | OpenBao, ValidatingAdmissionPolicy, Kyverno, Falco, gitleaks, SPIRE, Trivy, Grype, kube-bench |
| **12 · Platform Engineering** | safe updates वाला एक service-template scaffolder, owners और dependencies वाला एक service catalog, Git, deploys और incidents से DORA metrics, हर PR के लिए preview environments, एक spec को Kubernetes objects में बदलने वाली एक platform API | copier, Backstage catalog model, DuckDB, `kubectl auth can-i`, score-k8s |
| **13 · AI Infrastructure** | DRA के साथ एक GPU scheduler, एक LLM serving simulator, एक token-counting gateway, एक queue-based autoscaler, एक prefix-cache router, HNSW और IVF indexes, एक model artifact inspector, एक token cost model | CPU पर llama.cpp, LiteLLM, pgvector, llm-d, ORAS |
| **14 · AIOps & Agentic Ops** | kubectl के लिए एक MCP server, एक incident-triage agent, AI से log analysis, LLM calls के लिए OpenTelemetry, agent sandboxes, सुरक्षित write tools, agent evals | Claude Code, Claude API, OpenTelemetry, promptfoo |

दो और phases (MLOps, capstones) [roadmap](../ROADMAP.md) पर हैं।

## एक lesson कैसे चलता है

```
The Problem  →  The Concept  →  Build It  →  Use It  →  Ship It  →  Quiz
 a real          one diagram     standard     the real    a skill,    six questions,
 incident                        library +    tool, same  prompt or   before and
                                 tests        job         MCP server  after
```

- **चलाने लायक और tested।** हर lesson में unit tests वाला code है जो कहीं भी चलता है (न root, न Docker, न cloud account की ज़रूरत), और एक `smoke.sh` है जो Linux-only steps और असली tools को Docker, kind और OpenTofu वाले CI runner पर चलाता है। यह pass होने के बाद ही किसी lesson पर "last verified" दिखता है।
- **हमेशा current।** Tool versions हर हफ़्ते track किए जाते हैं, retire हो चुकी technology (ingress-nginx, PodSecurityPolicy, Swarm, Bitnami images) जहाँ भी आती है वहाँ flag की जाती है, और AI lessons आज के models और APIs इस्तेमाल करते हैं।
- **हमेशा आपके पास।** हर lesson `outputs/` में एक agent skill, prompt या MCP server देता है, ताकि अंत में आपके पास सिर्फ़ notes नहीं, बल्कि एक पूरा toolbox हो।

## सीखने के रास्ते

| Path | किसके लिए | Lessons |
|:--|:--|:--|
| [DevOps Engineer](https://cloudcaptain.io/learn/paths/devops) | एक अकेले Linux process से लेकर Kubernetes, IaC, cloud और AIOps तक | 96 |
| [AI Platform Engineer](https://cloudcaptain.io/learn/paths/ai-platform) | वे engineers जिन्हें models चलाने का काम मिला है: GPUs, serving, gateways, routing, cost | 23 |
| [AIOps Engineer](https://cloudcaptain.io/learn/paths/aiops) | infrastructure पर agents से सुरक्षित तरीके से काम करवाना | 9 |

## आपके agent में tutor

`npx skills add nomadicmehul/CloudCaptain` चार skills install करता है: `captain-start` (placement quiz और `LEARNING.md` में एक plan), `captain-learn` (हर session में एक lesson, Build It → Use It → Ship It, quiz और tests के साथ), `captain-guide` (X कहाँ सीखूँ, role Y के लिए क्या चाहिए) और `captain-check` (किसी phase का Socratic review)। ये Claude Code, Cursor, Codex और `SKILL.md` पढ़ने वाले किसी भी agent में काम करते हैं।

## Reference library

Curriculum के साथ-साथ, [cloudcaptain.io/docs](https://cloudcaptain.io/docs) पर वे long-form guides मौजूद हैं जिनसे CloudCaptain की शुरुआत हुई थी: 150+ पन्नों का original लेखन, 21 command cheat sheets, 19 interview question sets, 5 certification exam-prep guides (CKA/CKAD/CKS, Terraform, AWS, Azure, GCP), 7 learning paths और 8 career paths। Lessons इस सामग्री को दोहराने के बजाय इसके links देते हैं, और जिन reference pages पर कोई lesson आधारित है, वे "Build this from scratch" card के ज़रिए वापस उस lesson से जुड़ते हैं।

Site का **Captain's Bridge** reading mode हर section की progress track करता है, और [/progress](https://cloudcaptain.io/progress) आपके browser में ही lessons, quiz scores और पढ़ाई याद रखता है। न account, न tracking।

<div align="center">
  <img src="../website/static/img/captains-bridge-demo.gif" width="760" alt="Captain's Bridge: पढ़ते-पढ़ते sections पर tick लगता जाता है, और /progress याद रखता है कि आपने क्या पढ़ा है" />
</div>

## CloudCaptain क्यों

| | CloudCaptain | roadmap.sh | devops-exercises | awesome-* lists |
|:--|:--|:--|:--|:--|
| हर tool शुरुआत से, tests के साथ बनाना | ✅ 104 lessons | ❌ | ❌ | ❌ |
| CI में असली tools के सामने verified | ✅ हर lesson | ❌ | ❌ | ❌ |
| AI infrastructure और AIOps | ✅ 15 lessons | आंशिक | ❌ | links |
| Install करने लायक agent tutor | ✅ `npx skills add` | ❌ | ❌ | ❌ |
| Long-form guides, cheat sheets, interview Q&A, exam prep | ✅ | roadmap nodes बाहर link करते हैं | Q&A | links |
| पढ़ाई और quiz की progress | ✅ आपके browser में | ✅ | ❌ | ❌ |

## Local machine पर चलाएँ

```bash
git clone https://github.com/nomadicmehul/CloudCaptain.git
cd CloudCaptain/website
npm install
npm start                                  # http://localhost:3000, lessons at /learn
npm run curriculum:test -- linux/pid1-supervisor   # one lesson's tests
```

हर lesson एक folder है: `curriculum/phases/NN-phase/NN-lesson/`, जिसमें `docs/en.md`, `code/` (tests और `smoke.sh` के साथ), `quiz.json` और `outputs/` होते हैं। Contract [LESSON_TEMPLATE.md](../LESSON_TEMPLATE.md) में है; repo का नक्शा [AGENTS.md](../AGENTS.md) में।

## योगदान

Lessons, fixes और अनुवाद, सबका स्वागत है; [CONTRIBUTING.md](../CONTRIBUTING.md) देखें। नया lesson `npm run curriculum:scaffold` से शुरू होता है, उसे validator और अपने tests pass करने होते हैं, और उसके pull request पर smoke run उसे verify करता है। पुराने पड़ चुके content के लिए अलग issue template है, और हर pull request को एक preview deployment मिलता है।

किसी bootcamp, university या company में CloudCaptain से पढ़ा रहे हैं? [FORKING.md](../FORKING.md) बताता है कि इसे fork करके अपनी ज़रूरत के हिसाब से कैसे ढालें। इस काम को support करने के लिए [SPONSORS.md](../SPONSORS.md) देखें।

## साइट ट्रैफ़िक

<div align="center">
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-total.json" alt="कुल visits" />
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-week.json" alt="इस हफ़्ते के visits" />
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-today.json" alt="आज के visits" />

  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../.github/stats/chart-dark.svg">
    <img src="../.github/stats/chart-light.svg" width="800" alt="पिछले 90 दिनों का visitor traffic" />
  </picture>
  <p><em><a href="https://www.goatcounter.com">GoatCounter</a> के ज़रिए रोज़ update होता है: privacy-friendly, न cookies, न किसी व्यक्ति की tracking।</em></p>
</div>

## लाइसेंस

MIT, देखें [LICENSE](../LICENSE)। इसे इस्तेमाल करें, fork करें, इससे पढ़ाएँ।

---

<div align="center">
  <p><a href="https://nomadicmehul.dev/">Mehul Patel</a> और open-source community द्वारा बनाया गया · <a href="https://cloudcaptain.io">cloudcaptain.io</a></p>
  <p>अगर CloudCaptain से आपको मदद मिली हो, तो <a href="https://github.com/nomadicmehul/CloudCaptain">repo को star करें</a>: इससे दूसरे engineers को paid courses का एक मुफ़्त विकल्प ढूँढने में मदद मिलती है।</p>
</div>
