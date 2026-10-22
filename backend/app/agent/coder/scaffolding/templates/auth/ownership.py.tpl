"""Server-assigned row ownership; clients never supply the scope."""

from fastapi import Depends


def install(app, services):
    if services.authenticated_user is None:
        raise RuntimeError("Ownership requires an authentication provider")

    def scope(user=Depends(services.authenticated_user)):
        return {"owner_id": user["id"]}

    services.scope_columns["owner_id"] = "INTEGER NOT NULL REFERENCES _auth_users(id)"
    services.record_scope = scope
