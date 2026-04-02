# Deep Agents Filesystem: Local vs GCS Fuse Experiment

Internal PoC that compares Deep Agents `FilesystemBackend` behavior across:
- Local filesystem mount (`mnt/local`)
- Cloud Storage FUSE mount (`mnt/gcsfuse`)

The project provides:
- A dual-container API wrapper exposing canonical backend methods.
- Tenant-isolated path scoping (`/tenants/{tenant_id}/...`) with `virtual_mode=True`.
- A notebook-driven benchmark and comparison workflow.

## Important Note

This project is for **internal experimentation only**.
The Deep Agents documentation warns that `FilesystemBackend` is not recommended for internet-facing HTTP APIs due to filesystem exposure risk.

## Project Layout

- `modules/settings.py`: central constants and runtime settings.
- `modules/helper.py`: tenancy-safe path mapping, payload shaping, timing, and zip helpers.
- `modules/api_service.py`: FastAPI wrapper for canonical filesystem methods.
- `docker/Dockerfile.local`: local backend API image.
- `docker/Dockerfile.gcsfuse`: gcsfuse backend API image.
- `docker/entrypoint.gcsfuse.sh`: mount + API startup entrypoint for gcsfuse.
- `docker-compose.yml`: orchestrates local and gcsfuse services.
- `main-experiments.ipynb`: notebook client for objectives and benchmarking.

## Setup

1. Create environment file:
```bash
cp .env.example .env
```
2. Fill `.env` values:
- `SA_PATH_JSON`: host path to service-account JSON for GCS (file path recommended).
- `PROJECT_ID`: GCP project.
- `BUCKET_NAME`: bucket used by gcsfuse.
- Optional API and benchmark overrides.

If `SA_PATH_JSON` points to a directory, the gcsfuse entrypoint tries to use the
first `*.json` file found in that directory.

3. Install dependencies for local notebook/tests:
```bash
uv sync --extra dev --extra notebook
```

## Run with Docker Compose

```bash
docker compose up --build
```

Services:
- Local API: `http://localhost:8080`
- GCS Fuse API: `http://localhost:8081`

Health checks:
```bash
curl http://localhost:8080/health
curl http://localhost:8081/health
```

## API Surface

### JSON endpoints
- `POST /v1/fs/ls`
- `POST /v1/fs/als`
- `POST /v1/fs/read`
- `POST /v1/fs/aread`
- `POST /v1/fs/grep`
- `POST /v1/fs/agrep`
- `POST /v1/fs/glob`
- `POST /v1/fs/aglob`
- `POST /v1/fs/write`
- `POST /v1/fs/awrite`
- `POST /v1/fs/edit`
- `POST /v1/fs/aedit`

### Multipart upload endpoints
- `POST /v1/fs/upload_files`
- `POST /v1/fs/aupload_files`

### Zip-stream download endpoints
- `POST /v1/fs/download_files`
- `POST /v1/fs/adownload_files`

Shared requirements:
- Header: `X-Tenant-ID: <tenant>`
- All paths are automatically remapped into `/tenants/{tenant_id}/...`.
- Canonical endpoints remain stable even when the installed Deep Agents package
  still exposes legacy backend method names (`ls_info`, `glob_info`, `grep_raw`).

## Notebook Workflow

Open `main-experiments.ipynb` and execute sections in order:
1. Multi-tenant behavior and concurrent request checks.
2. Canonical method applicability matrix (local vs gcsfuse).
3. Balanced benchmark suite with CSV exports.

CSV outputs are saved under `artifacts/results/<timestamp>/`.

## Known Limitations (Expected)

Based on Cloud Storage FUSE semantics, compared with local POSIX filesystems:
- Metadata and listing latency can be higher on object storage-backed mounts.
- Concurrent updates and visibility timing may differ from strict local filesystem expectations.
- Some workflows can show degraded behavior under high contention or list-heavy patterns.

These differences are intentionally surfaced and measured in the notebook results.
