#!/usr/bin/env bash
set -euo pipefail

: "${BUCKET_NAME:?BUCKET_NAME must be set.}"
: "${SA_PATH_JSON:?SA_PATH_JSON must be set to a readable credential path.}"
: "${MOUNT_DIR:=/mnt/gcsfuse}"
: "${API_PORT:=8081}"

if [[ -d "${SA_PATH_JSON}" ]]; then
  CANDIDATE_JSON="$(find "${SA_PATH_JSON}" -maxdepth 1 -type f -name '*.json' | head -n 1 || true)"
  if [[ -z "${CANDIDATE_JSON}" ]]; then
    echo "SA_PATH_JSON points to a directory without any .json file: ${SA_PATH_JSON}" >&2
    exit 1
  fi
  SA_PATH_JSON="${CANDIDATE_JSON}"
fi

if [[ ! -f "${SA_PATH_JSON}" ]]; then
  echo "SA_PATH_JSON must point to a JSON file. Current value: ${SA_PATH_JSON}" >&2
  exit 1
fi

mkdir -p "${MOUNT_DIR}"

if mountpoint -q "${MOUNT_DIR}"; then
  fusermount3 -u "${MOUNT_DIR}" || true
fi

GCSFUSE_ARGS=()
if [[ -n "${GCSFUSE_MOUNT_FLAGS:-}" ]]; then
  read -r -a GCSFUSE_ARGS <<< "${GCSFUSE_MOUNT_FLAGS}"
fi

gcsfuse "${GCSFUSE_ARGS[@]}" --key-file "${SA_PATH_JSON}" "${BUCKET_NAME}" "${MOUNT_DIR}" &
GCSFUSE_PID=$!

cleanup() {
  if mountpoint -q "${MOUNT_DIR}"; then
    fusermount3 -u "${MOUNT_DIR}" || true
  fi
  if ps -p "${GCSFUSE_PID}" >/dev/null 2>&1; then
    kill "${GCSFUSE_PID}" || true
  fi
}

trap cleanup EXIT INT TERM

for _ in $(seq 1 30); do
  if mountpoint -q "${MOUNT_DIR}"; then
    break
  fi
  sleep 1
done

if ! mountpoint -q "${MOUNT_DIR}"; then
  echo "gcsfuse did not mount at ${MOUNT_DIR}." >&2
  exit 1
fi

exec uvicorn modules.api_service:app --host 0.0.0.0 --port "${API_PORT}"
