---
name: experiment-hygiene
description: Make training runs trustworthy evidence - required lineage tags (commit, dirty flag, data version, parent), immutable params, stepped metrics, artifacts per run, registration from a run, promotion and rollback by alias - and audit an MLflow-compatible tracking server for runs that break the rules
version: 1.0.0
phase: mlops
lesson: mlops/experiment-tracking
tags: [mlops, mlflow, experiment-tracking, model-registry, lineage, dvc, reproducibility]
---

# Experiment hygiene

Use this when a team starts tracking experiments, when someone asks "why is the new model worse?", before a model version is promoted, or when an audit asks which code and data produced the model in production. The goal: every number in a review can be traced to a run, every run names its code and data exactly, and production names a version through an alias, never a file path.

## 1. Point everything at one tracking server

```bash
export MLFLOW_TRACKING_URI=http://mlflow.internal:5000          # or minitrack: python3 main.py server --port 5005
curl -s "$MLFLOW_TRACKING_URI/version"                          # MLflow answers its version; check it matches the client's
python3 -c 'import mlflow; print(mlflow.__version__)'
```

Notebooks, CI jobs and training clusters use the same URI from the environment, never a local `mlruns/` directory that dies with the laptop. Artifacts go through the server (`mlflow server --artifacts-destination s3://...`) so clients need no bucket credentials.

## 2. Every run carries lineage

| Tag | Value | Set by |
|---|---|---|
| `mlflow.source.git.commit` | full commit SHA of the training code | MLflow automatically from a git checkout; `minitrack train` |
| `git.dirty` | `true` if tracked files had uncommitted changes | the training script (`git status --porcelain --untracked-files=no`) |
| `data.md5` | md5 from the dataset's `.dvc` pointer, or of the file | the training script; `minitrack train` reads `<file>.dvc` |
| `data.path` | the dataset path in the repo | the training script |
| `mlflow.parentRunId` | the sweep's parent run | `mlflow.start_run(nested=True)` |
| `mlflow.runName` | a readable name such as `lr-0.3` | `run_name=` |

Refuse to train when the data file does not match its committed pointer (`minitrack train` exits 1 with `does not match its pointer`). A run from a dirty tree is allowed for exploration and never promoted.

## 3. Log params once, metrics with steps, artifacts per run

- Params: every input that changes results (learning rate, epochs, feature list, seed, row count), as strings, once. A second value for the same key is an error by design; start a new run instead.
- Metrics: the training curve with `step=epoch` (or batch), plus final evaluation metrics. Batch them (`client.log_batch`, `mlflow.log_metrics`) instead of one request per point.
- A metric you want to range over in searches must be a metric, not a param: `params.lr > '0.05'` is refused.
- Artifacts: the model under `model/`, plus the evaluation report. A run that diverged is marked `FAILED`, keeps its curve, and has no model artifact.

## 4. Compare only like with like

```bash
python3 main.py compare --experiment house-prices --metric rmse            # groups by (data.md5, commit), warns across groups
python3 main.py compare --experiment house-prices --filter "metrics.rmse < 0.5 and tags.\`data.md5\` = '<md5>'" --json
```

```python
client.search_runs([exp_id], "metrics.rmse < 0.5 and tags.`data.md5` = '<md5>' and attributes.status = 'FINISHED'", order_by=["metrics.rmse ASC"])
```

| Finding | Meaning | Action |
|---|---|---|
| best runs have different `data.md5` | the metrics were computed on different data | re-evaluate both models on one data version before choosing |
| best runs have different commits | a code change, not only a param change | review the diff between the commits |
| `git.dirty = true` on the candidate | its code is not identified by the commit | retrain from a clean commit |
| no `data.md5` tag | data version unknown | not promotable; retrain with lineage |
| curve still falling at the last step | under-trained, the comparison is premature | more epochs or a higher learning rate, then compare |
| curve rises or jumps | divergence or a data problem | check the learning rate and the data; the run should be `FAILED` |
| difference smaller than seed-to-seed variation | noise | rerun with several seeds and compare the spread |

## 5. Register from a run, promote with an alias

```python
mv = client.create_model_version("house-prices", f"runs:/{run_id}/model", run_id=run_id)
client.set_model_version_tag("house-prices", mv.version, "validation_status", "approved")
client.set_registered_model_alias("house-prices", "champion", mv.version)
```

```bash
python3 main.py resolve models:/house-prices@champion --predict area_m2=95 rooms=3 age_years=20
```

Serving loads `models:/<name>@champion`, never a bucket path and never `latest`. Aliases replace the deprecated stages; `latest` and `v<number>` are reserved alias names.

## 6. Roll back by moving the pointer

```python
client.set_registered_model_alias("house-prices", "champion", previous_version)
client.get_model_version_by_alias("house-prices", "champion").version
```

Keep the regressed version: it is the evidence for the post-mortem. Record who moved the alias and why as a model version tag.

## 7. Audit a tracking server

```python
from mlflow import MlflowClient
c = MlflowClient()
for exp in c.search_experiments():
    for run in c.search_runs([exp.experiment_id], "attributes.status = 'FINISHED'", max_results=1000):
        t = run.data.tags
        missing = [k for k in ("mlflow.source.git.commit", "data.md5") if k not in t]
        if missing or t.get("git.dirty") == "true":
            print(exp.name, run.info.run_name, run.info.run_id, missing, t.get("git.dirty"))
for rm in c.search_registered_models():
    for alias, version in rm.aliases.items():
        mv = c.get_model_version(rm.name, version)
        print(rm.name, alias, version, mv.run_id or "NO RUN", mv.source)
```

Every aliased version must have a `run_id` whose run has full lineage.

## 8. Report

State: the tracking URI and server version; experiments audited and the count of finished runs missing commit or data tags, or from dirty trees; for each registered model, every alias with its version, run, commit, data md5 and the metric it was promoted on; runs compared in the current decision, grouped by data version and commit, with the winner per group and why groups were or were not comparable; and the rollback target (the previous champion version) for each alias in production.
