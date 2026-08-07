"""Generic sandbox egress relay — grants, sandbox auth, and streaming passthrough.

Sandboxes never hold vendor credentials; they dial ``/v1/egress/{grant_id}``
with a workspace-scoped relay JWT and the relay attaches the credential
host-side per request. MCP OAuth connections are the v1 grant producer; the
resolver layer is the extension point for further credential kinds.
"""
