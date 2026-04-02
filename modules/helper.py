"""Helper functions for tenant-safe backend calls and experiment results."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from io import BytesIO
import json
from pathlib import PurePosixPath
import re
import time
from typing import Any
import zipfile

from fastapi import HTTPException, UploadFile, status


METHOD_PATH_FIELDS: dict[str, tuple[str, ...]] = {
    "ls": ("path",),
    "als": ("path",),
    "read": ("file_path",),
    "aread": ("file_path",),
    "grep": ("path",),
    "agrep": ("path",),
    "glob": ("path",),
    "aglob": ("path",),
    "write": ("file_path",),
    "awrite": ("file_path",),
    "edit": ("file_path",),
    "aedit": ("file_path",),
}


def validate_tenant_id(tenant_id: str, pattern: str) -> str:
    """Validate and normalize tenant ID for per-tenant path isolation.

    Args:
        tenant_id: Raw tenant identifier from request headers.
        pattern: Regular expression pattern used for tenant validation.

    Returns:
        Lowercased and trimmed tenant identifier.

    Raises:
        HTTPException: If the tenant ID is empty or does not match the pattern.
    """
    normalized = tenant_id.strip().lower()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tenant ID cannot be empty.",
        )
    if not re.match(pattern, normalized):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Tenant ID is invalid. Expected lowercase alphanumeric, underscore, "
                "or hyphen, and 2-32 chars."
            ),
        )
    return normalized


def normalize_virtual_path(raw_path: str | None, *, default: str = "/") -> str:
    """Normalize user path input into a safe virtual path under the backend root.

    Args:
        raw_path: Optional user-provided path that may be relative or absolute.
        default: Fallback path used when ``raw_path`` is empty or missing.

    Returns:
        A normalized POSIX virtual path.

    Raises:
        HTTPException: If the path contains invalid separators or traversal tokens.
    """
    if raw_path is None:
        return default

    path = raw_path.strip()
    if not path:
        return default

    if "\\" in path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Path '{raw_path}' is invalid; use POSIX-style '/' separators.",
        )

    if not path.startswith("/"):
        path = f"/{path}"

    virtual_path = PurePosixPath(path)
    for part in virtual_path.parts:
        if part in {"..", "~"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Path '{raw_path}' includes a traversal token.",
            )

    normalized = "/" + "/".join(part for part in virtual_path.parts if part != "/")
    return normalized if normalized != "//" else "/"


def tenant_prefix(tenant_id: str) -> str:
    """Return the tenant root path used across all backend operations.

    Args:
        tenant_id: Validated tenant identifier.

    Returns:
        Tenant root path in ``/tenants/{tenant_id}`` format.
    """
    return f"/tenants/{tenant_id}"


def tenant_scoped_path(tenant_id: str, path: str | None) -> str:
    """Map a user-supplied path into the tenant-owned virtual path prefix.

    Args:
        tenant_id: Validated tenant identifier.
        path: User-supplied path to be scoped.

    Returns:
        Tenant-prefixed virtual path constrained to tenant scope.
    """
    normalized = normalize_virtual_path(path)
    base = tenant_prefix(tenant_id)
    if normalized == "/":
        return base
    return f"{base}{normalized}"


def scope_paths_for_method(method_name: str, payload: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    """Scope all path-like payload fields for a method under one tenant prefix.

    Args:
        method_name: Backend method name used to determine path field mapping.
        payload: Request payload before tenant scoping.
        tenant_id: Validated tenant identifier.

    Returns:
        Payload copy with all relevant path fields rewritten to tenant scope.
    """
    scoped = dict(payload)
    for field in METHOD_PATH_FIELDS.get(method_name, ()):
        if field not in scoped:
            continue
        scoped[field] = tenant_scoped_path(tenant_id, scoped[field])

    if method_name in {"grep", "agrep"} and "path" not in scoped:
        scoped["path"] = tenant_scoped_path(tenant_id, "/")
    if method_name in {"ls", "als", "glob", "aglob"} and "path" not in scoped:
        scoped["path"] = tenant_scoped_path(tenant_id, "/")

    return scoped


def scope_download_paths(paths: list[str], tenant_id: str) -> list[str]:
    """Apply tenant prefix to all paths used by download calls.

    Args:
        paths: Unscoped download paths from request payload.
        tenant_id: Validated tenant identifier.

    Returns:
        List of tenant-scoped download paths.
    """
    return [tenant_scoped_path(tenant_id, path) for path in paths]


async def parse_upload_payload(
    paths: list[str],
    files: list[UploadFile],
    tenant_id: str,
    max_files: int,
) -> list[tuple[str, bytes]]:
    """Build upload tuple payload while preserving path-file positional mapping.

    Args:
        paths: Destination paths submitted in multipart form data.
        files: Uploaded files submitted in multipart form data.
        tenant_id: Validated tenant identifier.
        max_files: Maximum allowed uploaded file count.

    Returns:
        List of ``(tenant_scoped_path, content_bytes)`` tuples.

    Raises:
        HTTPException: If payload is empty, mismatched, or exceeds file limit.
    """
    if not paths or not files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Multipart upload requires non-empty 'paths' and 'files'.",
        )
    if len(paths) != len(files):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The number of 'paths' must match the number of 'files'.",
        )
    if len(files) > max_files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Upload exceeds max files limit: {max_files}.",
        )

    payload: list[tuple[str, bytes]] = []
    for raw_path, upload in zip(paths, files, strict=True):
        content = await upload.read()
        payload.append((tenant_scoped_path(tenant_id, raw_path), content))
    return payload


def serialize_payload(value: Any) -> Any:
    """Convert dataclass-heavy backend responses into JSON-safe structures.

    Args:
        value: Arbitrary backend result payload.

    Returns:
        JSON-serializable representation of the original payload.
    """
    if value is None:
        return None
    if is_dataclass(value):
        return serialize_payload(asdict(value))
    if isinstance(value, list):
        return [serialize_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: serialize_payload(item) for key, item in value.items()}
    if isinstance(value, bytes):
        return {"bytes_len": len(value)}
    return value


def response_envelope(
    *,
    backend_name: str,
    method_name: str,
    tenant_id: str,
    duration_ms: float,
    result: Any,
    error: str | None,
) -> dict[str, Any]:
    """Construct a stable API response envelope for all JSON endpoints.

    Args:
        backend_name: Name of the active backend (for example ``local``).
        method_name: Called filesystem method name.
        tenant_id: Validated tenant identifier.
        duration_ms: End-to-end method execution duration in milliseconds.
        result: Raw backend result object.
        error: Optional unexpected error string.

    Returns:
        Normalized response envelope shared by all JSON endpoints.
    """
    return {
        "backend": backend_name,
        "method": method_name,
        "tenant_id": tenant_id,
        "duration_ms": round(duration_ms, 3),
        "result": serialize_payload(result),
        "error": error,
    }


def call_start_time() -> float:
    """Capture start time for backend execution timing.

    Args:
        None.

    Returns:
        Monotonic high-resolution timestamp from ``time.perf_counter``.
    """
    return time.perf_counter()


def call_duration_ms(start_time: float) -> float:
    """Compute elapsed wall time in milliseconds from a captured start time.

    Args:
        start_time: Timestamp previously captured by :func:`call_start_time`.

    Returns:
        Elapsed duration in milliseconds.
    """
    return (time.perf_counter() - start_time) * 1000.0


def build_download_zip(
    *,
    responses: list[Any],
    backend_name: str,
    method_name: str,
    tenant_id: str,
    duration_ms: float,
) -> bytes:
    """Package file download results as a zip archive plus metadata JSON.

    Args:
        responses: Backend download response objects.
        backend_name: Name of the active backend.
        method_name: Download method name that produced the payload.
        tenant_id: Validated tenant identifier.
        duration_ms: Method execution duration in milliseconds.

    Returns:
        Byte content of the generated ZIP archive.
    """
    stream = BytesIO()
    metadata: dict[str, Any] = {
        "backend": backend_name,
        "method": method_name,
        "tenant_id": tenant_id,
        "duration_ms": round(duration_ms, 3),
        "files": [],
    }

    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in responses:
            path = getattr(item, "path", "")
            error = getattr(item, "error", None)
            content = getattr(item, "content", None)
            included = error is None and content is not None

            metadata["files"].append(
                {
                    "path": path,
                    "error": error,
                    "included": included,
                    "size_bytes": len(content) if isinstance(content, (bytes, bytearray)) else 0,
                }
            )

            if included:
                arcname = path.lstrip("/") or "root"
                archive.writestr(arcname, content)

        archive.writestr(
            "_metadata.json",
            json.dumps(metadata, indent=2, sort_keys=True),
        )

    return stream.getvalue()
