<div align="center">
  <img src="../website/static/img/cloudcaptain-logo.jpg" width="180" alt="CloudCaptain" />
  <h1>CloudCaptain</h1>
  <h3>Cloud、DevOps、AIOps をゼロから。<br/>ツールを作る。ツールを使う。成果物をシップする。</h3>
  <p>
    <strong>55 レッスン</strong> · <strong>8 フェーズ公開中</strong> · <strong>80 時間</strong> · <strong>1,429 テスト</strong> · <strong>スキル・プロンプト・MCP サーバー 107 個</strong>
  </p>
  <p>
    <a href="https://cloudcaptain.io/learn"><img src="https://img.shields.io/badge/Start_the_course-cloudcaptain.io%2Flearn-1E9BD7?style=for-the-badge" alt="コースを始める" /></a>
  </p>
  <p>
    <a href="https://github.com/nomadicmehul/CloudCaptain/actions/workflows/lesson-smoke.yml"><img src="https://github.com/nomadicmehul/CloudCaptain/actions/workflows/lesson-smoke.yml/badge.svg" alt="レッスンのスモークテスト" /></a>
    <a href="../LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="MIT ライセンス" /></a>
    <img src="https://img.shields.io/badge/PRs-welcome-blue" alt="PR 歓迎" />
    <a href="https://github.com/nomadicmehul/CloudCaptain/stargazers"><img src="https://img.shields.io/github/stars/nomadicmehul/cloudcaptain?style=flat&logo=github" alt="GitHub スター" /></a>
  </p>
  <p>
    <a href="../README.md">English</a> ·
    <a href="./README.hi.md">हिन्दी</a> ·
    <a href="./README.mr.md">मराठी</a> ·
    <a href="./README.gu.md">ગુજરાતી</a> ·
    <a href="./README.de.md">Deutsch</a> ·
    <a href="./README.es.md">Español</a> ·
    <a href="./README.pt.md">Português</a> ·
    <a href="./README.zh.md">中文</a> ·
    <strong>日本語</strong>
  </p>
  <p><sub>これは翻訳版です。内容に相違がある場合は<a href="../README.md">英語版 README</a> が正となります。翻訳の修正はプルリクエストで歓迎します。</sub></p>
</div>

```bash
npx skills add nomadicmehul/CloudCaptain   # installs the tutor into Claude Code, Cursor, Codex or any agent that reads SKILL.md
```

あとはエージェントに `/captain-start` と頼むだけです。短いレベル判定クイズ、学習パスの選択、そして最初のレッスンへと進みます。

<div align="center">
  <img src="../website/static/img/tutor-demo.gif" width="760" alt="The CloudCaptain tutor: npx skills add, then /captain-start asks your goal, level and time and starts the placement quiz" />
</div>

---

## 作るもの

どのレッスンも本番環境で起きる問題から始まります。まずその仕組みを素の Python か Bash でテスト付きで自作し、次に同じ作業を本物のツールで行います。そのため、あらゆるフラグやエラーメッセージが自分の書いたコードと結びつきます。

