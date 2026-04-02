"""Integration tests for the local FilesystemBackend API wrapper."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import io
import json
import zipfile

from fastapi.testclient import TestClient


def _post_json(
    client: TestClient,
    endpoint: str,
    payload: dict[str, object],
    headers: dict[str, str],
) -> dict[str, object]:
    """Send a JSON request and return JSON response.

    Args:
        client: FastAPI test client instance.
        endpoint: API endpoint path to call.
        payload: JSON payload sent in request body.
        headers: Request headers, including tenant ID.

    Returns:
        Parsed JSON response body.
    """
    response = client.post(endpoint, json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _extract_read_text(payload: dict[str, object]) -> str:
    """Extract text content from read/aread response across backend versions.

    Args:
        payload: Response envelope returned from read endpoints.

    Returns:
        Read text content, or an empty string when unavailable.
    """
    result = payload.get("result")
    if isinstance(result, dict):
        file_data = result.get("file_data")
        if isinstance(file_data, dict):
            content = file_data.get("content")
            if isinstance(content, str):
                return content
    if isinstance(result, str):
        return result
    return ""


def test_health_and_backend_info(client: TestClient) -> None:
    """Service should expose health and backend metadata endpoints.

    Args:
        client: FastAPI test client fixture.

    Returns:
        None.
    """
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    info = client.get("/v1/backend_info")
    assert info.status_code == 200
    data = info.json()
    assert data["backend"] == "test-local"
    assert data["backend_class"] == "FilesystemBackend"
    assert "write" in data["supported_methods"]


def test_canonical_json_methods_and_tenant_isolation(
    client: TestClient,
    tenant_header: dict[str, str],
    other_tenant_header: dict[str, str],
) -> None:
    """Canonical methods should work and remain tenant-isolated.

    Args:
        client: FastAPI test client fixture.
        tenant_header: Primary tenant header fixture.
        other_tenant_header: Secondary tenant header fixture.

    Returns:
        None.
    """
    _post_json(
        client,
        "/v1/fs/write",
        {"file_path": "/docs/notes.txt", "content": "alpha\nhello world"},
        tenant_header,
    )
    _post_json(
        client,
        "/v1/fs/awrite",
        {"file_path": "/docs/async.txt", "content": "async hello"},
        tenant_header,
    )

    read_sync = _post_json(
        client,
        "/v1/fs/read",
        {"file_path": "/docs/notes.txt", "offset": 0, "limit": 50},
        tenant_header,
    )
    assert "alpha" in _extract_read_text(read_sync)
    assert "hello world" in _extract_read_text(read_sync)

    read_async = _post_json(
        client,
        "/v1/fs/aread",
        {"file_path": "/docs/async.txt"},
        tenant_header,
    )
    assert "async hello" in _extract_read_text(read_async)

    ls_result = _post_json(client, "/v1/fs/ls", {"path": "/"}, tenant_header)
    assert any(entry["path"].startswith("/tenants/tenant-a/docs") for entry in ls_result["result"]["entries"])

    als_result = _post_json(client, "/v1/fs/als", {"path": "/docs"}, tenant_header)
    assert als_result["result"]["entries"]

    glob_result = _post_json(
        client,
        "/v1/fs/glob",
        {"pattern": "*.txt", "path": "/docs"},
        tenant_header,
    )
    assert len(glob_result["result"]["matches"]) >= 2

    aglob_result = _post_json(
        client,
        "/v1/fs/aglob",
        {"pattern": "*.txt", "path": "/docs"},
        tenant_header,
    )
    assert len(aglob_result["result"]["matches"]) >= 2

    grep_result = _post_json(
        client,
        "/v1/fs/grep",
        {"pattern": "hello", "path": "/docs", "glob": "*.txt"},
        tenant_header,
    )
    assert grep_result["result"]["matches"]

    agrep_result = _post_json(
        client,
        "/v1/fs/agrep",
        {"pattern": "hello", "path": "/docs", "glob": "*.txt"},
        tenant_header,
    )
    assert agrep_result["result"]["matches"]

    edit_result = _post_json(
        client,
        "/v1/fs/edit",
        {
            "file_path": "/docs/notes.txt",
            "old_string": "alpha",
            "new_string": "beta",
            "replace_all": False,
        },
        tenant_header,
    )
    assert edit_result["result"]["occurrences"] == 1

    aedit_result = _post_json(
        client,
        "/v1/fs/aedit",
        {
            "file_path": "/docs/async.txt",
            "old_string": "async",
            "new_string": "async-updated",
            "replace_all": False,
        },
        tenant_header,
    )
    assert aedit_result["result"]["occurrences"] == 1

    missing_for_other_tenant = _post_json(
        client,
        "/v1/fs/read",
        {"file_path": "/docs/notes.txt"},
        other_tenant_header,
    )
    assert "not found" in _extract_read_text(missing_for_other_tenant).lower()


def test_upload_download_and_metadata_zip(
    client: TestClient,
    tenant_header: dict[str, str],
) -> None:
    """Multipart upload and zip-stream download should preserve file payloads.

    Args:
        client: FastAPI test client fixture.
        tenant_header: Tenant header fixture.

    Returns:
        None.
    """
    response = client.post(
        "/v1/fs/upload_files",
        headers=tenant_header,
        data={"paths": ["/uploads/a.txt", "/uploads/b.txt"]},
        files=[
            ("files", ("a.txt", b"A content", "text/plain")),
            ("files", ("b.txt", b"B content", "text/plain")),
        ],
    )
    assert response.status_code == 200, response.text
    upload_data = response.json()
    assert upload_data["error"] is None
    assert len(upload_data["result"]) == 2

    download = client.post(
        "/v1/fs/download_files",
        headers=tenant_header,
        json={"paths": ["/uploads/a.txt", "/uploads/b.txt", "/uploads/missing.txt"]},
    )
    assert download.status_code == 200, download.text
    assert download.headers["content-type"].startswith("application/zip")

    with zipfile.ZipFile(io.BytesIO(download.content), "r") as archive:
        names = sorted(archive.namelist())
        assert "_metadata.json" in names
        assert "tenants/tenant-a/uploads/a.txt" in names
        assert "tenants/tenant-a/uploads/b.txt" in names
        metadata = json.loads(archive.read("_metadata.json").decode("utf-8"))
        assert metadata["method"] == "download_files"
        assert len(metadata["files"]) == 3
        assert any(item["error"] == "file_not_found" for item in metadata["files"])


def test_concurrency_smoke_same_and_cross_tenant(
    client: TestClient,
    tenant_header: dict[str, str],
    other_tenant_header: dict[str, str],
) -> None:
    """Concurrent writes should succeed and stay isolated across tenants.

    Args:
        client: FastAPI test client fixture.
        tenant_header: Primary tenant header fixture.
        other_tenant_header: Secondary tenant header fixture.

    Returns:
        None.
    """

    def write_for_tenant(index: int, headers: dict[str, str]) -> int:
        """Submit one tenant-scoped write request.

        Args:
            index: Integer suffix used for unique file naming.
            headers: Tenant-specific request headers.

        Returns:
            HTTP status code from the write request.
        """
        payload = {
            "file_path": f"/concurrency/item-{index}.txt",
            "content": f"payload-{index}",
        }
        response = client.post("/v1/fs/write", json=payload, headers=headers)
        return response.status_code

    with ThreadPoolExecutor(max_workers=12) as pool:
        statuses_a = list(pool.map(lambda i: write_for_tenant(i, tenant_header), range(0, 10)))
        statuses_b = list(pool.map(lambda i: write_for_tenant(i, other_tenant_header), range(10, 20)))

    assert all(code == 200 for code in statuses_a)
    assert all(code == 200 for code in statuses_b)

    ls_a = _post_json(client, "/v1/fs/ls", {"path": "/concurrency"}, tenant_header)
    ls_b = _post_json(client, "/v1/fs/ls", {"path": "/concurrency"}, other_tenant_header)
    assert len(ls_a["result"]["entries"]) == 10
    assert len(ls_b["result"]["entries"]) == 10
