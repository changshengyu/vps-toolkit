#!/usr/bin/env bash
# No action when sourced on a collector; dependencies installed only by explicit backup actions.
monitor_backup() {
    linux_root
    local action="$1"
    case "$action" in
        setup|run|restore|resume)
            ensure_packages python3 age
            require_command age-keygen
            ;;
    esac
    if [ "$action" = restore ]; then setup_docker; fi
    python3 "$ROOT_DIR/lib/monitor/backup.py" --monitor "$MONITOR_DIR" "$action"
}
