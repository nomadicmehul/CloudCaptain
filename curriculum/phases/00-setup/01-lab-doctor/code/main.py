"""lab_doctor: check the tools every CloudCaptain lesson assumes, say what is missing or old, and print the install plan for this machine."""
from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

Version = Tuple[int, ...]
Runner = Callable[[Sequence[str]], Tuple[int, str]]

VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)")


@dataclass
class Tool:
    name: str
    purpose: str
    minimum: str
    candidates: List[str]
    version_args: List[str]
    install: Dict[str, str] = field(default_factory=dict)
    required: bool = True
    minimums: Dict[str, str] = field(default_factory=dict)

    def minimum_for(self, binary: Optional[str]) -> str:
        return self.minimums.get(binary or "", self.minimum)


@dataclass
class Result:
    tool: Tool
    status: str
    binary: Optional[str] = None
    found: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


TOOLS: List[Tool] = [
    Tool("git", "clone the curriculum and keep your artifacts", "2.30", ["git"], ["--version"],
         {"brew": "brew install git", "apt": "sudo apt-get install -y git", "dnf": "sudo dnf install -y git"}),
    Tool("python", "run Build It code and its tests", "3.9", ["python3", "python"], ["--version"],
         {"brew": "brew install python", "apt": "sudo apt-get install -y python3", "dnf": "sudo dnf install -y python3"}),
    Tool("docker", "containers phase and most Use It steps", "24.0", ["docker", "podman"], ["--version"],
         {"brew": "brew install --cask docker", "apt": "curl -fsSL https://get.docker.com | sh", "dnf": "sudo dnf install -y podman"},
         minimums={"podman": "4.0"}),
    Tool("kubectl", "talk to any Kubernetes cluster", "1.30", ["kubectl"], ["version", "--client", "--output=yaml"],
         {"brew": "brew install kubectl", "apt": "sudo snap install kubectl --classic", "dnf": "sudo dnf install -y kubernetes-client"}),
    Tool("kind", "a throwaway Kubernetes cluster on your laptop", "0.20", ["kind", "k3d"], ["--version"],
         {"brew": "brew install kind", "apt": "go install sigs.k8s.io/kind@latest", "dnf": "go install sigs.k8s.io/kind@latest"},
         minimums={"k3d": "5.0"}),
    Tool("iac", "plan and apply infrastructure (OpenTofu or Terraform)", "1.6", ["tofu", "terraform"], ["version"],
         {"brew": "brew install opentofu", "apt": "sudo snap install opentofu --classic", "dnf": "sudo dnf install -y opentofu"}),
    Tool("node", "the tutor skills installer (npx skills add)", "20.0", ["node"], ["--version"],
         {"brew": "brew install node", "apt": "sudo apt-get install -y nodejs npm", "dnf": "sudo dnf install -y nodejs"}, required=False),
]


def parse_version(text: str) -> Optional[Version]:
    match = VERSION_RE.search(text or "")
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def version_at_least(found: Version, minimum: str) -> bool:
    want = parse_version(minimum) or ()
    width = max(len(found), len(want))
    pad = lambda v: tuple(v) + (0,) * (width - len(v))
    return pad(found) >= pad(want)


