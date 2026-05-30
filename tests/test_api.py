import os
import pytest
import tempfile
import shutil
import asyncio
from fastapi.testclient import TestClient

# Mock watchfiles globally BEFORE vexindex imports it
import watchfiles
import watchfiles.main

watcher_call_count = 0
watcher_should_fail = False

async def dummy_awatch(*args, **kwargs):
    global watcher_call_count, watcher_should_fail
    watcher_call_count += 1
    if watcher_should_fail and watcher_call_count == 1:
        raise ValueError("Simulated watchfiles exception")
    yield set()
    try:
        while True:
            await asyncio.sleep(0.01)
    except asyncio.CancelledError:
        raise

watchfiles.awatch = dummy_awatch
watchfiles.main.awatch = dummy_awatch

from vexindex.config import settings

# Configure temporary database and vector paths for testing
original_db_path = settings.VEXINDEX_DB_PATH
original_vector_path = settings.VEXINDEX_VECTOR_PATH
temp_dir = tempfile.mkdtemp()
test_db_path = os.path.join(temp_dir, "test_index.db")
test_vector_path = os.path.join(temp_dir, "test_vectors")
settings.VEXINDEX_DB_PATH = test_db_path
settings.VEXINDEX_VECTOR_PATH = test_vector_path

from vexindex.main import app

@pytest.fixture(scope="module", autouse=True)
def cleanup():
    yield
    from vexindex.vector import vector_store
    if hasattr(vector_store, "_client") and vector_store._client is not None:
        try:
            vector_store._client.close()
        except Exception:
            pass
        vector_store._client = None
    settings.VEXINDEX_DB_PATH = original_db_path
    settings.VEXINDEX_VECTOR_PATH = original_vector_path
    # Cleanup the temp directory
    shutil.rmtree(temp_dir, ignore_errors=True)

import pytest_asyncio

@pytest_asyncio.fixture
async def client():
    async with app.router.lifespan_context(app):
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            yield c


@pytest.mark.asyncio
async def test_project_lifecycle(client):
    # Create a temporary directory to act as a codebase project
    with tempfile.TemporaryDirectory() as project_dir:
        # Create a sample python file in the directory
        sample_file = os.path.join(project_dir, "app.py")
        with open(sample_file, "w", encoding="utf-8") as f:
            f.write(
                "class DBClient:\n"
                "    def connect(self):\n"
                "        return True\n"
            )
            
        from vexindex.vector import vector_store
        from unittest.mock import patch, AsyncMock
        vector_store._client = None

        mock_embed = patch("vexindex.vector.vector_store.embed_text", new_callable=AsyncMock, return_value=[0.1]*768)
        mock_upsert = patch("vexindex.vector.vector_store.upsert_chunks", new_callable=AsyncMock, return_value=None)
        mock_delete = patch("vexindex.vector.vector_store.delete_chunks_for_file", new_callable=AsyncMock, return_value=None)
        mock_search_vec = patch("vexindex.vector.vector_store.search_vectors", new_callable=AsyncMock, return_value=[])

        with mock_embed, mock_upsert, mock_delete, mock_search_vec:
            # 1. Register project
            response = await client.post(
                "/projects",
                json={"name": "Test project", "root_path": project_dir}
            )
            assert response.status_code == 201
            data = response.json()
            assert "id" in data
            assert data["name"] == "Test project"
            project_id = data["id"]
            
            # Wait for background task indexer to complete (yield to the event loop)
            for _ in range(20):
                status_response = await client.get(f"/index/status/{project_id}")
                assert status_response.status_code == 200
                status_data = status_response.json()
                assert status_data["project_id"] == project_id
                if status_data["status"] == "idle":
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("Indexing task did not finish in time")
            
            # 3. List projects
            list_response = await client.get("/projects")
            assert list_response.status_code == 200
            projects = list_response.json()
            assert len(projects) > 0
            assert any(p["id"] == project_id for p in projects)
            
            # 4. Search
            search_response = await client.post(
                "/search",
                json={"query": "connect", "project_id": project_id}
            )
            assert search_response.status_code == 200
            results = search_response.json()
            assert len(results) > 0
            assert results[0]["chunk_type"] == "function"
            assert results[0]["name"] == "connect"
            
            # 5. Delete project
            delete_response = await client.delete(f"/projects/{project_id}")
            assert delete_response.status_code == 200
            
            # Verify deleted
            verify_response = await client.get("/projects")
            assert verify_response.status_code == 200
            projects_after = verify_response.json()
            assert not any(p["id"] == project_id for p in projects_after)


