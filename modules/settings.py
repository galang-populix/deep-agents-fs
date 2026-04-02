"""Central configuration for the Deep Agents filesystem experiment."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class ExperimentSettings:
    """Runtime settings used by the API service and benchmark notebook."""

    backend_name: str
    mount_dir: Path
    host: str
    port: int
    tenant_id_regex: str
    api_prefix: str
    fs_methods: tuple[str, ...]
    async_methods: tuple[str, ...]
    upload_methods: tuple[str, ...]
    download_methods: tuple[str, ...]
    benchmark_default_iterations: int
    benchmark_default_concurrency: tuple[int, ...]
    benchmark_default_payload_sizes_kb: tuple[int, ...]
    benchmark_default_tenants: tuple[str, ...]
    benchmark_output_dir: Path
    gcsfuse_flags: tuple[str, ...]
    max_upload_files: int

    @classmethod
    def from_env(cls) -> "ExperimentSettings":
        """Build settings from environment variables and safe defaults.

        Args:
            cls: ``ExperimentSettings`` class reference.

        Returns:
            Fully populated runtime settings dataclass.
        """
        backend_name = os.getenv("BACKEND_NAME", "local").strip().lower() or "local"

        if backend_name == "gcsfuse":
            fallback_mount = os.getenv("GCSFUSE_MOUNT_DIR", "mnt/gcsfuse")
        else:
            fallback_mount = os.getenv("LOCAL_MOUNT_DIR", "mnt/local")

        raw_mount = os.getenv("MOUNT_DIR", fallback_mount)
        mount_dir = Path(raw_mount).resolve()

        return cls(
            backend_name=backend_name,
            mount_dir=mount_dir,
            host=os.getenv("API_HOST", "0.0.0.0"),
            port=int(os.getenv("API_PORT", "8080")),
            tenant_id_regex=os.getenv(
                "TENANT_ID_REGEX",
                r"^[a-z0-9][a-z0-9_-]{1,31}$",
            ),
            api_prefix=os.getenv("API_PREFIX", "/v1/fs"),
            fs_methods=(
                "ls",
                "als",
                "read",
                "aread",
                "grep",
                "agrep",
                "glob",
                "aglob",
                "write",
                "awrite",
                "edit",
                "aedit",
                "upload_files",
                "aupload_files",
                "download_files",
                "adownload_files",
            ),
            async_methods=(
                "als",
                "aread",
                "agrep",
                "aglob",
                "awrite",
                "aedit",
                "aupload_files",
                "adownload_files",
            ),
            upload_methods=("upload_files", "aupload_files"),
            download_methods=("download_files", "adownload_files"),
            benchmark_default_iterations=int(os.getenv("BENCHMARK_ITERATIONS", "25")),
            benchmark_default_concurrency=(1, 5, 10, 20),
            benchmark_default_payload_sizes_kb=(1, 32, 128),
            benchmark_default_tenants=("tenant-a", "tenant-b", "tenant-c"),
            benchmark_output_dir=Path(
                os.getenv("BENCHMARK_OUTPUT_DIR", "artifacts/results")
            ).resolve(),
            gcsfuse_flags=tuple(
                flag
                for flag in os.getenv(
                    "GCSFUSE_MOUNT_FLAGS",
                    "--implicit-dirs --foreground",
                ).split(" ")
                if flag
            ),
            max_upload_files=int(os.getenv("MAX_UPLOAD_FILES", "200")),
        )


SETTINGS = ExperimentSettings.from_env()
