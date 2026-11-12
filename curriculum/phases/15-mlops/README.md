# MLOps & LLMOps

*Pipelines, registries, evaluation and rollout for models, for engineers who run the platform.*

A model reaches production through the same machinery as any other release, with two differences that break the usual habits: its behaviour comes from data as much as from code, and it can get worse without anyone deploying anything. So the platform has to version datasets the way Git versions code, record every training run with its parameters, metrics and lineage, run pipelines that skip the steps whose inputs did not change, refuse a release when an evaluation regresses, roll a new version out behind shadow and canary traffic judged by quality as well as latency, and notice when the data drifts away from what the model was trained on. This phase builds each of those pieces from the standard library and checks it against the tool teams run: DVC, MLflow, Kubeflow Pipelines and Argo Workflows, promptfoo, Argo Rollouts and Evidently. The models stay tiny and deterministic; the subject is the operations around them, not the training.

## How this phase is built

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| [Data versioning](01-data-versioning/docs/en.md) | Build | `minidvc`: a content-addressed data cache, pointer files in Git, push and pull to S3, garbage collection and a pipeline lock | DVC, MinIO, lakeFS, Git LFS |
| [Experiment tracking](02-experiment-tracking/docs/en.md) | Build | `minitrack`: a tracking server for MLflow's REST API, run search, artifacts and a model registry with aliases | MLflow, the MLflow client, model aliases |
| [ML pipelines](03-ml-pipelines/docs/en.md) | Build | `minipipe`: typed components compiled to a DAG, cached steps, artifact passing and an Argo Workflow emitter | Kubeflow Pipelines v2, Argo Workflows |
| [Eval gates](04-llm-eval-gates/docs/en.md) | Build | `minieval`: eval suites as data, pass@k, Wilson intervals and a McNemar release gate with cost and latency budgets | promptfoo, lm-evaluation-harness |
| [Model rollout](05-model-rollout/docs/en.md) | Build | `minirollout`: shadow traffic, a sticky weighted canary and promotion decided by statistical tests | Argo Rollouts, Gateway API mirroring, KServe |
| [Drift monitoring](06-drift-monitoring/docs/en.md) | Build | `minidrift`: PSI, Kolmogorov-Smirnov, Wasserstein and chi-square drift, data-quality checks and drift gauges for alerting | Evidently, whylogs, Prometheus alerts |

The models and serving side (GPUs, model servers, gateways, quantization) is in [AI Infrastructure](https://cloudcaptain.io/learn/ai-infra); agent evaluations are in [AIOps](https://cloudcaptain.io/learn/aiops).

## Reference layer

The long-form guide for this phase lives under the [AI/ML Ops learning path](https://cloudcaptain.io/docs/learning-paths/ai-ml).
