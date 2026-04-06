# Deep Agents Filesystem Backend Experiment (Local vs GCS Fuse)

## Abstract
This report evaluates Deep Agents `FilesystemBackend` behavior through an API wrapper deployed on two storage backends: local POSIX mount (`/mnt/local`) and Cloud Storage FUSE mount (`/mnt/gcsfuse`). Analysis uses the executed notebook run and raw artifacts under `sample-artifacts/results/20260403_030213`. Both backends were healthy, tenant-scoped (`virtual_mode=True`), and achieved full canonical method coverage (`16/16 PASS`). Performance differed materially by workload: `read_latency` mean was `1.69x` slower on gcsfuse, `list_heavy_ls` mean was `33.53x` slower, and `write_throughput` scenario duration was `23.72x` slower. Conclusion: gcsfuse preserves functional compatibility but introduces strong latency and throughput penalties for list-heavy and write-heavy workloads.

## Introduction
This internal PoC compares identical API-level filesystem operations across local storage and gcsfuse-backed object storage to assess functional parity and operational tradeoffs for AI/data workloads. The experiment follows the repository workflow in `main-experiments.ipynb` and `README.md`.

Important context from project constraints:
- The service is for internal experimentation.
- `FilesystemBackend` is not intended for internet-facing HTTP APIs.

## Methods
### System Setup
- Services:
1. Local backend API: `http://localhost:8080`
2. GCS Fuse backend API: `http://localhost:8081`
- Tenant isolation model: all paths remapped to `/tenants/{tenant_id}/...`.
- Backend metadata target: `virtual_mode=True` on both services.

### Workload Definitions
Three benchmark scenarios were executed:
1. `read_latency`: repeated `read` on one seeded target file (`25` iterations).
2. `list_heavy_ls`: repeated `ls` over a directory pre-populated with `20` files (`25` iterations).
3. `write_throughput`: concurrent `write` requests at concurrency `{1, 5, 10, 20}`.

### Classification and Metrics
- Method matrix classification:
1. `PASS`: HTTP 200 with no top-level error and no method-level error.
2. `DEGRADED`: HTTP 200 with method-level error payload.
3. `FAIL`: non-200 or top-level failure.
- Benchmark metrics:
1. `latency_mean_ms`
2. `latency_p95_ms`
3. `latency_max_ms`
4. `throughput_ops_sec` (for write throughput rows)

### Data Provenance
Primary dataset (executed run): `sample-artifacts/results/20260403_030213/`
- `summary.csv`
- `throughput.csv`
- `latency.csv`
- `method_matrix.csv`
- `errors.csv`

Additional executed notebook outputs were used for health, tenancy-check, and concurrency-smoke tables because these specific tables are displayed in the notebook and not exported as standalone CSVs.

## Results
### R1. Health and Backend Metadata
Source: executed output table in `main-experiments.ipynb` Part 1.

| Backend | URL | Health | Mount Dir | virtual_mode | Tenant Prefix |
|---|---|---|---|---|---|
| local | http://localhost:8080 | ok | /mnt/local | True | /tenants/{tenant_id} |
| gcsfuse | http://localhost:8081 | ok | /mnt/gcsfuse | True | /tenants/{tenant_id} |

### R2. Canonical Method Applicability
Source: `sample-artifacts/results/20260403_030213/method_matrix.csv`, `errors.csv`.

- Local: `16/16 PASS`.
- GCS Fuse: `16/16 PASS`.
- `errors.csv` contains only header row (no non-PASS records persisted for this run).

### R3. Core Benchmark Summary
Source: `sample-artifacts/results/20260403_030213/summary.csv`.

| Scenario | Backend | Samples | OK % | Mean (ms) | P95 (ms) | Max (ms) |
|---|---:|---:|---:|---:|---:|---:|
| read_latency | local | 25 | 100.0 | 4.017 | 5.969 | 6.357 |
| read_latency | gcsfuse | 25 | 100.0 | 6.801 | 9.393 | 9.853 |
| list_heavy_ls | local | 25 | 100.0 | 6.87 | 8.889 | 13.168 |
| list_heavy_ls | gcsfuse | 25 | 100.0 | 230.356 | 304.717 | 721.434 |
| write_throughput | local | 4 | 100.0 | 37.019 | 91.369 | 101.784 |
| write_throughput | gcsfuse | 4 | 100.0 | 877.927 | 1275.503 | 1338.836 |

### R4. Effect Sizes (GCS Fuse relative to Local)
Computed from `summary.csv`:
- `read_latency` mean: `1.69x` slower (`6.801 / 4.017`).
- `list_heavy_ls` mean: `33.53x` slower (`230.356 / 6.87`).
- `write_throughput` scenario mean duration: `23.72x` slower (`877.927 / 37.019`).
- `list_heavy_ls` tail amplification: `p95 34.28x`, `max 54.79x`.