| フェーズ | 自分で作るもの | その後に動かすもの |
|:--|:--|:--|
| **00 · セットアップとラボ** | マシンを調べてインストール計画を表示するラボ診断ツール | kind、Docker、お使いのコーディングエージェント |
| **01 · Linux とシステム** | PID 1 スーパーバイザー、fd リーク検出器、strace アナライザー、ケーパビリティのデコーダー、systemd ユニット、手作業での cgroups、マウントテーブルリーダー、USE メソッドのダッシュボード | `strace`、`capsh`、`systemctl`、`findmnt`、PSI |
| **05 · コンテナ** | コンテナランタイム、イメージ監査ツール、レジストリクライアント、Dockerfile を使わないイメージビルダー、veth/bridge ネットワーク、Compose、rootless のユーザー名前空間、seccomp ジェネレーター | Docker、Podman、OCI レジストリ |
| **06 · オーケストレーション** | リコンサイラー、スケジューラ、kubelet、kube-proxy のルールとクラスタ DNS、RBAC オーソライザー、Helm、オペレーター、Gateway API ルーター | kind 上の Kubernetes、Helm、Envoy Gateway |
| **07 · Infrastructure as Code** | plan/apply エンジン、依存関係グラフ、モジュール、ロック機能付きのステートサーバー、ドリフト検出、ポリシーエンジン、Pulumi と Crossplane のフロントエンド、冪等な構成管理ツール | OpenTofu、conftest/OPA、Pulumi、Ansible |
| **09 · クラウド（AWS から）** | IAM ポリシー評価器、VPC 到達性アナライザー、SigV4 署名と S3 サーバー、Lambda ランタイム API、Auto Scaling グループ、FOCUS コスト配分ツール、セキュリティポスチャスキャナー | moto を相手にした AWS CLI、Lambda エミュレーター、Checkov |
| **13 · AI インフラストラクチャ** | DRA を使った GPU スケジューラ、LLM サービングシミュレーター、トークンを数えるゲートウェイ、キューベースのオートスケーラー、プレフィックスキャッシュルーター、HNSW と IVF のインデックス、モデルアーティファクト検査ツール、トークンコストモデル | CPU 上の llama.cpp、LiteLLM、pgvector、llm-d、ORAS |
| **14 · AIOps とエージェント型運用** | kubectl 用の MCP サーバー、インシデントトリアージエージェント、AI によるログ分析、LLM 呼び出しの OpenTelemetry 計装、エージェントのサンドボックス、安全な書き込みツール、エージェントの評価（evals） | Claude Code、Claude API、OpenTelemetry、promptfoo |

さらに 9 つのフェーズ（ネットワーク、Git、スクリプティング、CI/CD、オブザーバビリティ、セキュリティ、プラットフォームエンジニアリング、MLOps、キャップストーン）が[ロードマップ](../ROADMAP.md)に載っています。

## レッスンの進め方

```
The Problem  →  The Concept  →  Build It  →  Use It  →  Ship It  →  Quiz
 a real          one diagram     standard     the real    a skill,    six questions,
 incident                        library +    tool, same  prompt or   before and
                                 tests        job         MCP server  after
```

- **動かせて、テスト済み。** 各レッスンには、どこでも実行できる（root 権限も Docker もクラウドアカウントも不要）ユニットテスト付きのコードと、Linux 専用の手順と本物のツールを Docker・kind・OpenTofu を備えた CI ランナー上で実行する `smoke.sh` が含まれます。レッスンに「last verified（最終検証日）」が表示されるのは、これが成功した後だけです。
- **常に最新。** ツールのバージョンは毎週追跡され、廃止された技術（ingress-nginx、PodSecurityPolicy、Swarm、Bitnami イメージ）は登場するすべての箇所で明示されます。AI のレッスンでは現行のモデルと API を使います。
- **手元に残る。** 各レッスンはエージェントスキル、プロンプト、または MCP サーバーを `outputs/` に出力します。修了したときに残るのはノートだけではなく、ツールボックスです。

## 学習パス

