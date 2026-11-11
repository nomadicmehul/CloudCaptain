# Scripting & Automation

*Bash and Python for operations: idempotent scripts, CLIs, APIs and safe automation.*

Most automation that breaks in production breaks in the same few ways: an unquoted variable that splits, a script that keeps going after an error, a retry loop that hammers a rate-limited API, a cron job that runs twice across a daylight-saving change, a tool nobody else can install. None of that needs a framework to fix; it needs the habits that good tools encode. This phase writes those tools from the standard library (a Bash linter, a CLI framework, a resilient API client, a jq and awk subset, a scheduler, a wheel builder) and checks each against the tool you would reach for.

## How this phase is built

| Lesson | Type | What you build | What you then use |
|---|---|---|---|
| [Robust Bash](01-robust-bash/docs/en.md) | Build | `bashlint`: a tokenizer and the checks ShellCheck is famous for, plus a strict-mode template script | ShellCheck, shfmt, bats-core, `bash -x` |
| [CLI design](02-cli-design/docs/en.md) | Build | `cliforge`: subcommands, config precedence with `--explain`, `--json`, exit codes, `--dry-run`, completion and golden tests | clig.dev, Click and Typer, `NO_COLOR` |
| [API automation](03-api-automation/docs/en.md) | Build | `apiclient`: timeouts, jittered retries, `Retry-After`, idempotency keys, rate limits, a circuit breaker and pagination | urllib3 `Retry`, `curl --retry`, `gh api --paginate` |
| [Stream processing](04-stream-processing/docs/en.md) | Build | `ministream`: a jq subset and an awk subset that stream in constant memory | jq, awk, sed, `xargs -P` |
| [Scheduling](05-job-scheduling/docs/en.md) | Build | `minicron`: cron and `OnCalendar` parsing, DST-correct next runs, missed-run policies and locks | crontab, systemd timers, `flock`, Kubernetes CronJob |
| [Packaging](06-packaging-tools/docs/en.md) | Build | `shipit`: a reproducible wheel with RECORD hashes, a zipapp and a hash-checked installer | `python -m build`, pip `--require-hashes`, uv, pipx |

Further lessons for this phase are proposed in [ROADMAP.md](https://github.com/nomadicmehul/CloudCaptain/blob/main/ROADMAP.md).

## Reference layer

The long-form guides, cheat sheets and interview questions for this phase live under [Bash](https://cloudcaptain.io/docs/tools/bash), [Python](https://cloudcaptain.io/docs/tools/python) and [YAML](https://cloudcaptain.io/docs/tools/yaml).