### R5. Write Throughput by Concurrency
Source: `sample-artifacts/results/20260403_030213/throughput.csv`.

| Concurrency | Local ops/s | GCS Fuse ops/s | GCS/Local | Local/GCS |
|---:|---:|---:|---:|---:|
| 1 | 269.820 | 2.095 | 0.00776 | 128.792 |
| 5 | 488.484 | 6.420 | 0.01314 | 76.088 |
| 10 | 309.111 | 10.910 | 0.03529 | 28.333 |
| 20 | 196.494 | 14.938 | 0.07602 | 13.154 |

Observation: gcsfuse throughput improves with concurrency but remains substantially below local across all tested levels.

### R6. Tenancy and Concurrency Observations
Source: executed output tables in `main-experiments.ipynb` Part 1.

Tenancy check table:
- All rows returned HTTP `200`.
- All rows were labeled `DEGRADED` (both backends).

Interpretation caveat:
- The notebook tenancy classification logic can under-report PASS for cross-tenant checks because it only marks PASS when a specific `result.error` shape contains `"not found"`.
- Write checks also target fixed paths (`/tenant-check/{tenant}.txt`), so repeated runs can trigger method-level `already exists` behavior and be marked `DEGRADED` even when isolation is intact.
- Therefore, these `DEGRADED` labels are not sufficient evidence of tenant-isolation failure.

Concurrency smoke table:
- All writes succeeded (`success_count == total`) for both backends at concurrency `5`, `10`, and `20`.
- Elapsed wall time (ms):
1. local: `13.368`, `23.861`, `39.182`
2. gcsfuse: `759.578`, `643.736`, `2049.034`

## Discussion
For AI/data pipeline workloads, backend choice should be aligned with access pattern:
- Read-dominant and low-frequency operations: gcsfuse may be acceptable when object-store-backed persistence is operationally required.
- List-heavy workflows (artifact discovery, recursive scans, metadata-intensive orchestration): local backend is strongly preferable due to large latency and tail penalties on gcsfuse.
- Write-heavy or bursty concurrent ingest: local backend provides much higher effective throughput and lower wall-clock completion time.

Functional compatibility was high (`16/16 PASS`), but performance parity was not.

## Threats to Validity
- Single-run analysis (`20260403_030213`) without repeated-trial confidence intervals.
- Potential warm-cache or prior-state effects across runs.
- Object-store and FUSE behavior may vary by environment and mount flags.
- Tenancy `DEGRADED` counts are sensitive to notebook response-shape assumptions and path reuse.

## Conclusion
In this executed experiment, the gcsfuse backend maintained API-level functional completeness but incurred substantial performance overhead versus local storage, especially for directory listing and concurrent writes. For latency-sensitive and throughput-sensitive AI workloads, local mount behavior is materially better. Gcsfuse is best treated as a compatibility/storage-integration option where higher latency is acceptable.

## Appendix A: Full Canonical Method Matrix (Executed Run)
Source: `sample-artifacts/results/20260403_030213/method_matrix.csv`.

| Method | Local status | Local ms | GCS status | GCS ms | GCS/Local |
|---|---:|---:|---:|---:|---:|
| adownload_files | PASS | 4.418 | PASS | 11.657 | 2.64x |
| aedit | PASS | 2.869 | PASS | 504.917 | 175.99x |
| aglob | PASS | 7.655 | PASS | 307.755 | 40.20x |
| agrep | PASS | 3.691 | PASS | 204.515 | 55.41x |
| als | PASS | 5.871 | PASS | 97.171 | 16.55x |
| aread | PASS | 5.095 | PASS | 102.859 | 20.19x |
| aupload_files | PASS | 5.489 | PASS | 505.842 | 92.16x |
| awrite | PASS | 5.725 | PASS | 617.330 | 107.83x |
| download_files | PASS | 4.140 | PASS | 308.591 | 74.54x |
| edit | PASS | 3.730 | PASS | 508.446 | 136.31x |
| glob | PASS | 3.865 | PASS | 209.985 | 54.33x |
| grep | PASS | 6.266 | PASS | 713.516 | 113.87x |
| ls | PASS | 5.038 | PASS | 115.726 | 22.97x |
| read | PASS | 4.792 | PASS | 99.612 | 20.79x |
| upload_files | PASS | 5.085 | PASS | 1061.255 | 208.70x |
| write | PASS | 3.724 | PASS | 614.421 | 164.99x |

## Appendix B: Artifact File Map
Dataset root: `sample-artifacts/results/20260403_030213/`
- `summary.csv`: aggregated scenario-level latency statistics.
- `latency.csv`: per-iteration latency for `read_latency` and `list_heavy_ls`.
- `throughput.csv`: write throughput and duration per concurrency level.
- `method_matrix.csv`: per-method applicability and latency by backend.
- `errors.csv`: subset of non-PASS method rows (empty in this run).
