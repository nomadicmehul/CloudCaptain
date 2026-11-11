<div align="center">
  <img src="../website/static/img/cloudcaptain-logo.jpg" width="180" alt="CloudCaptain" />
  <h1>CloudCaptain</h1>
  <h3>从零开始掌握 Cloud、DevOps 与 AIOps。<br/>亲手构建工具，上手使用工具，交付一件产物。</h3>
  <p>
    <strong>104 节课</strong> · <strong>15 个阶段已上线</strong> · <strong>151 小时</strong> · <strong>3,077 个测试</strong> · <strong>205 个技能、提示词与 MCP 服务器</strong>
  </p>
  <p>
    <a href="https://cloudcaptain.io/learn"><img src="https://img.shields.io/badge/Start_the_course-cloudcaptain.io%2Flearn-1E9BD7?style=for-the-badge" alt="开始学习课程" /></a>
  </p>
  <p>
    <a href="https://github.com/nomadicmehul/CloudCaptain/actions/workflows/lesson-smoke.yml"><img src="https://github.com/nomadicmehul/CloudCaptain/actions/workflows/lesson-smoke.yml/badge.svg" alt="课程冒烟测试" /></a>
    <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT 许可证" /></a>
    <img src="https://img.shields.io/badge/PRs-welcome-blue" alt="欢迎提交 PR" />
    <a href="https://github.com/nomadicmehul/CloudCaptain/stargazers"><img src="https://img.shields.io/github/stars/nomadicmehul/cloudcaptain?style=flat&logo=github" alt="GitHub Star 数" /></a>
  </p>
  <p>
    <a href="../README.md">English</a> ·
    <a href="./README.hi.md">हिन्दी</a> ·
    <a href="./README.mr.md">मराठी</a> ·
    <a href="./README.gu.md">ગુજરાતી</a> ·
    <a href="./README.de.md">Deutsch</a> ·
    <a href="./README.es.md">Español</a> ·
    <a href="./README.pt.md">Português</a> ·
    <strong>中文</strong> ·
    <a href="./README.ja.md">日本語</a>
  </p>
  <p><em>本文为翻译版本，内容以<a href="../README.md">英文 README</a> 为准；如发现错误，欢迎通过 Pull Request 提交修正。</em></p>
</div>

```bash
npx skills add nomadicmehul/CloudCaptain   # installs the tutor into Claude Code, Cursor, Codex or any agent that reads SKILL.md
```

然后让你的智能体运行 `/captain-start`：一个简短的分级测验、一条学习路径，以及第一节课。

<div align="center">
  <img src="../website/static/img/tutor-demo.gif" width="760" alt="The CloudCaptain tutor: npx skills add, then /captain-start asks your goal, level and time and starts the placement quiz" />
</div>

---

## 你将构建什么

每节课都从一个生产环境中的真实问题出发：先让你用纯 Python 或 Bash 亲手实现其底层机制并编写测试，再用真实工具完成同样的工作。这样，每个命令行参数、每条报错信息都能对应到你亲手写过的代码。

