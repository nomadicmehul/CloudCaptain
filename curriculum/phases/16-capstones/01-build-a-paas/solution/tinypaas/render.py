from __future__ import annotations

from typing import Any, Dict, List

NAME_LABEL = "app.kubernetes.io/name"
MANAGED_LABEL = "app.kubernetes.io/managed-by"
RELEASE_ANNOTATION = "tinypaas.dev/release"
CONFIG_ANNOTATION = "tinypaas.dev/config-hash"
CONTAINER = "web"


def namespace_name(prefix: str, app: str) -> str:
    return f"{prefix}-{app}"


def namespace(name: str) -> Dict[str, Any]:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "labels": {MANAGED_LABEL: "tinypaas"}}}


def env_list(config: Dict[str, str], port: int) -> List[Dict[str, str]]:
    merged = dict(config)
    merged.setdefault("PORT", str(port))
    return [{"name": key, "value": merged[key]} for key in sorted(merged)]


def probe(path: str, period: int, failures: int, delay: int = 0) -> Dict[str, Any]:
    return {
        "httpGet": {"path": path, "port": "http"},
        "initialDelaySeconds": delay,
        "periodSeconds": period,
        "timeoutSeconds": 2,
        "failureThreshold": failures,
    }


def container(release: Dict[str, Any]) -> Dict[str, Any]:
    manifest = release["manifest"]
    spec = {
        "name": CONTAINER,
        "image": release["image"],
        "imagePullPolicy": "IfNotPresent",
        "ports": [{"name": "http", "containerPort": manifest["port"]}],
        "env": env_list(release["config"], manifest["port"]),
        "readinessProbe": probe(manifest["health"], period=2, failures=2),
        "livenessProbe": probe(manifest["health"], period=10, failures=3, delay=10),
        "resources": {"requests": dict(manifest["resources"]), "limits": dict(manifest["resources"])},
    }
    if manifest["dockerfile"] and manifest.get("web"):
        spec["command"] = ["sh", "-c", manifest["web"]]
    return spec


def deployment(app: str, ns: str, release: Dict[str, Any], replicas: int) -> Dict[str, Any]:
    labels = {NAME_LABEL: app}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": app, "namespace": ns, "labels": {NAME_LABEL: app, MANAGED_LABEL: "tinypaas"}},
        "spec": {
            "replicas": replicas,
            "revisionHistoryLimit": 10,
            "selector": {"matchLabels": labels},
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": {RELEASE_ANNOTATION: str(release["number"]), CONFIG_ANNOTATION: release["config_hash"]},
                },
                "spec": {"terminationGracePeriodSeconds": 5, "containers": [container(release)]},
            },
        },
    }


def service(app: str, ns: str) -> Dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": app, "namespace": ns, "labels": {NAME_LABEL: app, MANAGED_LABEL: "tinypaas"}},
        "spec": {"type": "NodePort", "selector": {NAME_LABEL: app}, "ports": [{"name": "http", "port": 80, "targetPort": "http"}]},
    }


def release_objects(app: str, ns: str, release: Dict[str, Any], replicas: int) -> Dict[str, Any]:
    return {"apiVersion": "v1", "kind": "List", "items": [deployment(app, ns, release, replicas), service(app, ns)]}
