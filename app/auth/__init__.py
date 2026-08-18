"""Authentication and project-scoped authorisation."""

from app.auth.dependencies import (
    AUTHORISATION_MARKER,
    UNSCOPED_ROUTES,
    AuthenticationNotConfigured,
    authenticate,
    require_action,
    require_project_access,
    require_role,
)
from app.auth.roles import PERMISSIONS, Action, Principal, Role, validate_permissions

__all__ = [
    "AUTHORISATION_MARKER",
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
    "validate_permissions",
]
