import hashlib
import json
import sys
import tempfile
from pathlib import Path

import mlflow
from mlflow import MlflowClient
from mlflow.entities import Metric
from mlflow.exceptions import MlflowException

sys.path.insert(0, str(Path(__file__).resolve().parent))
import main  # noqa: E402

EXPERIMENT = "cc-house-prices"
OTHER_EXPERIMENT = "cc-churn"
MODEL = "cc-house-prices"
LRS = [0.01, 0.03, 0.1, 0.3, 2.5]
EPOCHS = 30
COMMIT = "c0ffee" * 6 + "c0ff"
PROBE = {"area_m2": 95.0, "rooms": 3.0, "age_years": 20.0}
SEARCHES = [
    ("metrics.rmse < 0.5 and params.lr = '0.1'", ["metrics.rmse ASC"]),
    ("metrics.rmse < 0.5", ["metrics.rmse ASC"]),
    ("", ["metrics.rmse DESC"]),
    ("params.lr != '0.01'", ["params.lr DESC"]),
    ("attributes.status = 'FAILED'", []),
    ("tags.`mlflow.parentRunId` LIKE '%'", ["attributes.run_name ASC"]),
    ("run_name ILIKE 'LR-0.%'", ["run_name DESC"]),
    ("metrics.r2 >= 0.95 and tags.`data.md5` = '{md5}'", ["metrics.r2 DESC"]),
    ("tags.mlflow.parentRunId != '' and params.lr LIKE '0.0%'", ["params.lr ASC"]),
    ("params.epochs = '30' AND metrics.train_rmse <= 2", ["metrics.train_rmse ASC"]),
    ("", []),
]
BAD_FILTERS = [
    "params.lr = 0.1",
    "metrics.rmse < 0.5 or params.lr = '0.1'",
    "params.lr > '0.05'",
    "metrics.rmse < '0.5'",
]


class Diverged(Exception):
    pass


def error_code(action):
    try:
        action()
    except MlflowException as exc:
        return exc.error_code
    return "no error"


def train_and_log(client, rows, lineage, workdir):
    with mlflow.start_run(run_name="sweep") as parent:
        mlflow.set_tags(lineage)
        best = None
        for lr in LRS:
            result = main.train_linear(rows, lr, EPOCHS)
            try:
                with mlflow.start_run(run_name=main.run_name_for(lr), nested=True) as run:
                    mlflow.log_param("lr", main.format_number(lr))
                    mlflow.log_params({"epochs": str(EPOCHS), "rows": str(len(rows))})
                    mlflow.set_tags(lineage)
                    for step, value in enumerate(result.history):
                        mlflow.log_metric("rmse", value, step=step)
                    if result.diverged:
                        mlflow.set_tag("failure", f"diverged after {len(result.history)} epochs")
                        raise Diverged(lr)
                    stamp = main.now_ms()
                    client.log_batch(run.info.run_id, metrics=[Metric(k, v, stamp, EPOCHS) for k, v in sorted(result.metrics.items()) if k != "rmse"])
                    model_path = workdir / "model.json"
                    model_path.write_text(json.dumps(result.model(), indent=2, sort_keys=True) + "\n")
                    mlflow.log_artifact(str(model_path), artifact_path="model")
                    mlflow.log_dict({"lr": lr, "data": lineage}, "meta/lineage.json")
                    if best is None or result.history[-1] < best[1]:
                        best = (main.run_name_for(lr), result.history[-1])
            except Diverged:
                pass
        mlflow.log_metric("best_rmse", best[1])
        mlflow.set_tag("best_run", best[0])
    return parent.info.run_id


