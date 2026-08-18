"""Authentication and project-scoped authorisation."""

from app.auth.dependencies import (
    UNSCOPED_ROUTES,
    AuthenticationNotConfigured,
    authenticate,
    require_action,
    require_project_access,
    require_role,
)
from app.auth.roles import PERMISSIONS, Action, Principal, Role

__all__ = [
    "PERMISSIONS",
    "UNSCOPED_ROUTES",
    "Action",
    "AuthenticationNotConfigured",
    "Principal",
    "Role",
    "authenticate",
    "require_action",
    "require_project_access",
    "require_role",
]
