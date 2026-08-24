"""Which cache view a turn resolves, and which tier lands in each one.

Two rules carry the whole design here. The delivery view (``dir``) must hold
the user tier only, because the reconciler is the sole writer of workspace
skill dirs and the generic managed upload would fight it; and the two views
must live in separate scope namespaces, because each scope's resolve deletes
its own siblings.

The scope *choice* is the third rule: a workspace whose effective user tier is
the plain user tier reuses the plain user view rather than forking a private
copy of identical bytes, so most workspaces cause no extra materialization.
"""

import pytest

from src.server.services.user_skills import materialize


def _row(
    name: str,
    *,
    workspace_id: str | None = None,
    content: str = "c",
    enabled: bool = True,
) -> dict:
    return {
        "name": name,
        "description": f"{name} skill",
        "content_hash": f"sha256:{content}",
        "confirmed": True,
        "enabled": enabled,
        "workspace_id": workspace_id,
        "user_skill_id": f"id-{name}-{workspace_id or 'user'}",
        "command": None,
    }


@pytest.fixture
def resolved(monkeypatch):
    """Record every ``resolve_user_skill_dir`` call as ``{scope: [names]}``."""
    seen: dict[str, list[str]] = {}

    async def _resolve(user_id, rows, *, scope="user"):
        seen[scope] = [r["name"] for r in rows]
        return (f"/cache/{scope}" if rows else None), rows

    monkeypatch.setattr(materialize, "resolve_user_skill_dir", _resolve)
    return seen


@pytest.fixture
def db(monkeypatch):
    """Stub the reads ``load_user_skill_bundle`` makes.

    ``rows`` is the enabled set; ``ws_rows`` is every workspace-scoped row
    including the disabled ones, which is a distinct read because shadowing is
    by name rather than by enabled state. It defaults to the enabled workspace
    rows so a test that does not care about disables says nothing about it.
    """
    state: dict = {"rows": [], "ws_rows": None, "ws_disabled": set()}

    async def _rows(user_id, workspace_id=None):
        return state["rows"]

    async def _all_ws_rows(user_id, workspace_id=None):
        if state["ws_rows"] is not None:
            return state["ws_rows"]
        return [r for r in state["rows"] if r["workspace_id"] == workspace_id]

    async def _ws_disabled(workspace_id):
        return state["ws_disabled"]

    async def _empty_set(user_id):
        return set()

    async def _empty_dict(user_id):
        return {}

    monkeypatch.setattr(materialize, "list_enabled_user_skills", _rows)
    monkeypatch.setattr(materialize, "list_user_skills", _all_ws_rows)
    monkeypatch.setattr(materialize, "list_workspace_skill_disables", _ws_disabled)
    monkeypatch.setattr(materialize, "get_disabled_builtin_skills", _empty_set)
    monkeypatch.setattr(materialize, "get_skill_command_overrides", _empty_dict)
    return state


@pytest.mark.asyncio
async def test_user_tier_only_resolves_the_plain_user_scope(db, resolved):
    db["rows"] = [_row("alpha"), _row("beta")]

    bundle = await materialize.load_user_skill_bundle("u-1")

    assert resolved["user"] == ["alpha", "beta"]
    assert bundle.dir == "/cache/user"
    assert bundle.workspace_dir is None
    assert [s.name for s in bundle.skills] == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_workspace_rows_stay_out_of_the_delivery_view(db, resolved):
    db["rows"] = [_row("alpha"), _row("ws-only", workspace_id="ws-1")]

    bundle = await materialize.load_user_skill_bundle("u-1", "ws-1")

    assert resolved["user"] == ["alpha"], "delivery view is the user tier only"
    assert resolved[materialize._own_scope_key("ws-1")] == ["ws-only"]
    assert bundle.dir == "/cache/user"
    assert bundle.workspace_dir is not None and bundle.workspace_dir != bundle.dir
    # Both tiers are still one effective set for the manifest and slash menu.
    assert {s.name for s in bundle.skills} == {"alpha", "ws-only"}
    assert {s.name for s in bundle.skills if s.workspace_scoped} == {"ws-only"}