def session(uri, workdir):
    mlflow.set_tracking_uri(uri)
    client = MlflowClient(uri)
    data = workdir / "houses.csv"
    rows = main.make_dataset(400, 7)
    main.write_csv(rows, data)
    lineage = main.data_lineage(data)
    lineage.update({main.TAG_GIT_COMMIT: COMMIT, main.TAG_GIT_DIRTY: "false"})
    md5 = lineage[main.TAG_DATA_MD5]
    out = {}

    experiment_id = client.create_experiment(EXPERIMENT, tags={"team": "pricing", "task": "regression"})
    client.create_experiment(OTHER_EXPERIMENT, tags={"team": "growth"})
    mlflow.set_experiment(EXPERIMENT)
    experiment = client.get_experiment_by_name(EXPERIMENT)
    out["experiment"] = {
        "name": experiment.name,
        "tags": experiment.tags,
        "lifecycle_stage": experiment.lifecycle_stage,
        "artifact_location": experiment.artifact_location.replace(experiment_id, "<id>"),
        "by_id": client.get_experiment(experiment_id).name,
    }
    out["experiment_searches"] = {
        "name LIKE 'cc-%'": sorted(e.name for e in client.search_experiments(filter_string="name LIKE 'cc-%'")),
        "order_by name ASC": [e.name for e in client.search_experiments(order_by=["name ASC"])],
        "tags.team = 'growth'": [e.name for e in client.search_experiments(filter_string="tags.team = 'growth'")],
    }

    parent_id = train_and_log(client, rows, lineage, workdir)
    all_runs = client.search_runs([experiment_id], max_results=1000)
    names = {r.info.run_id: r.info.run_name for r in all_runs}

    def normalize_tags(tags):
        return {k: names.get(v, v) if k == main.TAG_PARENT else v for k, v in tags.items()}

    out["runs"] = {
        r.info.run_name: {
            "status": r.info.status,
            "params": r.data.params,
            "metrics": r.data.metrics,
            "tags": normalize_tags(r.data.tags),
            "artifact_uri": r.info.artifact_uri.replace(r.info.run_id, "<run>").replace(experiment_id, "<id>"),
            "lifecycle_stage": r.info.lifecycle_stage,
            "has_end_time": r.info.end_time is not None,
        }
        for r in all_runs
    }
    by_name = {v: k for k, v in names.items()}
    out["run_get_matches_search"] = all(client.get_run(run_id).data.metrics == next(r for r in all_runs if r.info.run_id == run_id).data.metrics for run_id in names)
    out["metric_history"] = {
        name: [(m.step, m.value) for m in client.get_metric_history(run_id, "rmse")]
        for run_id, name in sorted(names.items(), key=lambda item: item[1])
        if name != "sweep"
    }
    out["searches"] = {}
    for filter_string, order_by in SEARCHES:
        found = client.search_runs([experiment_id], filter_string.format(md5=md5), order_by=order_by)
        out["searches"][f"{filter_string} | {order_by}"] = [r.info.run_name for r in found]
    pages, token = [], None
    while True:
        page = client.search_runs([experiment_id], "", order_by=["attributes.run_name ASC"], max_results=2, page_token=token)
        pages.append([r.info.run_name for r in page])
        token = page.token
        if not token:
            break
    out["pages_of_two"] = pages
    out["errors"] = {f"filter {f}": error_code(lambda f=f: client.search_runs([experiment_id], f)) for f in BAD_FILTERS}
    best_id = by_name["lr-0.3"]
    out["errors"]["log_param same value"] = error_code(lambda: client.log_param(best_id, "lr", "0.3"))
    out["errors"]["log_param new value"] = error_code(lambda: client.log_param(best_id, "lr", "0.5"))
    out["errors"]["get missing run"] = error_code(lambda: client.get_run("0" * 32))
    out["errors"]["duplicate experiment"] = error_code(lambda: client.create_experiment(EXPERIMENT))

    def listing(run_id, path=None):
        return [(f.path, f.is_dir, f.file_size) for f in client.list_artifacts(run_id, path)]

    out["artifacts"] = {
        "root": listing(best_id),
        "model": listing(best_id, "model"),
        "meta": listing(best_id, "meta"),
        "failed run": listing(by_name["lr-2.5"]),
        "parent": listing(parent_id),
    }
    downloaded = Path(client.download_artifacts(best_id, "model/model.json", str(workdir / "dl")))
    out["artifacts"]["model.json sha256"] = hashlib.sha256(downloaded.read_bytes()).hexdigest()
    out["artifacts"]["lineage.json"] = mlflow.artifacts.load_dict(f"runs:/{best_id}/meta/lineage.json")

    client.create_registered_model(MODEL, tags={"owner": "pricing"}, description="House price regression")
    v1 = client.create_model_version(MODEL, f"runs:/{by_name['lr-0.1']}/model", run_id=by_name["lr-0.1"])
    v2 = client.create_model_version(MODEL, f"runs:/{best_id}/model", run_id=best_id)
    client.set_model_version_tag(MODEL, v2.version, "validation_status", "approved")
    client.set_registered_model_tag(MODEL, "task", "regression")
    client.set_registered_model_alias(MODEL, "champion", v2.version)
    client.set_registered_model_alias(MODEL, "challenger", v1.version)

    def version_view(mv):
        return {"version": mv.version, "run": names.get(mv.run_id), "source": mv.source.replace(mv.run_id, names.get(mv.run_id, "?")), "status": mv.status, "tags": mv.tags}

    def champion():
        mv = client.get_model_version_by_alias(MODEL, "champion")
        local = Path(mlflow.artifacts.download_artifacts(f"models:/{MODEL}@champion", dst_path=str(workdir / f"champion-{mv.version}")))
        model = json.loads((local / "model.json").read_text())
        return {"alias_points_to": version_view(mv), "aliases": sorted(mv.aliases), "files": sorted(p.name for p in local.iterdir()), "prediction": main.predict(model, PROBE)}

    out["registry"] = {"created": [version_view(v1), version_view(v2)], "champion": champion()}
    registered = client.get_registered_model(MODEL)
    out["registry"]["model"] = {
        "description": registered.description,
        "tags": registered.tags,
        "aliases": registered.aliases,
        "latest_versions": [v.version for v in registered.latest_versions],
    }
    out["registry"]["search name"] = [version_view(v) for v in client.search_model_versions(f"name = '{MODEL}'", order_by=["version_number DESC"])]
    out["registry"]["search tag"] = [v.version for v in client.search_model_versions("tags.validation_status = 'approved'")]
    client.set_registered_model_alias(MODEL, "champion", v1.version)
    out["registry"]["after rollback"] = champion()
    client.delete_registered_model_alias(MODEL, "challenger")
    out["registry"]["aliases after delete"] = client.get_registered_model(MODEL).aliases
    out["registry"]["get v1"] = {"aliases": sorted(client.get_model_version(MODEL, "1").aliases), "tags": client.get_model_version(MODEL, "2").tags}
    out["registry"]["errors"] = {
        "missing alias": error_code(lambda: client.get_model_version_by_alias(MODEL, "challenger")),
        "missing model": error_code(lambda: client.get_registered_model("cc-nope")),
        "duplicate model": error_code(lambda: client.create_registered_model(MODEL)),
        "missing version": error_code(lambda: client.get_model_version(MODEL, "9")),
        "alias to missing version": error_code(lambda: client.set_registered_model_alias(MODEL, "shadow", "9")),
        "reserved alias latest": error_code(lambda: client.set_registered_model_alias(MODEL, "latest", "1")),
        "reserved alias v3": error_code(lambda: client.set_registered_model_alias(MODEL, "v3", "1")),
    }
    return out


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        result = session(sys.argv[1], Path(tmp))
    text = json.dumps(result, indent=2, sort_keys=True)
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(text + "\n")
    else:
        print(text)
