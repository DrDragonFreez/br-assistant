#!/bin/sh

set -e

if [ -n "${PROMETHEUS_MULTIPROC_DIR}" ]; then
    mkdir -p "${PROMETHEUS_MULTIPROC_DIR}"
    rm -f "${PROMETHEUS_MULTIPROC_DIR}"/*.db 2>/dev/null || true
fi

if [ "${RUN_MIGRATIONS}" = "1" ]; then
    echo ">> waiting for postgres ${POSTGRES_HOST:-postgres}:${POSTGRES_PORT:-5432}"
    attempt=0
    until python -c "
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
with socket.create_connection((host, port), timeout=2):
    pass
" "${POSTGRES_HOST:-postgres}" "${POSTGRES_PORT:-5432}" 2>/dev/null; do
        attempt=$((attempt + 1))
        if [ "${attempt}" -ge 30 ]; then
            echo "!! postgres is not reachable, giving up after ${attempt} attempts" >&2
            exit 1
        fi
        sleep 2
    done

    echo ">> alembic upgrade head"
    alembic upgrade head
fi

exec "$@"
