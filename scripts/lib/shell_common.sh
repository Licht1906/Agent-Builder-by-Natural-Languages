#!/usr/bin/env bash
# UC1-33 (#145) / UC1-34 (#146) — `log`/`die`/`wait_for_postgres`, shared by
# `install.sh`, `scripts/package_airgap_artifact.sh`, and
# `scripts/import_airgap_artifact.sh` so the three install paths report and
# fail the same way instead of three copies of the same six lines drifting.
#
# Source this file (`source scripts/lib/shell_common.sh`) after `set -euo
# pipefail` in the calling script.

log() { printf '\n\033[1;32m==>\033[0m %s\n' "$1"; }
die() {
    printf '\033[1;31mxx\033[0m %s\n' "$1" >&2
    exit 1
}

#: `wait_for_postgres db_service_name compose_arg...` — polls
#: `pg_isready` inside the named service until it answers. Takes the
#: `docker compose`/`docker-compose` invocation as the remaining arguments
#: (e.g. `wait_for_postgres db docker compose -f docker-compose.airgap.yml`)
#: rather than assuming a fixed binary name, since `install.sh` and
#: `import_airgap_artifact.sh` each resolve `docker compose` vs
#: `docker-compose` themselves before calling this.
wait_for_postgres() {
    local db_service="$1"
    shift
    until "$@" exec -T "$db_service" pg_isready -U postgres >/dev/null 2>&1; do
        sleep 1
    done
}
