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


def test_documents_router_has_fourteen_routes():
    """The documents router should have exactly 14 routes after the
    realopen-ai23 changes (Workspace Documents: thumbnail, pages,
    pages/{n}, reindex/stream, knowledge removal + collections PATCH)."""
    routes = list(router.routes)
    assert len(routes) == 14, (
        f"Expected 14 routes in documents router, got {len(routes)}: "
        f"{[getattr(r, 'path', '') for r in routes]}"
    )


def test_all_expected_routes_exist():
    """All expected routes must be present in the documents router."""
    routes = list(router.routes)
    paths = {getattr(r, "path", "") for r in routes}
    expected = {
        "/documents/upload",
        "/documents/upload/stream",
        "/documents",
        "/documents/chunks/{chunk_id}/image",
        "/documents/{document_id}",
        "/documents/{document_id}/download",
        "/documents/{document_id}/thumbnail",
        "/documents/{document_id}/pages",
        "/documents/{document_id}/pages/{page_number}",
        "/documents/{document_id}/reindex/stream",
        "/documents/{document_id}/knowledge",
        "/conversations/{conversation_id}/documents",
    }
    missing = expected - paths
    assert not missing, f"Missing routes: {missing}"


def test_preview_routes_methods():
    """The new preview / knowledge endpoints use the right HTTP verbs."""
    routes = list(router.routes)
    for r in routes:
        path = getattr(r, "path", "")
        methods = getattr(r, "methods", set())
        if path == "/documents/{document_id}/thumbnail":
            assert methods == {"GET"}
        elif path == "/documents/{document_id}/pages":
            assert methods == {"GET"}
        elif path == "/documents/{document_id}/pages/{page_number}":
            assert methods == {"GET"}
        elif path == "/documents/{document_id}/reindex/stream":
            assert methods == {"POST"}
        elif path == "/documents/{document_id}/knowledge":
            assert methods == {"DELETE"}


def test_pages_route_does_not_shadow_chunks_route():
    """/documents/{id}/pages/{page} must never capture the chunks image path.

    Both are 4-segment routes; FastAPI tries them in registration order.
    A GET /documents/chunks/{uuid}/image must resolve to the chunks route
    (registered earlier), never to /documents/{document_id}/pages/{page}
    with document_id="chunks".
    """
    routes = list(router.routes)
    paths = [getattr(r, "path", "") for r in routes]

    chunks_idx = paths.index("/documents/chunks/{chunk_id}/image")
    pages_idx = paths.index("/documents/{document_id}/pages/{page_number}")
    assert chunks_idx < pages_idx, (
        "/documents/chunks/{chunk_id}/image must be registered before "
        "/documents/{document_id}/pages/{page_number}"
    )
