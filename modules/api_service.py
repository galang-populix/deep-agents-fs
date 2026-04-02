"""FastAPI service exposing canonical Deep Agents filesystem operations."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
import inspect
import logging
from pathlib import Path
from typing import Any

from deepagents.backends.filesystem import FilesystemBackend
from fastapi import Depends, FastAPI, File, Form, Header, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from modules.helper import (
    build_download_zip,
    call_duration_ms,
    call_start_time,
    parse_upload_payload,
    response_envelope,
    scope_download_paths,
    scope_paths_for_method,
    validate_tenant_id,
)
from modules.settings import SETTINGS, ExperimentSettings


logger = logging.getLogger(__name__)


LEGACY_METHOD_MAP: dict[str, str] = {
    "ls": "ls_info",
    "als": "als_info",
    "glob": "glob_info",
    "aglob": "aglob_info",
    "grep": "grep_raw",
    "agrep": "agrep_raw",
}


class PathRequest(BaseModel):
    """Request body containing one optional path."""

    path: str = "/"


class ReadRequest(BaseModel):
    """Request body for read and aread operations."""

    file_path: str
    offset: int = 0
    limit: int = 2000


class GrepRequest(BaseModel):
    """Request body for grep and agrep operations."""

    pattern: str
    path: str | None = None
    glob: str | None = None


class GlobRequest(BaseModel):
    """Request body for glob and aglob operations."""

    pattern: str
    path: str = "/"


class WriteRequest(BaseModel):
    """Request body for write and awrite operations."""

    file_path: str
    content: str


class EditRequest(BaseModel):
    """Request body for edit and aedit operations."""

    file_path: str
    old_string: str
    new_string: str
    replace_all: bool = False


class DownloadRequest(BaseModel):
    """Request body for download_files and adownload_files operations."""

    paths: list[str] = Field(default_factory=list)


class BackendContainer:
    """Container for backend instance and immutable settings."""

    def __init__(self, settings: ExperimentSettings) -> None:
        """Initialize backend container with virtual-mode isolation.

        Args:
            settings: Runtime experiment settings used to build backend state.

        Returns:
            None.
        """
        self.settings = settings
        self.settings.mount_dir.mkdir(parents=True, exist_ok=True)
        self.backend = FilesystemBackend(
            root_dir=self.settings.mount_dir,
            virtual_mode=True,
        )


def _serialize_for_info(value: Any) -> Any:
    """Serialize settings and dataclass fields for JSON responses.

    Args:
        value: Arbitrary object produced by backend info serialization.

    Returns:
        JSON-serializable representation of ``value``.
    """
    if is_dataclass(value):
        return _serialize_for_info(asdict(value))
    if isinstance(value, dict):
        return {key: _serialize_for_info(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize_for_info(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


async def _invoke_backend(
    container: BackendContainer,
    method_name: str,
    payload: dict[str, Any],
) -> tuple[Any, float, str | None]:
    """Execute one backend method with timing and exception capture.

    Args:
        container: Runtime backend container.
        method_name: Canonical method name requested by API endpoint.
        payload: Method keyword arguments after tenant path scoping.

    Returns:
        Tuple of ``(result, duration_ms, unexpected_error)``.
    """
    resolved_name = method_name
    if not hasattr(container.backend, resolved_name):
        resolved_name = LEGACY_METHOD_MAP.get(method_name, method_name)
    if not hasattr(container.backend, resolved_name):
        return None, 0.0, f"Backend does not implement '{method_name}'."

    backend_method = getattr(container.backend, resolved_name)
    started = call_start_time()
    try:
        if inspect.iscoroutinefunction(backend_method):
            result = await backend_method(**payload)
        else:
            result = await asyncio.to_thread(backend_method, **payload)

        if resolved_name != method_name:
            result = _normalize_legacy_result(method_name=method_name, value=result)
        return result, call_duration_ms(started), None
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected backend error on %s", method_name)
        return None, call_duration_ms(started), str(exc)


def _normalize_legacy_result(method_name: str, value: Any) -> Any:
    """Normalize legacy Deep Agents method outputs into canonical-like structures.

    Args:
        method_name: Canonical method name expected by API clients.
        value: Raw return value from legacy compatibility method.

    Returns:
        Canonical-like result shape that matches current endpoint contracts.
    """
    if method_name in {"ls", "als"}:
        return {"entries": value, "error": None}
    if method_name in {"glob", "aglob"}:
        return {"matches": value, "error": None}
    if method_name in {"grep", "agrep"}:
        if isinstance(value, str):
            return {"matches": [], "error": value}
        return {"matches": value, "error": None}
    return value


def get_container(request: Request) -> BackendContainer:
    """Resolve runtime backend container from app state.

    Args:
        request: Incoming FastAPI request object.

    Returns:
        Active :class:`BackendContainer` stored in app state.
    """
    return request.app.state.container


async def _handle_json_method(
    *,
    method_name: str,
    payload: dict[str, Any],
    tenant_header: str,
    container: BackendContainer,
) -> JSONResponse:
    """Run one JSON method and return a normalized response envelope.

    Args:
        method_name: Canonical backend method to invoke.
        payload: JSON payload already validated by the endpoint model.
        tenant_header: Raw ``X-Tenant-ID`` header value.
        container: Runtime backend container.

    Returns:
        JSON response containing the normalized envelope.
    """
    tenant_id = validate_tenant_id(
        tenant_id=tenant_header,
        pattern=container.settings.tenant_id_regex,
    )
    scoped_payload = scope_paths_for_method(
        method_name=method_name,
        payload=payload,
        tenant_id=tenant_id,
    )
    result, duration_ms, unexpected_error = await _invoke_backend(
        container=container,
        method_name=method_name,
        payload=scoped_payload,
    )
    envelope = response_envelope(
        backend_name=container.settings.backend_name,
        method_name=method_name,
        tenant_id=tenant_id,
        duration_ms=duration_ms,
        result=result,
        error=unexpected_error,
    )
    return JSONResponse(content=envelope)


async def _handle_upload_method(
    *,
    method_name: str,
    paths: list[str],
    files: list[UploadFile],
    tenant_header: str,
    container: BackendContainer,
) -> JSONResponse:
    """Run upload method using multipart form payload.

    Args:
        method_name: Upload method name (sync or async variant).
        paths: Multipart destination path values.
        files: Multipart uploaded files.
        tenant_header: Raw ``X-Tenant-ID`` header value.
        container: Runtime backend container.

    Returns:
        JSON response containing normalized upload results.
    """
    tenant_id = validate_tenant_id(
        tenant_id=tenant_header,
        pattern=container.settings.tenant_id_regex,
    )
    scoped_files = await parse_upload_payload(
        paths=paths,
        files=files,
        tenant_id=tenant_id,
        max_files=container.settings.max_upload_files,
    )
    result, duration_ms, unexpected_error = await _invoke_backend(
        container=container,
        method_name=method_name,
        payload={"files": scoped_files},
    )
    envelope = response_envelope(
        backend_name=container.settings.backend_name,
        method_name=method_name,
        tenant_id=tenant_id,
        duration_ms=duration_ms,
        result=result,
        error=unexpected_error,
    )
    return JSONResponse(content=envelope)


async def _handle_download_method(
    *,
    method_name: str,
    payload: DownloadRequest,
    tenant_header: str,
    container: BackendContainer,
) -> Response:
    """Run download method and return zipped files plus metadata.

    Args:
        method_name: Download method name (sync or async variant).
        payload: Download request body with source paths.
        tenant_header: Raw ``X-Tenant-ID`` header value.
        container: Runtime backend container.

    Returns:
        ZIP streaming response on success, or JSON error response on failure.
    """
    tenant_id = validate_tenant_id(
        tenant_id=tenant_header,
        pattern=container.settings.tenant_id_regex,
    )
    scoped_paths = scope_download_paths(payload.paths, tenant_id)
    result, duration_ms, unexpected_error = await _invoke_backend(
        container=container,
        method_name=method_name,
        payload={"paths": scoped_paths},
    )
    if unexpected_error is not None:
        envelope = response_envelope(
            backend_name=container.settings.backend_name,
            method_name=method_name,
            tenant_id=tenant_id,
            duration_ms=duration_ms,
            result=None,
            error=unexpected_error,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=envelope,
        )

    archive_bytes = build_download_zip(
        responses=result or [],
        backend_name=container.settings.backend_name,
        method_name=method_name,
        tenant_id=tenant_id,
        duration_ms=duration_ms,
    )
    filename = f"{container.settings.backend_name}-{tenant_id}-{method_name}.zip"
    return Response(
        content=archive_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Backend": container.settings.backend_name,
            "X-Method": method_name,
            "X-Tenant-ID": tenant_id,
        },
    )


def create_app(settings: ExperimentSettings | None = None) -> FastAPI:
    """Create the FastAPI app configured for one filesystem backend.

    Args:
        settings: Optional explicit runtime settings override.

    Returns:
        Configured FastAPI application instance.
    """
    runtime_settings = settings or SETTINGS

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Initialize and attach runtime container during app lifespan.

        Args:
            app: FastAPI application instance.

        Returns:
            Async context manager lifecycle control.
        """
        app.state.container = BackendContainer(runtime_settings)
        yield

    app = FastAPI(
        title="Deep Agents Filesystem Experiment API",
        version="0.1.0",
        description=(
            "Internal PoC API wrapper around FilesystemBackend for local and "
            "gcsfuse mount comparison."
        ),
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health(
        container: BackendContainer = Depends(get_container),
    ) -> dict[str, Any]:
        """Return service health and backend identity.

        Args:
            container: Runtime backend container dependency.

        Returns:
            Health payload containing status, backend, and mount directory.
        """
        return {
            "status": "ok",
            "backend": container.settings.backend_name,
            "mount_dir": str(container.settings.mount_dir),
        }

    @app.get("/v1/backend_info")
    async def backend_info(
        container: BackendContainer = Depends(get_container),
    ) -> dict[str, Any]:
        """Return backend and runtime settings used by this service.

        Args:
            container: Runtime backend container dependency.

        Returns:
            Metadata describing backend capabilities and active settings.
        """
        return {
            "backend": container.settings.backend_name,
            "backend_class": container.backend.__class__.__name__,
            "mount_dir": str(container.settings.mount_dir),
            "virtual_mode": True,
            "tenant_root_prefix": "/tenants/{tenant_id}",
            "supported_methods": container.settings.fs_methods,
            "async_methods": container.settings.async_methods,
            "gcsfuse_flags": container.settings.gcsfuse_flags,
            "settings": _serialize_for_info(container.settings),
        }

    @app.post("/v1/fs/ls")
    async def fs_ls(
        payload: PathRequest,
        x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
        container: BackendContainer = Depends(get_container),
    ) -> JSONResponse:
        """Handle ``ls`` endpoint call.

        Args:
            payload: Request body containing target path.
            x_tenant_id: Tenant header value.
            container: Runtime backend container dependency.

        Returns:
            JSON response envelope for ``ls``.
        """
        return await _handle_json_method(
            method_name="ls",
            payload=payload.model_dump(exclude_none=True),
            tenant_header=x_tenant_id,
            container=container,
        )

    @app.post("/v1/fs/als")
    async def fs_als(
        payload: PathRequest,
        x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
        container: BackendContainer = Depends(get_container),
    ) -> JSONResponse:
        """Handle ``als`` endpoint call.

        Args:
            payload: Request body containing target path.
            x_tenant_id: Tenant header value.
            container: Runtime backend container dependency.

        Returns:
            JSON response envelope for ``als``.
        """
        return await _handle_json_method(
            method_name="als",
            payload=payload.model_dump(exclude_none=True),
            tenant_header=x_tenant_id,
            container=container,
        )

    @app.post("/v1/fs/read")
    async def fs_read(
        payload: ReadRequest,
        x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
        container: BackendContainer = Depends(get_container),
    ) -> JSONResponse:
        """Handle ``read`` endpoint call.

        Args:
            payload: Request body containing file path and paging fields.
            x_tenant_id: Tenant header value.
            container: Runtime backend container dependency.

        Returns:
            JSON response envelope for ``read``.
        """
        return await _handle_json_method(
            method_name="read",
            payload=payload.model_dump(exclude_none=True),
            tenant_header=x_tenant_id,
            container=container,
        )

    @app.post("/v1/fs/aread")
    async def fs_aread(
        payload: ReadRequest,
        x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
        container: BackendContainer = Depends(get_container),
    ) -> JSONResponse:
        """Handle ``aread`` endpoint call.

        Args:
            payload: Request body containing file path and paging fields.
            x_tenant_id: Tenant header value.
            container: Runtime backend container dependency.

        Returns:
            JSON response envelope for ``aread``.
        """
        return await _handle_json_method(
            method_name="aread",
            payload=payload.model_dump(exclude_none=True),
            tenant_header=x_tenant_id,
            container=container,
        )

    @app.post("/v1/fs/grep")
    async def fs_grep(
        payload: GrepRequest,
        x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
        container: BackendContainer = Depends(get_container),
    ) -> JSONResponse:
        """Handle ``grep`` endpoint call.

        Args:
            payload: Request body containing search pattern and optional path filters.
            x_tenant_id: Tenant header value.
            container: Runtime backend container dependency.

        Returns:
            JSON response envelope for ``grep``.
        """
        return await _handle_json_method(
            method_name="grep",
            payload=payload.model_dump(exclude_none=True),
            tenant_header=x_tenant_id,
            container=container,
        )

    @app.post("/v1/fs/agrep")
    async def fs_agrep(
        payload: GrepRequest,
        x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
        container: BackendContainer = Depends(get_container),
    ) -> JSONResponse:
        """Handle ``agrep`` endpoint call.

        Args:
            payload: Request body containing search pattern and optional path filters.
            x_tenant_id: Tenant header value.
            container: Runtime backend container dependency.

        Returns:
            JSON response envelope for ``agrep``.
        """
        return await _handle_json_method(
            method_name="agrep",
            payload=payload.model_dump(exclude_none=True),
            tenant_header=x_tenant_id,
            container=container,
        )

    @app.post("/v1/fs/glob")
    async def fs_glob(
        payload: GlobRequest,
        x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
        container: BackendContainer = Depends(get_container),
    ) -> JSONResponse:
        """Handle ``glob`` endpoint call.

        Args:
            payload: Request body containing glob pattern and base path.
            x_tenant_id: Tenant header value.
            container: Runtime backend container dependency.

        Returns:
            JSON response envelope for ``glob``.
        """
        return await _handle_json_method(
            method_name="glob",
            payload=payload.model_dump(exclude_none=True),
            tenant_header=x_tenant_id,
            container=container,
        )

    @app.post("/v1/fs/aglob")
    async def fs_aglob(
        payload: GlobRequest,
        x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
        container: BackendContainer = Depends(get_container),
    ) -> JSONResponse:
        """Handle ``aglob`` endpoint call.

        Args:
            payload: Request body containing glob pattern and base path.
            x_tenant_id: Tenant header value.
            container: Runtime backend container dependency.

        Returns:
            JSON response envelope for ``aglob``.
        """
        return await _handle_json_method(
            method_name="aglob",
            payload=payload.model_dump(exclude_none=True),
            tenant_header=x_tenant_id,
            container=container,
        )

    @app.post("/v1/fs/write")
    async def fs_write(
        payload: WriteRequest,
        x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
        container: BackendContainer = Depends(get_container),
    ) -> JSONResponse:
        """Handle ``write`` endpoint call.

        Args:
            payload: Request body containing target file path and content.
            x_tenant_id: Tenant header value.
            container: Runtime backend container dependency.

        Returns:
            JSON response envelope for ``write``.
        """
        return await _handle_json_method(
            method_name="write",
            payload=payload.model_dump(exclude_none=True),
            tenant_header=x_tenant_id,
            container=container,
        )

    @app.post("/v1/fs/awrite")
    async def fs_awrite(
        payload: WriteRequest,
        x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
        container: BackendContainer = Depends(get_container),
    ) -> JSONResponse:
        """Handle ``awrite`` endpoint call.

        Args:
            payload: Request body containing target file path and content.
            x_tenant_id: Tenant header value.
            container: Runtime backend container dependency.

        Returns:
            JSON response envelope for ``awrite``.
        """
        return await _handle_json_method(
            method_name="awrite",
            payload=payload.model_dump(exclude_none=True),
            tenant_header=x_tenant_id,
            container=container,
        )

    @app.post("/v1/fs/edit")
    async def fs_edit(
        payload: EditRequest,
        x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
        container: BackendContainer = Depends(get_container),
    ) -> JSONResponse:
        """Handle ``edit`` endpoint call.

        Args:
            payload: Request body containing edit replacement instructions.
            x_tenant_id: Tenant header value.
            container: Runtime backend container dependency.

        Returns:
            JSON response envelope for ``edit``.
        """
        return await _handle_json_method(
            method_name="edit",
            payload=payload.model_dump(exclude_none=True),
            tenant_header=x_tenant_id,
            container=container,
        )

    @app.post("/v1/fs/aedit")
    async def fs_aedit(
        payload: EditRequest,
        x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
        container: BackendContainer = Depends(get_container),
    ) -> JSONResponse:
        """Handle ``aedit`` endpoint call.

        Args:
            payload: Request body containing edit replacement instructions.
            x_tenant_id: Tenant header value.
            container: Runtime backend container dependency.

        Returns:
            JSON response envelope for ``aedit``.
        """
        return await _handle_json_method(
            method_name="aedit",
            payload=payload.model_dump(exclude_none=True),
            tenant_header=x_tenant_id,
            container=container,
        )

    @app.post("/v1/fs/upload_files")
    async def fs_upload_files(
        paths: list[str] = Form(...),
        files: list[UploadFile] = File(...),
        x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
        container: BackendContainer = Depends(get_container),
    ) -> JSONResponse:
        """Handle ``upload_files`` endpoint call.

        Args:
            paths: Multipart destination path list.
            files: Multipart file list.
            x_tenant_id: Tenant header value.
            container: Runtime backend container dependency.

        Returns:
            JSON response envelope for ``upload_files``.
        """
        return await _handle_upload_method(
            method_name="upload_files",
            paths=paths,
            files=files,
            tenant_header=x_tenant_id,
            container=container,
        )

    @app.post("/v1/fs/aupload_files")
    async def fs_aupload_files(
        paths: list[str] = Form(...),
        files: list[UploadFile] = File(...),
        x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
        container: BackendContainer = Depends(get_container),
    ) -> JSONResponse:
        """Handle ``aupload_files`` endpoint call.

        Args:
            paths: Multipart destination path list.
            files: Multipart file list.
            x_tenant_id: Tenant header value.
            container: Runtime backend container dependency.

        Returns:
            JSON response envelope for ``aupload_files``.
        """
        return await _handle_upload_method(
            method_name="aupload_files",
            paths=paths,
            files=files,
            tenant_header=x_tenant_id,
            container=container,
        )

    @app.post("/v1/fs/download_files")
    async def fs_download_files(
        payload: DownloadRequest,
        x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
        container: BackendContainer = Depends(get_container),
    ) -> Response:
        """Handle ``download_files`` endpoint call.

        Args:
            payload: Request body containing paths to download.
            x_tenant_id: Tenant header value.
            container: Runtime backend container dependency.

        Returns:
            ZIP response stream with metadata payload.
        """
        return await _handle_download_method(
            method_name="download_files",
            payload=payload,
            tenant_header=x_tenant_id,
            container=container,
        )

    @app.post("/v1/fs/adownload_files")
    async def fs_adownload_files(
        payload: DownloadRequest,
        x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
        container: BackendContainer = Depends(get_container),
    ) -> Response:
        """Handle ``adownload_files`` endpoint call.

        Args:
            payload: Request body containing paths to download.
            x_tenant_id: Tenant header value.
            container: Runtime backend container dependency.

        Returns:
            ZIP response stream with metadata payload.
        """
        return await _handle_download_method(
            method_name="adownload_files",
            payload=payload,
            tenant_header=x_tenant_id,
            container=container,
        )

    return app


def run() -> None:
    """Run the app with Uvicorn using module settings.

    Args:
        None.

    Returns:
        None.
    """
    import uvicorn

    uvicorn.run(
        "modules.api_service:app",
        host=SETTINGS.host,
        port=SETTINGS.port,
        reload=False,
    )


app = create_app()
