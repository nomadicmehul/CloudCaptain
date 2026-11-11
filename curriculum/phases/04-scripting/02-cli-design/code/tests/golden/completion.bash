_ccops_complete() {
    local cur prev word candidate path i skip commands flags dynamic line
    local -a extra
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev=""
    if (( COMP_CWORD > 0 )); then
        prev="${COMP_WORDS[COMP_CWORD-1]}"
    fi
    path="ccops"
    skip=0
    extra=()
    for (( i = 1; i < COMP_CWORD; i++ )); do
        word="${COMP_WORDS[i]}"
        if (( skip )); then
            skip=0
            continue
        fi
        case "$word" in
            --config|-i|--inventory)
                extra+=("$word" "${COMP_WORDS[i+1]}")
                skip=1
                ;;
            --config=*|--inventory=*)
                extra+=("$word")
                ;;
            --color|--env|--ip|--lock-timeout|--output|--role|--state|--tag|-o)
                skip=1
                ;;
            -*)
                ;;
            *)
                candidate="$path $word"
                case "$candidate" in
                    "ccops completion"|"ccops config"|"ccops hosts"|"ccops hosts add"|"ccops hosts list"|"ccops hosts remove"|"ccops hosts show"|"ccops man")
                        path="$candidate"
                        ;;
                esac
                ;;
        esac
    done
    case "$prev" in
        --config|--inventory|-i)
            if type compopt >/dev/null 2>&1; then
                compopt -o filenames
            fi
            while IFS= read -r line; do COMPREPLY+=("$line"); done < <(compgen -f -- "$cur")
            return 0
            ;;
        --env|--ip|--lock-timeout|--role|--tag)
            return 0
            ;;
        --state)
            while IFS= read -r line; do COMPREPLY+=("$line"); done < <(compgen -W "active maintenance retired" -- "$cur")
            return 0
            ;;
        --color)
            while IFS= read -r line; do COMPREPLY+=("$line"); done < <(compgen -W "auto always never" -- "$cur")
            return 0
            ;;
        --output|-o)
            while IFS= read -r line; do COMPREPLY+=("$line"); done < <(compgen -W "text json" -- "$cur")
            return 0
            ;;
    esac
    commands=""
    dynamic=""
    case "$path" in
        "ccops")
            commands="hosts config completion man"
            flags="-o --output --json --color -i --inventory --lock-timeout --config -q --quiet -v --verbose -h --help --version"
            ;;
        "ccops completion")
            commands="bash"
            flags="-o --output --json --color -i --inventory --lock-timeout --config -q --quiet -v --verbose -h --help"
            ;;
        "ccops config")
            commands=""
            flags="--explain -o --output --json --color -i --inventory --lock-timeout --config -q --quiet -v --verbose -h --help"
            ;;
        "ccops hosts")
            commands="list show add remove"
            flags="-o --output --json --color -i --inventory --lock-timeout --config -q --quiet -v --verbose -h --help"
            ;;
        "ccops hosts add")
            commands=""
            flags="--env --role --ip --state --tag -n --dry-run -o --output --json --color -i --inventory --lock-timeout --config -q --quiet -v --verbose -h --help"
            ;;
        "ccops hosts list")
            commands=""
            flags="--env --role --state -o --output --json --color -i --inventory --lock-timeout --config -q --quiet -v --verbose -h --help"
            ;;
        "ccops hosts remove")
            commands=""
            flags="--missing-ok -n --dry-run -y --yes -o --output --json --color -i --inventory --lock-timeout --config -q --quiet -v --verbose -h --help"
            dynamic="hosts list --quiet"
            ;;
        "ccops hosts show")
            commands=""
            flags="-o --output --json --color -i --inventory --lock-timeout --config -q --quiet -v --verbose -h --help"
            dynamic="hosts list --quiet"
            ;;
        "ccops man")
            commands=""
            flags="-o --output --json --color -i --inventory --lock-timeout --config -q --quiet -v --verbose -h --help"
            ;;
    esac
    if [[ "$cur" == -* ]]; then
        while IFS= read -r line; do COMPREPLY+=("$line"); done < <(compgen -W "$flags" -- "$cur")
    elif [[ -n "$commands" ]]; then
        while IFS= read -r line; do COMPREPLY+=("$line"); done < <(compgen -W "$commands" -- "$cur")
    elif [[ -n "$dynamic" ]]; then
        while IFS= read -r line; do
            [[ -n "$line" ]] && COMPREPLY+=("$line")
        done < <(compgen -W "$("${COMP_WORDS[0]}" ${extra[@]+"${extra[@]}"} $dynamic 2>/dev/null)" -- "$cur")
    fi
    return 0
}
complete -F _ccops_complete ccops
