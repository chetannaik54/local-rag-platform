"""Permissions and built-in roles (RBAC).

Every protected endpoint asks for one permission. Roles are just named sets of permissions:
four built-in roles (fixed, same for every organisation) plus custom roles an organisation admin can make.
"""

PERMISSIONS: dict[str, str] = {
    "chat.use": "Chat with assistants, give feedback, confirm actions",
    "documents.read": "See the list of documents",
    "documents.write": "Upload and delete documents",
    "settings.read": "View retrieval and model settings",
    "settings.write": "Change retrieval and model settings",
    "agents.manage": "Create, edit and delete assistants",
    "connectors.manage": "Manage folder and web-page connectors",
    "analytics.view": "View analytics and use management tools",
    "handoffs.handle": "Answer conversations handed to a human and manage canned replies",
    "users.manage": "Manage users, passwords and API keys",
    "roles.manage": "Create and edit custom roles",
    "embed.manage": "Manage website embed keys",
    "platform.manage": "Manage organizations and platform-wide settings (super admin only)",
}

ALL = frozenset(PERMISSIONS)
ORG_ALL = ALL - {"platform.manage"}

BUILTIN_ROLES: dict[str, dict] = {
    "super_admin": {"label": "Super admin", "permissions": ALL},
    "admin": {"label": "Admin", "permissions": ORG_ALL},
    "agent": {"label": "Support agent", "permissions": frozenset(
        {"chat.use", "documents.read", "settings.read", "handoffs.handle"})},
    "member": {"label": "Member", "permissions": frozenset({"chat.use", "documents.read"})},
}

# Anonymous website visitors using an embed key can only chat.
VISITOR_PERMISSIONS = frozenset({"chat.use"})

# legacy role names used by the first version of the API
LEGACY_ROLE_ALIASES = {"user": "member"}

# Assistants/tools may require a permission; these are the choices offered in the UI.
ASSISTANT_PERMISSION_CHOICES = ["chat.use", "handoffs.handle", "analytics.view", "users.manage"]
