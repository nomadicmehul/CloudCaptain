from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

from . import aws

STATE_BUCKET = "lz-tfstate"
REGION = "eu-central-1"
LOCK_MARKER = "Error acquiring the state lock"
LOCK_FIELD = re.compile(r"^\s*(ID|Path|Operation|Who|Created):\s*(.*?)\s*$", re.MULTILINE)
EMULATED_SERVICES = ("s3", "sts", "iam", "dynamodb")


@dataclass
class Completed:
    code: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return (self.out + "\n" + self.err).strip()


Runner = Callable[[Sequence[str], str, Dict[str, str]], Completed]


class LzError(Exception):
    def __init__(self, message: str, code: int = 1, payload: Optional[Dict[str, object]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


def subprocess_runner(argv: Sequence[str], cwd: str, env: Dict[str, str]) -> Completed:
    try:
        done = subprocess.run(list(argv), cwd=cwd, env=env, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return Completed(127, "", f"{argv[0]}: command not found; install OpenTofu (https://opentofu.org/docs/intro/install/)")
    return Completed(done.returncode, done.stdout, done.stderr)


def lock_info(text: str) -> Optional[Dict[str, str]]:
    if LOCK_MARKER not in text:
        return None
    return {key.lower(): value for key, value in LOCK_FIELD.findall(text)}


def backend_config(endpoint: str) -> str:
    endpoints = "\n".join(f'  {service} = "{endpoint}"' for service in EMULATED_SERVICES)
    return "\n".join([
        "endpoints = {",
        endpoints,
        "}",
        "use_path_style              = true",
        "skip_credentials_validation = true",
        "skip_requesting_account_id  = true",
        "skip_metadata_api_check     = true",
        "skip_region_validation      = true",
        "skip_s3_checksum            = true",
        "",
    ])


class Tofu:
    def __init__(self, root: str, environ: Optional[Dict[str, str]] = None, runner: Runner = subprocess_runner, binary: str = "tofu") -> None:
        self.root = root
        self.environ = dict(os.environ if environ is None else environ)
        self.runner = runner
        self.binary = binary
        given = self.environ.get("LZ_AWS_ENDPOINT", "").strip()
        try:
            self.endpoint = aws.local_endpoint(given) if given else ""
        except aws.EndpointError as error:
            raise LzError(str(error), 2) from None

    @property
    def work(self) -> str:
        path = os.path.join(self.root, ".lz")
        os.makedirs(os.path.join(path, "plans"), exist_ok=True)
        return path

    def env_dir(self, env: str) -> str:
        path = os.path.join(self.root, "envs", env)
        if not re.fullmatch(r"[a-z][a-z0-9-]*", env) or not os.path.isfile(os.path.join(path, "main.tf")):
            known = sorted(d for d in os.listdir(os.path.join(self.root, "envs")) if os.path.isdir(os.path.join(self.root, "envs", d))) if os.path.isdir(os.path.join(self.root, "envs")) else []
            raise LzError(f"no environment {env!r}: expected envs/{env}/main.tf (known: {', '.join(known) or 'none'})", 2)
        return path

    def environment(self) -> Dict[str, str]:
        env = dict(self.environ)
        env.setdefault("TF_IN_AUTOMATION", "1")
        env["TF_INPUT"] = "0"
        if self.endpoint:
            env["TF_VAR_aws_endpoint"] = self.endpoint
            env.setdefault("AWS_ACCESS_KEY_ID", aws.FAKE_KEY)
            env.setdefault("AWS_SECRET_ACCESS_KEY", aws.FAKE_KEY)
            env.setdefault("AWS_REGION", REGION)
            env["AWS_EC2_METADATA_DISABLED"] = "true"
        return env

    def run(self, env: str, args: List[str]) -> Completed:
        return self.runner([self.binary] + args, self.env_dir(env), self.environment())

    def checked(self, env: str, args: List[str], what: str) -> Completed:
        done = self.run(env, args)
        if done.code != 0:
            info = lock_info(done.text)
            if info is not None:
                raise LzError(f"envs/{env}: the state is locked by {info.get('who', 'someone')} (lock {info.get('id', '?')}); wait for that run or `tofu force-unlock` it", 3, {"error": "state-locked", "lock": info})
            raise LzError(f"{what} failed for envs/{env} (exit {done.code}):\n{done.text[-3000:]}", 1)
        return done

    def init(self, env: str) -> None:
        args = ["init", "-input=false", "-no-color", "-reconfigure"]
        if self.endpoint:
            aws.ensure_bucket(self.endpoint, STATE_BUCKET, REGION)
            path = os.path.join(self.work, f"backend-{env}.hcl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(backend_config(self.endpoint))
            args.append(f"-backend-config={path}")
        self.checked(env, args, "tofu init")

    def show_json(self, env: str, plan_file: str) -> Dict[str, object]:
        done = self.checked(env, ["show", "-json", "-no-color", plan_file], "tofu show -json")
        try:
            return json.loads(done.out)
        except ValueError as error:
            raise LzError(f"tofu show -json did not print JSON: {error}") from None

    def plan(self, env: str, refresh_only: bool = False) -> Dict[str, object]:
        self.init(env)
        suffix = "drift" if refresh_only else "plan"
        plan_file = os.path.join(self.work, "plans", f"{env}.{suffix}.tfplan")
        args = ["plan", "-input=false", "-no-color", "-lock-timeout=0s", f"-out={plan_file}"]
        if refresh_only:
            args.append("-refresh-only")
        self.checked(env, args, "tofu plan")
        data = self.show_json(env, plan_file)
        json_path = os.path.join(self.work, "plans", f"{env}.{suffix}.json")
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        return {"plan_file": plan_file, "plan_json": json_path, "plan": data}

    def apply(self, env: str) -> Dict[str, object]:
        self.init(env)
        self.checked(env, ["apply", "-input=false", "-no-color", "-auto-approve", "-lock-timeout=0s"], "tofu apply")
        self.checked(env, ["apply", "-refresh-only", "-input=false", "-no-color", "-auto-approve", "-lock-timeout=0s"], "tofu apply -refresh-only")
        done = self.checked(env, ["output", "-json", "-no-color"], "tofu output")
        try:
            outputs = json.loads(done.out or "{}")
        except ValueError:
            outputs = {}
        return {name: value.get("value") for name, value in outputs.items() if isinstance(value, dict)}
