"""A duplicate must not attach before its file manifest is visible."""

import asyncio

import pytest

from src.server.database import workspace as workspaces
from src.server.database import workspace_file as files
from src.server.database.computer import create_computer

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


async def _source(user_id, pool):
    computer = await create_computer(user_id, kind="docker", name="Test computer")
    computer_id = str(computer["computer_id"])
    source = await workspaces.create_workspace_on_computer(user_id, "Source", computer_id)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO workspace_files (workspace_id, file_path, file_name, file_size, content_text) "
            "VALUES (%s, 'proof.txt', 'proof.txt', 5, 'proof')",
            (source["workspace_id"],),
        )
    return source, computer_id


async def test_duplicate_is_invisible_until_files_commit(seed_user, test_db_pool, monkeypatch):
    user_id = seed_user["user_id"]
    source, computer_id = await _source(user_id, test_db_pool)
    entered, release = asyncio.Event(), asyncio.Event()
    original = files.copy_workspace_files

    async def delayed_copy(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(files, "copy_workspace_files", delayed_copy)
    task = asyncio.create_task(workspaces.duplicate_workspace_on_computer(
        str(source["workspace_id"]), user_id, "Copy", computer_id,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with test_db_pool.connection() as conn:
            result = await conn.execute("SELECT workspace_id FROM workspaces WHERE name='Copy'")
            assert await result.fetchone() is None
    finally:
        release.set()
        duplicate = await task
    async with test_db_pool.connection() as conn:
        result = await conn.execute(
            "SELECT content_text FROM workspace_files WHERE workspace_id=%s",
            (duplicate["workspace_id"],),
        )
        assert (await result.fetchone())["content_text"] == "proof"


async def test_failed_copy_rolls_back_project(seed_user, test_db_pool, monkeypatch):
    source, computer_id = await _source(seed_user["user_id"], test_db_pool)

    async def fail(*args, **kwargs):
        raise RuntimeError("copy failed")

    monkeypatch.setattr(files, "copy_workspace_files", fail)
    with pytest.raises(RuntimeError, match="copy failed"):
        await workspaces.duplicate_workspace_on_computer(
            str(source["workspace_id"]), seed_user["user_id"], "Copy", computer_id,
        )
    async with test_db_pool.connection() as conn:
        result = await conn.execute("SELECT workspace_id FROM workspaces WHERE name='Copy'")
        assert await result.fetchone() is None


async def test_slug_retry_keeps_outer_copy_transaction_usable(seed_user, test_db_pool, monkeypatch):
    source, computer_id = await _source(seed_user["user_id"], test_db_pool)
    monkeypatch.setattr(workspaces, "workspace_dir_name", lambda *args, hex_chars:
                        source["dir_name"] if hex_chars == 4 else "copy-retried")
    duplicate = await workspaces.duplicate_workspace_on_computer(
        str(source["workspace_id"]), seed_user["user_id"], "Copy", computer_id,
    )
    assert duplicate["dir_name"] == "copy-retried"
    async with test_db_pool.connection() as conn:
        result = await conn.execute(
            "SELECT content_text FROM workspace_files WHERE workspace_id=%s",
            (duplicate["workspace_id"],),
        )
        assert (await result.fetchone())["content_text"] == "proof"
