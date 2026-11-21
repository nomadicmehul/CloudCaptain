---
sidebar_position: 6
title: "AI/ML Ops Learning Path"
description: "Learn ML operations and AI infrastructure — built on DevOps foundations you can learn right here"
---

# AI / ML Ops Learning Path

Where DevOps meets Machine Learning — build, deploy, and operate ML systems at scale. **~10–12 weeks** at 5–7 hours/week, assuming basic programming knowledge.

**By the end you'll be able to:** containerize and serve a model behind an API, build a training pipeline with experiment tracking, run ML workloads on Kubernetes, and reason about LLM serving and inference costs — the skills behind AI/ML Infrastructure Engineer roles (see the [career path](/career-paths)).

:::info Build it from scratch
Follow the matching lesson tracks: [AIOps](/learn/paths/aiops) and [AI Platform](/learn/paths/ai-platform). Each stage below links lessons from the [/learn curriculum](/learn).
:::

## Why MLOps?

As AI becomes core to modern applications, the gap between building ML models and running them in production must be bridged. MLOps applies DevOps principles — automation, versioning, CI/CD, observability — to ML-specific workflows. The good news: **most of the foundation is standard DevOps**, and it's all on CloudCaptain.

## Stage 1: Foundations (2–3 weeks) — learn these here

1. **[Python](/docs/tools/python/fundamentals)** — the language of ML; then [Python for DevOps scripting](/docs/tools/python/devops-scripting)
2. **[Docker](/docs/tools/docker/fundamentals)** — every model ships in a container; master the [Dockerfile](/docs/tools/docker/dockerfile-guide)
3. **[Git](/docs/tools/git/fundamentals)** — plus ML-specific versioning (DVC, Git-LFS)
4. **[Linux](/docs/tools/linux/fundamentals)** — GPU boxes are Linux boxes
5. Basic ML concepts — training, inference, evaluation (any intro ML course works)

Lessons: [Process Isolation](/learn/containers/process-isolation), [Image Layers](/learn/containers/image-layers), [File Descriptors](/learn/linux/file-descriptors)

✅ **Checkpoint:** you can containerize a Python app and explain the difference between training and inference infrastructure.

## Stage 2: ML Infrastructure (2–3 weeks)

- **Experiment tracking & model registry** — MLflow, Weights & Biases
- **Data pipelines** — Apache Airflow, Kubeflow Pipelines, Argo Workflows
- **Feature stores** — Feast, Tecton
- **GPU infrastructure** — cloud GPU instances, spot strategies; grounding in [AWS](/docs/cloud/aws/fundamentals) or [GCP](/docs/cloud/gcp/fundamentals) (GCP is the most ML-native provider)

Lessons: [Data Versioning](/learn/mlops/data-versioning), [Experiment Tracking](/learn/mlops/experiment-tracking), [GPU Scheduling](/learn/ai-infra/gpu-scheduling)

## Stage 3: Model Serving & Deployment (2–3 weeks)

- **Serving frameworks** — TensorFlow Serving, Triton, BentoML
- **[Kubernetes](/docs/tools/kubernetes/fundamentals) for ML** — Kubeflow, Seldon Core, KServe; know [Workloads](/docs/tools/kubernetes/workloads) and [autoscaling](/docs/tools/kubernetes/production-operations) first
- **Progressive delivery** — A/B tests and canary deployments for models, using [CI/CD](/docs/tools/cicd/fundamentals) and [GitOps](/docs/tools/cicd/gitops) patterns
- **Edge deployment** — quantization, ONNX, model optimization

Lessons: [LLM Serving](/learn/ai-infra/llm-serving), [Inference Autoscaling](/learn/ai-infra/inference-autoscaling), [Model Rollout](/learn/mlops/model-rollout)

## Stage 4: Production ML & LLMOps (2–3 weeks)

- **Monitoring** — model performance, data drift, concept drift (Evidently, WhyLabs)
- **Automated retraining** — trigger pipelines from drift signals
- **LLMOps** — serving large language models (vLLM, Ollama), prompt/version management, token-cost engineering
- **[FinOps for ML](/docs/cloud/finops/fundamentals)** — GPU costs dominate; learn to measure and cut them
- **Responsible AI** — governance, auditability, reproducibility

Lessons: [Drift Monitoring](/learn/mlops/drift-monitoring), [LLM Observability](/learn/aiops/llm-observability), [AI FinOps](/learn/ai-infra/ai-finops)

## Key Tools Map

| Category | Tools |
|:---------|:------|
| Experiment Tracking | MLflow, W&B, Neptune |
| Pipelines | Kubeflow, Airflow, Argo Workflows |
| Serving | Seldon, BentoML, TF Serving, Triton |
| Monitoring | Evidently, WhyLabs, Fiddler |
| LLMOps | vLLM, Ollama, LangChain |

## Prove It

1. **Build** — deploy a model behind a REST API on Kubernetes with autoscaling, tracked in MLflow. That single project exercises every stage above.
2. **Interview** — the ML-adjacent fundamentals get tested too: [Python](/docs/tools/python/interview-questions) · [Docker](/docs/tools/docker/interview-questions) · [Kubernetes](/docs/tools/kubernetes/interview-questions)
3. **Level up** — pair with the [Containers path](/docs/learning-paths/containers) for orchestration depth, or the [Cloud path](/docs/learning-paths/cloud) for GPU infrastructure economics.
