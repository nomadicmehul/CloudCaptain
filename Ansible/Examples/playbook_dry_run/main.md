
## Running the playbook using --syntax-check command for Dry-run

```
ansible-playbook --syntax-check --list-tasks -i hosts_file playbook.yaml
```

## some more examples

Sometimes you may want to have a task to be executed even in check mode. To achieve this, use the always_run clause on the task. 

```
tasks:

  - name: this task is run even in check mode
    command: /something/to/run --even-in-check-mode
    always_run: yes
```

The --diff option to ansible-playbook works great with --check (detailed above) but can also be used by itself. When this flag is supplied, if any templated files on the remote system are changed, and the ansible-playbook CLI will report back the textual changes made to the file (or, if used with --check, the changes that would have been made). Since the diff feature produces a large amount of output, it is best used when checking a single host at a time, like so:

```
ansible-playbook foo.yml --check --diff --limit foo.example.com
```

