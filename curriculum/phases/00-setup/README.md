# Setup & Lab

*A laptop lab (kind, Docker, free cloud tiers) and your coding agent, ready for every later phase.*

Nothing in this curriculum needs a cloud account to start. It needs a Linux kernel you can reach (your own, or a VM on macOS and Windows), a container engine, a throwaway Kubernetes cluster, an infrastructure-as-code CLI, and the tutor skills in your coding agent. This phase gets you there with scripts you can rerun, so the environment is never the thing you are debugging.

## How this phase is built

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| Lab Doctor | Build | a probe that compares the machine against the course's requirements and prints the install plan | `docker run`, `kind create cluster`, `kubectl get nodes`, `tofu version`, `npx skills add` |

More lessons in this phase are listed in [ROADMAP.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/ROADMAP.md): a Linux VM on macOS and Windows with Lima and WSL 2, free-tier cloud accounts with spend guards, and setting up your coding agent with the tutor.

## Reference layer

Per-tool installation guides live in the Reference layer: [Docker](https://cloudcaptain.io/docs/tools/docker/), [Kubernetes](https://cloudcaptain.io/docs/tools/kubernetes/), [Terraform](https://cloudcaptain.io/docs/tools/terraform/), [Linux](https://cloudcaptain.io/docs/tools/linux/).