@pytest.mark.asyncio
async def test_untouched_user_tier_reuses_the_shared_user_view(db, resolved):
    """No shadowing and no disables: the workspace's physical view is byte for
    byte the plain user view, so it must not fork a private scope."""
    db["rows"] = [_row("alpha"), _row("beta"), _row("ws-only", workspace_id="ws-1")]

    await materialize.load_user_skill_bundle("u-1", "ws-1")

    assert resolved["user"] == ["alpha", "beta"]
    assert materialize._scope_key("ws-1") not in resolved


@pytest.mark.asyncio
async def test_a_shadowed_user_skill_forks_a_workspace_scope(db, resolved):
    """The workspace row hides the same-named user row, so this workspace's
    delivery view is genuinely different from every other workspace's."""
    db["rows"] = [_row("alpha"), _row("alpha", workspace_id="ws-1", content="d")]

    bundle = await materialize.load_user_skill_bundle("u-1", "ws-1")

    ws_scope = materialize._scope_key("ws-1")
    assert resolved[ws_scope] == [], "the shadowed user row leaves the view"
    assert "user" not in resolved
    assert resolved[materialize._own_scope_key("ws-1")] == ["alpha"]
    # One effective entry, and it is the workspace copy.
    assert [(s.name, s.workspace_scoped) for s in bundle.skills] == [("alpha", True)]


@pytest.mark.asyncio
async def test_a_disabled_workspace_row_still_shadows_its_user_twin(db, resolved):
    """Shadowing is by name, not by enabled state.

    If disabling the workspace copy promoted the user-tier row into the
    delivery view, the asset sync would write those bytes over a dir the
    reconciler owns, and the next pass would put the workspace row's content
    back. Turning a workspace skill off turns that name off in the workspace.
    """
    db["rows"] = [_row("alpha"), _row("beta")]
    db["ws_rows"] = [_row("alpha", workspace_id="ws-1", content="d", enabled=False)]

    bundle = await materialize.load_user_skill_bundle("u-1", "ws-1")

    ws_scope = materialize._scope_key("ws-1")
    assert resolved[ws_scope] == ["beta"], "the user-tier alpha stays shadowed"
    assert "user" not in resolved, "the plain user view would deliver alpha"
    assert [s.name for s in bundle.skills] == ["beta"]


@pytest.mark.asyncio
async def test_a_workspace_disable_forks_a_workspace_scope(db, resolved):
    db["rows"] = [_row("alpha"), _row("beta")]
    db["ws_disabled"] = {"alpha"}

    bundle = await materialize.load_user_skill_bundle("u-1", "ws-1")

    assert resolved[materialize._scope_key("ws-1")] == ["beta"]
    assert "user" not in resolved
    assert [s.name for s in bundle.skills] == ["beta"]
    assert "alpha" in bundle.disabled_builtins


@pytest.mark.asyncio
async def test_a_row_whose_archive_is_unfetchable_drops_from_the_manifest(
    db, monkeypatch
):
    """A skill with no body on disk must not advertise a slash trigger that
    would resolve to nothing."""
    db["rows"] = [_row("alpha"), _row("broken")]

    async def _resolve(user_id, rows, *, scope="user"):
        kept = [r for r in rows if r["name"] != "broken"]
        return (f"/cache/{scope}" if kept else None), kept

    monkeypatch.setattr(materialize, "resolve_user_skill_dir", _resolve)

    bundle = await materialize.load_user_skill_bundle("u-1")

    assert [s.name for s in bundle.skills] == ["alpha"]


class TestScopeNamespaces:
    def test_delivery_and_own_scopes_never_collide(self):
        """They GC their own siblings, so sharing a namespace would make each
        resolve delete the other's view."""
        assert materialize._own_scope_key("ws-1") != materialize._scope_key("ws-1")
        assert materialize._own_scope_key(None) != materialize._scope_key(None)

    def test_scopes_are_per_workspace(self):
        assert materialize._scope_key("ws-1") != materialize._scope_key("ws-2")
        assert materialize._scope_key(None) == "user"

    def test_view_hash_is_content_addressed_and_order_free(self):
        a, b = _row("alpha"), _row("beta", content="d")
        assert materialize._view_hash([a, b]) == materialize._view_hash([b, a])
        assert materialize._view_hash([a, b]) != materialize._view_hash([a])
        assert materialize._view_hash([a]) != materialize._view_hash(
            [_row("alpha", content="changed")]
        )
