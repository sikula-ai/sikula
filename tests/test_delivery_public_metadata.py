from core.delivery_public_metadata import (
    REDACTED_DELIVERY_PUBLIC_METADATA,
    is_safe_delivery_public_metadata,
    project_delivery_public_identity,
    sanitize_delivery_public_metadata,
)


def test_delivery_public_metadata_accepts_urls_and_relative_labels() -> None:
    for value in (
        "Integrate https://api.example.test/users",
        "Integrate http://api.example.test/users",
        "Consume the GET /api/v1/resource endpoint without changing that contract.",
        "Create POST /orders",
        "Implement GET /users",
        "Expose PATCH /v1/files/{id}",
        "Add GET /countries/population-stats",
        "Expose GET /etc/passwd as a compatibility route",
        "Implement GET /Users as an API route",
        "Handle POST /home",
        "Keep endpoint /api/v1/resource stable",
        "Keep /orders route stable",
        "Use v1/users",
        "Open café/menu",
    ):
        assert is_safe_delivery_public_metadata(value)
        assert sanitize_delivery_public_metadata(value) == value


def test_delivery_public_metadata_rejects_private_paths_and_control_characters() -> None:
    for value in (
        "Read /Users/example/private/task.md",
        "source=/Users/example/private/task.md",
        "Read //server/private/task.md",
        "source=//server/private/task.md",
        "Implement GET //server/private/task.md",
        "Read /api/v1/resource from disk",
        "Call GET /api/v1/resource?source=/Users/alice/private",
        "/etc/passwd",
        r"Read C:\Users\example\private\task.md",
        r"root:C:\Users\example\private\task.md",
        r"Read \\server\private\task.md",
        r"Read \Users\example\private\task.md",
        r"root:\Users\example\private\task.md",
        "source=file:///Users/example/private/task.md",
        "source=file://server/private/task.md",
        "source=FILE:///Users/example/private/task.md",
        "Injected\nline",
        "Injected\u009bline",
        "Spoofed\u202etxt.exe",
        "Invalid\ud800surrogate",
        "x" * 1001,
    ):
        assert not is_safe_delivery_public_metadata(value)
        assert sanitize_delivery_public_metadata(value) == REDACTED_DELIVERY_PUBLIC_METADATA


def test_delivery_public_identity_projection_is_stable_and_correlation_safe() -> None:
    first = "/Users/example/private/unit"
    second = r"C:\Users\example\private\unit"

    projected_first = project_delivery_public_identity(first)
    projected_second = project_delivery_public_identity(second)

    assert projected_first == project_delivery_public_identity(first)
    assert projected_first != projected_second
    assert projected_first is not None and projected_first.startswith("<redacted:")
    assert projected_second is not None and projected_second.startswith("<redacted:")
    projected_literal = project_delivery_public_identity(projected_first)
    assert projected_literal != projected_first
    assert projected_literal == project_delivery_public_identity(projected_first)
    assert first not in projected_first
    assert second not in projected_second
    assert project_delivery_public_identity("safe-unit") == "safe-unit"
    assert project_delivery_public_identity("GET /users") == "GET /users"
    assert project_delivery_public_identity("\ud800") is not None
