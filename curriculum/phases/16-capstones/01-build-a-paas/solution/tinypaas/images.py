from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
from typing import Any, Dict, Optional

from . import manifest as manifests
from .runner import PaasError, Runner, must

DEFAULT_BASE_IMAGE = "python:3.13-alpine"
SHORT_SHA = 7


def build_kind(manifest: Dict[str, Any]) -> str:
    if manifest["dockerfile"]:
        return "dockerfile"
    return "static" if manifest.get("static") else "python"


def generated_dockerfile(manifest: Dict[str, Any], base_image: str, has_requirements: bool = False) -> str:
    port = manifest["port"]
    lines = [f"FROM {base_image}"]
    if build_kind(manifest) == "static":
        serve = 'exec python3 -m http.server "$PORT" --bind 0.0.0.0 --directory /srv'
        lines += ["WORKDIR /srv", f"COPY {manifest['static'].rstrip('/')}/ /srv/", f"ENV PORT={port}", f"EXPOSE {port}", f"CMD {json.dumps(['sh', '-c', serve])}"]
        return "\n".join(lines) + "\n"
    lines += ["WORKDIR /app", "COPY . /app/"]
    if has_requirements:
        lines.append("RUN pip install --no-cache-dir -r requirements.txt")
    lines += ["ENV PYTHONUNBUFFERED=1", f"ENV PORT={port}", f"EXPOSE {port}", f"CMD {json.dumps(['sh', '-c', 'exec ' + manifest['web']])}"]
    return "\n".join(lines) + "\n"


def image_ref(app: str, commit: str) -> str:
    return f"tinypaas/{app}:{commit[:SHORT_SHA]}"


def source_commit(app_dir: str, run: Runner) -> Dict[str, str]:
    inside = run(["git", "-C", app_dir, "rev-parse", "--is-inside-work-tree"], timeout=20)
    if not inside.ok or inside.out.strip() != "true":
        raise PaasError(f"{app_dir} is not a git checkout: tinypaas builds commits, so run git init and commit the app first")
    head = run(["git", "-C", app_dir, "rev-parse", "--verify", "HEAD^{commit}"], timeout=20)
    if not head.ok:
        raise PaasError(f"{app_dir} has no commits yet: commit the app first")
    prefix = must(run(["git", "-C", app_dir, "rev-parse", "--show-prefix"], timeout=20), "git rev-parse --show-prefix")
    return {"commit": head.out.strip(), "prefix": prefix.out.strip()}


def safe_members(archive: tarfile.TarFile):
    for member in archive.getmembers():
        parts = member.name.split("/")
        if member.name.startswith("/") or ".." in parts or member.issym() and os.path.isabs(member.linkname):
            raise PaasError(f"refusing to extract {member.name} from the git archive")
        yield member


def export_commit(app_dir: str, source: Dict[str, str], dest: str, run: Runner) -> None:
    tree = f"{source['commit']}:{source['prefix']}" if source["prefix"] else source["commit"]
    archive = os.path.join(dest, "source.tar")
    must(run(["git", "-C", app_dir, "archive", "--format=tar", "-o", archive, tree], timeout=120), "git archive")
    context = os.path.join(dest, "context")
    os.makedirs(context)
    with tarfile.open(archive) as tar:
        tar.extractall(context, members=list(safe_members(tar)))
    os.remove(archive)


def prepare_context(context: str, manifest: Dict[str, Any], base_image: str) -> str:
    if manifest["dockerfile"]:
        return "dockerfile"
    has_requirements = os.path.isfile(os.path.join(context, "requirements.txt"))
    with open(os.path.join(context, "Dockerfile"), "w", encoding="utf-8") as f:
        f.write(generated_dockerfile(manifest, base_image, has_requirements))
    return build_kind(manifest)


def build(app_dir: str, run: Runner, kind_cluster: Optional[str], base_image: Optional[str] = None, log=None) -> Dict[str, Any]:
    source = source_commit(app_dir, run)
    work = tempfile.mkdtemp(prefix="tinypaas-build-")
    try:
        export_commit(app_dir, source, work, run)
        context = os.path.join(work, "context")
        report = manifests.validate(context)
        if not report["valid"]:
            raise PaasError(f"app.json in commit {source['commit'][:SHORT_SHA]} is invalid (did you commit your fix?):\n{manifests.describe_errors(report['errors'])}")
        manifest = report["manifest"]
        kind = prepare_context(context, manifest, base_image or DEFAULT_BASE_IMAGE)
        ref = image_ref(manifest["name"], source["commit"])
        if log:
            log(f"-----> Building {ref} from commit {source['commit'][:SHORT_SHA]} ({kind})")
        must(run(["docker", "build", "--tag", ref,
                  "--label", f"org.opencontainers.image.revision={source['commit']}",
                  "--label", f"dev.tinypaas.app={manifest['name']}", context], timeout=600), "docker build")
        digest = must(run(["docker", "image", "inspect", "--format", "{{.Id}}", ref], timeout=30), "docker image inspect").out.strip()
        if kind_cluster:
            if log:
                log(f"-----> Loading {ref} into kind cluster {kind_cluster}")
            must(run(["kind", "load", "docker-image", ref, "--name", kind_cluster], timeout=300), "kind load docker-image")
        return {"app": manifest["name"], "image": ref, "commit": source["commit"], "digest": digest, "kind": kind, "manifest": manifest}
    finally:
        shutil.rmtree(work, ignore_errors=True)