| パス | 対象 | レッスン数 |
|:--|:--|:--|
| [DevOps エンジニア](https://cloudcaptain.io/learn/paths/devops) | 単一の Linux プロセスから Kubernetes、IaC、クラウド、AIOps まで | 47 |
| [AI プラットフォームエンジニア](https://cloudcaptain.io/learn/paths/ai-platform) | モデルの運用を任されたエンジニア：GPU、サービング、ゲートウェイ、ルーティング、コスト | 23 |
| [AIOps エンジニア](https://cloudcaptain.io/learn/paths/aiops) | エージェントにインフラ作業を安全に任せたい人 | 9 |

## エージェントで使えるチューター

`npx skills add nomadicmehul/CloudCaptain` で 4 つのスキルがインストールされます。`captain-start`（レベル判定クイズと、`LEARNING.md` に書き出す学習計画）、`captain-learn`（1 セッションにつき 1 レッスン。Build It → Use It → Ship It の流れで、クイズとテスト付き）、`captain-guide`（X はどこで学べるか、ロール Y には何が必要か）、`captain-check`（フェーズ単位のソクラテス式レビュー）です。Claude Code、Cursor、Codex のほか、`SKILL.md` を読めるあらゆるエージェントで動作します。

## リファレンスライブラリ

カリキュラムとは別に、[cloudcaptain.io/docs](https://cloudcaptain.io/docs) には CloudCaptain の出発点となった長文ガイドがそろっています。150 ページ以上のオリジナル記事、21 のコマンドチートシート、19 の面接質問集、5 つの認定試験対策ガイド（CKA/CKAD/CKS、Terraform、AWS、Azure、GCP）、7 つの学習パス、8 つのキャリアパスです。レッスンは内容を繰り返さずにこれらへリンクし、レッスンの土台となるリファレンスページからは「Build this from scratch」カードでレッスンへリンクしています。

サイトの **Captain's Bridge** 読書モードはセクションごとに進捗を記録し、[/progress](https://cloudcaptain.io/progress) はレッスン、クイズのスコア、読んだ内容をブラウザに保存します。アカウント不要、トラッキングもありません。

<div align="center">
  <img src="../website/static/img/captains-bridge-demo.gif" width="760" alt="Captain's Bridge：読み進めるとセクションにチェックが付き、/progress が読んだ内容を記憶します" />
</div>

## なぜ CloudCaptain なのか

| | CloudCaptain | roadmap.sh | devops-exercises | awesome-* リスト |
|:--|:--|:--|:--|:--|
| 各ツールをテスト付きでゼロから自作 | ✅ 55 レッスン | ❌ | ❌ | ❌ |
| CI で本物のツールに対して検証 | ✅ 全レッスン | ❌ | ❌ | ❌ |
| AI インフラと AIOps | ✅ 15 レッスン | 一部 | ❌ | リンク |
| インストールできるエージェントチューター | ✅ `npx skills add` | ❌ | ❌ | ❌ |
| 長文ガイド、チートシート、面接 Q&A、試験対策 | ✅ | ロードマップのノードから外部へリンク | Q&A | リンク |
| 読書とクイズの進捗管理 | ✅ ブラウザ内 | ✅ | ❌ | ❌ |

## ローカルで動かす

```bash
git clone https://github.com/nomadicmehul/CloudCaptain.git
cd CloudCaptain/website
npm install
npm start                                  # http://localhost:3000, lessons at /learn
npm run curriculum:test -- linux/pid1-supervisor   # one lesson's tests
```

各レッスンは 1 つのフォルダ `curriculum/phases/NN-phase/NN-lesson/` で、`docs/en.md`、`code/`（テストと `smoke.sh` を含む）、`quiz.json`、`outputs/` から構成されます。レッスンの規約は [LESSON_TEMPLATE.md](../LESSON_TEMPLATE.md)、リポジトリの全体像は [AGENTS.md](../AGENTS.md) にあります。

## コントリビュート

レッスン、修正、翻訳、いずれも歓迎します。詳しくは [CONTRIBUTING.md](../CONTRIBUTING.md) を参照してください。新しいレッスンは `npm run curriculum:scaffold` から始め、バリデーターとそのレッスンのテストに合格する必要があり、プルリクエスト上のスモーク実行で検証されます。古くなった内容の報告には専用の issue テンプレートがあり、すべてのプルリクエストにはプレビューデプロイが作成されます。

ブートキャンプ、大学、企業で CloudCaptain を使って教えていますか？ フォークして自分たち向けに調整する方法は [FORKING.md](../FORKING.md) で説明しています。活動を支援していただける方は [SPONSORS.md](../SPONSORS.md) をご覧ください。

## サイトのトラフィック

<div align="center">
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-total.json" alt="累計訪問数" />
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-week.json" alt="今週の訪問数" />
  <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/nomadicmehul/CloudCaptain/main/.github/stats/visitors-today.json" alt="今日の訪問数" />

  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../.github/stats/chart-dark.svg">
    <img src="../.github/stats/chart-light.svg" width="800" alt="過去 90 日間の訪問者トラフィック" />
  </picture>
  <p><em><a href="https://www.goatcounter.com">GoatCounter</a> により毎日更新：プライバシーに配慮し、Cookie なし、個人の追跡もありません。</em></p>
</div>

## ライセンス

MIT ライセンスです。[LICENSE](../LICENSE) を参照してください。自由に使い、フォークし、教材として使ってください。

---

<div align="center">
  <p><a href="https://nomadicmehul.dev/">Mehul Patel</a> とオープンソースコミュニティが開発 · <a href="https://cloudcaptain.io">cloudcaptain.io</a></p>
  <p>CloudCaptain が役に立ったら、<a href="https://github.com/nomadicmehul/CloudCaptain">リポジトリにスターを付けてください</a>。有料コースに代わる無料の選択肢を、ほかのエンジニアが見つけやすくなります。</p>
</div>
