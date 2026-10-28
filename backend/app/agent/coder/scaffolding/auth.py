"""Opt-in local sessions and ownership through the shared composition hooks."""

from app.agent.coder.scaffolding.contracts import Contribution
from app.agent.coder.scaffolding.crud import asset, entity_case
from app.agent.coder.starters import FRONTENDS


def render_auth(spec):
    if spec.backend != "fastapi":
        raise ValueError(
            "Authentication requires the FastAPI backend; browser-only authentication is not supported"
        )
    if any(entity.name == "auth" for entity in spec.entities):
        raise ValueError(
            "The auth resource name is reserved for authentication endpoints"
        )
    result = Contribution(
        files={
            "backend/auth.py": asset("auth/backend.py.tpl"),
            "backend/tests/test_auth.py": asset("auth/test_backend.py.tpl").replace(
                "AUTH_CASES", repr([entity_case(entity) for entity in spec.entities])
            ),
        },
        backend_installers=("backend.auth.install",),
    )
    if spec.frontend != "none":
        framework = FRONTENDS[spec.frontend]["framework"]
        if framework not in {"react", "vanilla"}:
            raise ValueError(
                "Authentication needs an adapter for this frontend framework"
            )
        result.files["frontend/src/auth/session.ts"] = asset("auth/session.ts.tpl")
        result.files["frontend/src/auth/session.test.ts"] = asset(
            "auth/session.test.ts.tpl"
        )
        result.frontend_plugins = ("./auth/session",)
        if framework == "react":
            result.files["frontend/src/auth/Boundary.tsx"] = asset(
                "auth/Boundary.tsx.tpl"
            )
            result.files["frontend/src/auth/Boundary.test.tsx"] = asset(
                "auth/Boundary.test.tsx.tpl"
            )
            result.frontend_boundaries = ("./auth/Boundary",)
        else:
            result.files["frontend/src/auth/mount.ts"] = asset("auth/mount.ts.tpl")
            result.files["frontend/src/auth/mount.test.ts"] = asset(
                "auth/mount.test.ts.tpl"
            )
            result.frontend_mounts = ("./auth/mount",)
    return result


def render_ownership(spec):
    if spec.backend != "fastapi":
        raise ValueError("Record ownership requires server-side authentication")
    if any(
        field.name == "owner_id" for entity in spec.entities for field in entity.fields
    ):
        raise ValueError("owner_id is application-managed when ownership is enabled")
    return Contribution(
        files={"backend/ownership.py": asset("auth/ownership.py.tpl")},
        backend_installers=("backend.ownership.install",),
    )
