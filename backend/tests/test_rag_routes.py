"""
Tests for FastAPI route registration in the documents router.

The /documents/chunks/{chunk_id}/image route MUST be registered BEFORE
/documents/{document_id} so FastAPI matches it correctly. If the order
is wrong, a GET to /documents/chunks/abc/image would match the
/documents/{document_id} route with document_id="chunks" — which would
then fail UUID parsing and return a 400 instead of the image bytes.
"""

from app.api.documents import router


def test_chunks_image_route_is_registered_before_document_id_route():
    """The /documents/chunks/{chunk_id}/image route must come BEFORE
    /documents/{document_id} in the router's route list.

    FastAPI matches routes in registration order — more specific paths
    must be registered first, parameterized paths last.
    """
    routes = list(router.routes)
    paths = [getattr(r, "path", "") for r in routes]

    chunks_idx = paths.index("/documents/chunks/{chunk_id}/image")
    doc_id_idx = paths.index("/documents/{document_id}")

    assert chunks_idx < doc_id_idx, (
        f"Route /documents/chunks/{{chunk_id}}/image (index {chunks_idx}) "
        f"must be registered BEFORE /documents/{{document_id}} "
        f"(index {doc_id_idx}). Otherwise FastAPI matches 'chunks' as "
        f"a document_id and the image endpoint never gets hit."
    )


def test_chunks_image_route_is_get():
    """The /documents/chunks/{chunk_id}/image route must be a GET."""
    routes = list(router.routes)
    for r in routes:
        path = getattr(r, "path", "")
        if path == "/documents/chunks/{chunk_id}/image":
            methods = getattr(r, "methods", set())
            assert "GET" in methods, (
                f"Expected GET method on /documents/chunks/{{chunk_id}}/image, "
                f"got {methods}"
            )
            return
    raise AssertionError("Route /documents/chunks/{chunk_id}/image not found")


def test_documents_router_has_nine_routes():
    """The documents router should have exactly 9 routes after the
    realopen-ai17 changes (added /documents/chunks/{id}/image)."""
    routes = list(router.routes)
    assert len(routes) == 9, (
        f"Expected 9 routes in documents router, got {len(routes)}: "
        f"{[getattr(r, 'path', '') for r in routes]}"
    )


def test_all_expected_routes_exist():
    """All 9 expected routes must be present in the documents router."""
    routes = list(router.routes)
    paths = {getattr(r, "path", "") for r in routes}
    expected = {
        "/documents/upload",
        "/documents/upload/stream",
        "/documents",
        "/documents/chunks/{chunk_id}/image",
        "/documents/{document_id}",
        "/documents/{document_id}/download",
        "/documents/{document_id}",
        "/conversations/{conversation_id}/documents",
    }
    missing = expected - paths
    assert not missing, f"Missing routes: {missing}"