| 阶段 | 亲手构建 | 再上手运行 |
|:--|:--|:--|
| **00 · 环境准备与实验环境** | 一个实验环境诊断工具（lab doctor），探测你的机器并输出安装计划 | kind、Docker、你的编程智能体 |
| **01 · Linux 与系统** | PID 1 进程管理器、fd 泄漏检测器、strace 分析器、capability 解码器、systemd 单元、手动操作 cgroups、挂载表读取器、USE 方法仪表盘 | `strace`、`capsh`、`systemctl`、`findmnt`、PSI |
| **02 · 网络** | 类似 `ip route get` 的路由查找、迭代式 DNS 解析器、TCP 套接字状态分析器、支持 tcpdump 过滤语法的 pcap 读取器、TLS ClientHello 解析器与证书链验证器、HTTP/1.1 服务器与反向代理、带健康检查的负载均衡器、遵循 RFC 9111 的缓存代理 | `ip`、`dig`、`ss`、tcpdump、`openssl`、curl、nginx、HAProxy、CoreDNS |
| **03 · Git 与协作** | `git fsck` 认可的内容寻址对象存储、diff 与三方合并引擎、基于 packfile 和 smart HTTP 的克隆客户端、带 CODEOWNERS 与规则集的合并队列、只检查暂存内容的钩子与 `pre-receive` 策略、基于提交图的 bisect 与 blame | `git` 底层命令、`git merge-file`、`GIT_TRACE_PACKET`、规则集、合并队列、pre-commit、`git bisect run` |
| **04 · 脚本编程与自动化** | 带严格模式检查的 Bash 静态检查器、CLI 框架、支持重试与限流的 API 客户端、jq 与 awk 子集、cron 与 OnCalendar 调度器、可复现的 wheel 构建器 | ShellCheck、curl、urllib3、jq、awk、croniter、`systemd-analyze`、pip 与 `build` |
| **05 · 容器** | 容器运行时、镜像审计器、镜像仓库客户端、不用 Dockerfile 的镜像构建器、veth/网桥网络、Compose、rootless 用户命名空间、seccomp 配置生成器 | Docker、Podman、OCI 镜像仓库 |
| **06 · 编排** | 调谐器（reconciler）、调度器、kubelet、kube-proxy 规则与集群 DNS、RBAC 鉴权器、Helm、Operator、Gateway API 路由器 | 运行在 kind 上的 Kubernetes、Helm、Envoy Gateway |
| **07 · 基础设施即代码** | plan/apply 引擎、依赖图、模块、带锁的状态服务器、漂移检测、策略引擎、Pulumi 与 Crossplane 前端、幂等的配置管理器 | OpenTofu、conftest/OPA、Pulumi、Ansible |
| **08 · CI/CD 与 GitOps** | 工作流运行器、工作流安全审计器、OIDC 签发器与验证器、SBOM 生成器、带透明日志的签名器、GitOps 调谐器、金丝雀发布控制器、发布工具 | GitHub Actions 与 act、zizmor、cosign、syft、Argo CD、Argo Rollouts、git-cliff |
| **09 · 云（AWS 优先）** | IAM 策略评估器、VPC 可达性分析器、SigV4 签名与 S3 服务器、Lambda 运行时 API、Auto Scaling 组、FOCUS 成本分摊器、安全态势扫描器 | 对接 moto 的 AWS CLI、Lambda 模拟器、Checkov |
| **10 · 可观测性与 SRE** | 采用 Gorilla 压缩块的时序数据库、PromQL 引擎、告警路由器、支持 LogQL 子集的日志管道、支持尾部采样的追踪器、SLO 燃烧率规则、事件管理服务、混沌实验 | Prometheus、promtool、OpenTelemetry Collector、Jaeger、Sloth、Alertmanager、Loki、Grafana Alloy、Chaos Mesh |
| **11 · 安全与 DevSecOps** | 带租约和 Shamir 解封的密钥服务、支持 CEL 子集的准入 Webhook、Falco 风格的规则引擎、NetworkPolicy 评估器、密钥扫描器、基于 mTLS 的 SPIFFE 身份、基于 CVSS、EPSS 与 VEX 的 CVE 优先级排序器、CIS 基准检查器 | OpenBao、ValidatingAdmissionPolicy、Kyverno、Falco、gitleaks、SPIRE、Trivy、Grype、kube-bench |
| **12 · 平台工程** | 支持安全更新的服务模板生成器、记录负责人与依赖关系的服务目录、基于 Git、部署与事件的 DORA 指标、按 PR 创建的预览环境、把一份规格展开为 Kubernetes 对象的平台 API | copier、Backstage 目录模型、DuckDB、`kubectl auth can-i`、score-k8s |
| **13 · AI 基础设施** | 支持 DRA 的 GPU 调度器、LLM 推理服务模拟器、按 token 计量的网关、基于队列的自动扩缩器、前缀缓存路由器、HNSW 与 IVF 索引、模型制品检查器、token 成本模型 | 在 CPU 上运行的 llama.cpp、LiteLLM、pgvector、llm-d、ORAS |
| **14 · AIOps 与智能体运维** | 面向 kubectl 的 MCP 服务器、故障分诊智能体、AI 日志分析、面向 LLM 调用的 OpenTelemetry、智能体沙箱、安全的写操作工具、智能体评测 | Claude Code、Claude API、OpenTelemetry、promptfoo |

另外 2 个阶段（MLOps、综合实战项目）已列入[路线图](../ROADMAP.md)。

## 一节课如何进行

```
The Problem  →  The Concept  →  Build It  →  Use It  →  Ship It  →  Quiz
 a real          one diagram     standard     the real    a skill,    six questions,
 incident                        library +    tool, same  prompt or   before and
                                 tests        job         MCP server  after
```

- **可运行、有测试**。每节课都配有带单元测试的代码，可在任何环境运行（无需 root、无需 Docker、无需云账号），另有一个 `smoke.sh`，在装有 Docker、kind 和 OpenTofu 的 CI runner 上执行仅限 Linux 的步骤并运行真实工具。只有通过之后，课程才会显示 “last verified”（最近验证时间）。
- **紧跟当下**。工具版本每周跟踪更新；已退役的技术（ingress-nginx、PodSecurityPolicy、Swarm、Bitnami 镜像）无论出现在哪里都会被标注；AI 课程使用当下的模型和 API。
- **成果归你所有**。每节课都会向 `outputs/` 交付一个智能体技能、一个提示词或一个 MCP 服务器，学完之后你收获的是一套工具箱，而不只是笔记。

## 学习路径