@pytest.mark.asyncio
async def test_search_endpoint_alpha(client):
    from unittest.mock import patch, AsyncMock
    mock_results = [{
        "chunk_id": "test-id",
        "file_path": "test.py",
        "start_line": 1,
        "end_line": 2,
        "chunk_type": "class",
        "name": "Test",
        "content": "class Test",
        "rank": 0.9
    }]
    with patch("vexindex.main.search_hybrid", new=AsyncMock(return_value=mock_results)) as mock_search:
        # Test default / no alpha
        response = await client.post("/search", json={"query": "test_query", "project_id": "some-id"})
        assert response.status_code == 200
        mock_search.assert_called_once_with(
            conn=app.state.db,
            query="test_query",
            project_id="some-id",
            limit=10,
            alpha=None
        )
        
        mock_search.reset_mock()
        
        # Test with custom alpha
        response = await client.post("/search", json={"query": "test_query", "project_id": "some-id", "alpha": 0.45, "limit": 5})
        assert response.status_code == 200
        mock_search.assert_called_once_with(
            conn=app.state.db,
            query="test_query",
            project_id="some-id",
            limit=5,
            alpha=0.45
        )


@pytest.mark.asyncio
async def test_fts5_sanitization():
    import aiosqlite
    from vexindex.db import init_db, search_fts, insert_chunk
    import tempfile
    import os
    
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "test.db")
        conn = await init_db(db_path)
        try:
            # Insert a project and a file
            await conn.execute(
                "INSERT INTO projects (id, name, root_path) VALUES (?, ?, ?)",
                ("proj-1", "Test", temp_dir)
            )
            await conn.execute(
                "INSERT INTO files (id, project_id, path, content_hash) VALUES (?, ?, ?, ?)",
                ("file-1", "proj-1", "app.py", "hash1")
            )
            # Insert chunk via insert_chunk helper (safely inserts into chunks and chunks_fts)
            await insert_chunk(
                conn=conn,
                chunk_id="chunk-1",
                file_id="file-1",
                start_line=1,
                end_line=3,
                chunk_type="function",
                name="hello",
                tokens=10,
                content="def hello(self):\n    print('error')",
                project_id="proj-1",
                file_path="app.py"
            )
            
            # Test empty query: should return [] safely
            res_empty = await search_fts(conn, "   ")
            assert res_empty == []
            
            # Test syntax/punctuation queries: should not raise exception, should match tokenized parts
            res_punctuation = await search_fts(conn, "def hello(self):")
            assert len(res_punctuation) > 0
            assert res_punctuation[0]["chunk_id"] == "chunk-1"
            
            # Query with only punctuation: should return [] safely
            res_only_punctuation = await search_fts(conn, "():-.,\"'/\\")
            assert res_only_punctuation == []
            
        finally:
            await conn.close()


@pytest.mark.asyncio
async def test_watcher_retry_on_exception():
    global watcher_call_count, watcher_should_fail
    watcher_call_count = 0
    watcher_should_fail = True
    
    from unittest.mock import AsyncMock, patch
    from vexindex.watcher import watch_project, get_watcher_status
    
    original_sleep = asyncio.sleep
    async def mock_sleep_side_effect(delay):
        await original_sleep(min(delay, 0.001))
    
    # Mock asyncio.sleep so we don't actually wait in tests
    with patch("vexindex.watcher.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_sleep.side_effect = mock_sleep_side_effect
        conn_mock = AsyncMock()
        with tempfile.TemporaryDirectory() as watch_dir:
            # Start watch_project in a task
            task = asyncio.create_task(watch_project("test-project", watch_dir, conn_mock))
            try:
                # Give it a small event loop turn to run
                await asyncio.sleep(0.05)
                
                # Verify that an error occurred, leading to a sleep retry call
                assert watcher_call_count >= 1
                assert any(call[0][0] == 1.0 for call in mock_sleep.call_args_list)
                
                # Verify status reflects failure
                w_status = get_watcher_status("test-project")
                assert w_status["status"] == "failed"
                assert "Simulated watchfiles exception" in w_status["error"]
            finally:
                # Cancel task to clean up
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                watcher_should_fail = False