def subprocess_runner(cmd: Sequence[str]) -> Tuple[int, str]:
    try:
        proc = subprocess.run(list(cmd), capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def probe(tool: Tool, runner: Runner = subprocess_runner, which: Callable[[str], Optional[str]] = shutil.which) -> Result:
    for candidate in tool.candidates:
        if which(candidate) is None:
            continue
        code, output = runner([candidate, *tool.version_args])
        found = parse_version(output)
        if code != 0 or found is None:
            return Result(tool, "broken", candidate, output.strip()[:80])
        found_text = ".".join(str(p) for p in found)
        return Result(tool, "ok" if version_at_least(found, tool.minimum_for(candidate)) else "old", candidate, found_text)
    return Result(tool, "missing")


def detect_platform() -> Tuple[str, str]:
    system = platform.system().lower()
    if system == "darwin":
        return "macos", "brew" if shutil.which("brew") else "none"
    if system == "linux":
        if shutil.which("apt-get"):
            return "linux", "apt"
        if shutil.which("dnf"):
            return "linux", "dnf"
        return "linux", "none"
    return system or "unknown", "none"


def plan_install(results: Sequence[Result], package_manager: str) -> List[str]:
    commands = []
    for result in results:
        if result.ok:
            continue
        command = result.tool.install.get(package_manager)
        if command:
            commands.append(command)
        else:
            commands.append(f"# install {result.tool.name} ({result.tool.purpose}) manually: no {package_manager} recipe")
    return commands


def linux_notes(system: str) -> List[str]:
    if system == "linux":
        notes = []
        try:
            with open("/sys/fs/cgroup/cgroup.controllers", encoding="ascii") as handle:
                notes.append(f"cgroup v2: yes ({handle.read().strip()})")
        except OSError:
            notes.append("cgroup v2: not mounted; memory limits in the containers phase will not work")
        notes.append("unshare: " + ("available" if shutil.which("unshare") else "missing (install util-linux)"))
        return notes
    return [
        "Linux-only steps (namespaces, cgroups, mounts) need a Linux VM on this machine:",
        "  macOS: `brew install lima && limactl start` or Docker Desktop's VM via `docker run --privileged -it ubuntu`",
        "  Windows: WSL 2 with Ubuntu, then run everything inside it",
    ]


def report_markdown(results: Sequence[Result], system: str, package_manager: str, commands: Sequence[str], notes: Sequence[str]) -> str:
    lines = [
        "# LAB.md — CloudCaptain lab report",
        "",
        f"Platform: {system} · package manager: {package_manager}",
        "",
        "| Tool | For | Minimum | Found | Status |",
        "|---|---|---|---|---|",
    ]
    for result in results:
        found = f"{result.binary} {result.found}" if result.found else "–"
        flag = {"ok": "✅ ok", "old": "⚠️ old", "missing": "❌ missing", "broken": "❌ broken"}[result.status]
        if not result.tool.required and not result.ok:
            flag += " (optional)"
        lines.append(f"| {result.tool.name} | {result.tool.purpose} | {result.tool.minimum_for(result.binary)} | {found} | {flag} |")
    lines.append("")
    lines.append("## Linux features")
    lines.extend(f"- {note}" for note in notes)
    lines.append("")
    lines.append("## Install plan")
    if commands:
        lines.append("```bash")
        lines.extend(commands)
        lines.append("```")
    else:
        lines.append("Nothing to install. Start the first lesson.")
    return "\n".join(lines) + "\n"


def to_json(results: Sequence[Result], system: str, package_manager: str, commands: Sequence[str]) -> dict:
    return {
        "platform": system,
        "packageManager": package_manager,
        "ready": all(r.ok or not r.tool.required for r in results),
        "tools": [{"name": r.tool.name, "status": r.status, "binary": r.binary, "found": r.found, "minimum": r.tool.minimum_for(r.binary), "required": r.tool.required} for r in results],
        "install": list(commands),
    }


def main(argv: Optional[Sequence[str]] = None, runner: Runner = subprocess_runner) -> int:
    parser = argparse.ArgumentParser(prog="lab_doctor", description=__doc__)
    parser.add_argument("command", nargs="?", default="check", choices=["check", "plan", "report"])
    parser.add_argument("--out", default="LAB.md", help="where `report` writes the markdown")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    system, package_manager = detect_platform()
    results = [probe(tool, runner) for tool in TOOLS]
    commands = plan_install(results, package_manager)
    notes = linux_notes(system)
    ready = all(r.ok or not r.tool.required for r in results)

    if args.json:
        print(json.dumps(to_json(results, system, package_manager, commands), indent=2))
    elif args.command == "plan":
        print("\n".join(commands) if commands else "# nothing to install")
    elif args.command == "report":
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(report_markdown(results, system, package_manager, commands, notes))
        print(f"wrote {args.out}")
    else:
        for result in results:
            found = f"{result.binary} {result.found}" if result.found else "-"
            print(f"{result.status:>8}  {result.tool.name:<8} {found:<22} min {result.tool.minimum_for(result.binary):<6} {result.tool.purpose}")
        print()
        print("\n".join(notes))
        print()
        print("ready: yes, start the first lesson" if ready else "ready: no, run `python3 main.py plan` for the install commands")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