| 路径 | 适合人群 | 课程数 |
|:--|:--|:--|
| [DevOps 工程师](https://cloudcaptain.io/learn/paths/devops) | 从单个 Linux 进程一路学到 Kubernetes、IaC、云与 AIOps | 96 |
| [AI 平台工程师](https://cloudcaptain.io/learn/paths/ai-platform) | 需要负责运行模型的工程师：GPU、推理服务、网关、路由、成本 | 23 |
| [AIOps 工程师](https://cloudcaptain.io/learn/paths/aiops) | 让智能体安全地在基础设施上干活 | 9 |

## 装进智能体里的导师

`npx skills add nomadicmehul/CloudCaptain` 会安装四个技能：`captain-start`（分级测验，并在 `LEARNING.md` 中生成学习计划）、`captain-learn`（每次会话一节课，按 Build It → Use It → Ship It 进行，含测验和测试）、`captain-guide`（X 该去哪里学、Y 岗位需要什么）以及 `captain-check`（对一个阶段进行苏格拉底式复盘）。它们可以在 Claude Code、Cursor、Codex 以及任何读取 `SKILL.md` 的智能体中使用。

## 参考资料库

在课程体系之外，[cloudcaptain.io/docs](https://cloudcaptain.io/docs) 保留了 CloudCaptain 起步时积累的长篇指南：150+ 页原创内容、21 份命令速查表、19 套面试题、5 份认证备考指南（CKA/CKAD/CKS、Terraform、AWS、Azure、GCP）、7 条学习路径和 8 条职业路径。课程会链接到这些内容，而不是重复它们；课程所依托的参考页面也会通过一张 “Build this from scratch”（从零构建）卡片反向链接到课程。

网站的 **Captain's Bridge** 阅读模式会按章节跟踪进度，[/progress](https://cloudcaptain.io/progress) 会在你的浏览器中记住课程、测验分数和阅读记录。无需账号，没有跟踪。

<div align="center">
  <img src="../website/static/img/captains-bridge-demo.gif" width="760" alt="Captain's Bridge：边读边勾选章节，/progress 会记住你读过的内容" />
</div>

## 为什么选择 CloudCaptain

| | CloudCaptain | roadmap.sh | devops-exercises | awesome-* 清单 |
|:--|:--|:--|:--|:--|
| 从零构建每个工具，并配有测试 | ✅ 104 节课 | ❌ | ❌ | ❌ |
| 在 CI 中用真实工具验证 | ✅ 每节课 | ❌ | ❌ | ❌ |
| AI 基础设施与 AIOps | ✅ 15 节课 | 部分 | ❌ | 链接 |
| 可安装的智能体导师 | ✅ `npx skills add` | ❌ | ❌ | ❌ |
| 长篇指南、速查表、面试问答、认证备考 | ✅ | 路线图节点链接到外部 | 问答 | 链接 |
| 阅读与测验进度 | ✅ 保存在你的浏览器中 | ✅ | ❌ | ❌ |

## 本地运行

```bash
git clone https://github.com/nomadicmehul/CloudCaptain.git
cd CloudCaptain/website
npm install
npm start                                  # http://localhost:3000, lessons at /learn
npm run curriculum:test -- linux/pid1-supervisor   # one lesson's tests
```

每节课都是一个文件夹：`curriculum/phases/NN-phase/NN-lesson/`，其中包含 `docs/en.md`、`code/`（含测试和 `smoke.sh`）、`quiz.json` 和 `outputs/`。课程规范见 [LESSON_TEMPLATE.md](../LESSON_TEMPLATE.md)；仓库结构说明见 [AGENTS.md](../AGENTS.md)。

## 参与贡献

欢迎贡献课程、修复和翻译，详见 [CONTRIBUTING.md](../CONTRIBUTING.md)。新课程从 `npm run curriculum:scaffold` 开始，必须通过校验器和自身的测试，并在其 Pull Request 上由冒烟测试运行完成验证。过时内容有专门的 issue 模板，每个 Pull Request 都会生成一个预览部署。

在训练营、大学或公司里用 CloudCaptain 教学？[FORKING.md](../FORKING.md) 介绍了如何 fork 并改编它。如果想支持这个项目，请参阅 [SPONSORS.md](../SPONSORS.md)。

## 网站访问量

<div align="center">
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-total.json" alt="总访问量" />
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-week.json" alt="本周访问量" />
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-today.json" alt="今日访问量" />

  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../.github/stats/chart-dark.svg">
    <img src="../.github/stats/chart-light.svg" width="800" alt="过去 90 天的访客流量" />
  </picture>
  <p><em>每日通过 <a href="https://www.goatcounter.com">GoatCounter</a> 更新：注重隐私，不使用 cookie，不追踪个人。</em></p>
</div>

## 许可证

MIT，详见 [LICENSE](../LICENSE)。尽管使用、fork，拿它来教学。

---

<div align="center">
  <p>由 <a href="https://nomadicmehul.dev/">Mehul Patel</a> 与开源社区共同打造 · <a href="https://cloudcaptain.io">cloudcaptain.io</a></p>
  <p>如果 CloudCaptain 对你有帮助，请<a href="https://github.com/nomadicmehul/CloudCaptain">给仓库点个 Star</a>：这能帮助更多工程师找到一个可以替代付费课程的免费选择。</p>
</div>
