"""Unit tests for tenancy and response helpers."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
import pytest

from modules.helper import (
    response_envelope,
    scope_paths_for_method,
    tenant_scoped_path,
    validate_tenant_id,
)


def test_validate_tenant_id_accepts_expected_pattern() -> None:
    """Tenant IDs that match policy should pass and normalize to lowercase.

    Args:
        None.

    Returns:
        None.
    """
    tenant_id = validate_tenant_id("Tenant_A", r"^[a-z0-9][a-z0-9_-]{1,31}$")
    assert tenant_id == "tenant_a"


def test_validate_tenant_id_rejects_invalid_value() -> None:
    """Tenant IDs with unsupported symbols must be rejected.

    Args:
        None.

    Returns:
        None.
    """
    with pytest.raises(HTTPException) as exc:
        validate_tenant_id("tenant/../x", r"^[a-z0-9][a-z0-9_-]{1,31}$")
    assert exc.value.status_code == 400


def test_tenant_scoped_path_maps_root_and_relative_paths() -> None:
    """Paths should always be remapped under the tenant prefix.

    Args:
        None.

    Returns:
        None.
    """
    assert tenant_scoped_path("tenant-a", "/") == "/tenants/tenant-a"
    assert tenant_scoped_path("tenant-a", "docs/file.txt") == "/tenants/tenant-a/docs/file.txt"


def test_tenant_scoped_path_rejects_traversal() -> None:
    """Traversal tokens should raise a validation error.

    Args:
        None.

    Returns:
        None.
    """
    with pytest.raises(HTTPException):
        tenant_scoped_path("tenant-a", "../secrets.txt")


def test_scope_paths_for_method_maps_file_path_field() -> None:
    """Path-like fields should be scoped based on method-specific mapping.

    Args:
        None.

    Returns:
        None.
    """
    scoped = scope_paths_for_method(
        method_name="write",
        payload={"file_path": "/notes.txt", "content": "x"},
        tenant_id="tenant-a",
    )
    assert scoped["file_path"] == "/tenants/tenant-a/notes.txt"
    assert scoped["content"] == "x"


@dataclass
class _Payload:
    value: int


def test_response_envelope_serializes_dataclass_result() -> None:
    """Dataclass results should be JSON-safe in envelope responses.

    Args:
        None.

    Returns:
        None.
    """
    envelope = response_envelope(
        backend_name="local",
        method_name="ls",
        tenant_id="tenant-a",
        duration_ms=12.3456,
        result=_Payload(value=7),
        error=None,
    )
    assert envelope["result"] == {"value": 7}
    assert envelope["duration_ms"] == 12.346
