import pytest
import os
import shutil
import aiosqlite
from unittest.mock import patch, AsyncMock

from vexindex.config import settings

# Override config to use a temporary DB and temporary vector path during tests
TEST_DB_PATH = "./test_index.db"
TEST_VECTOR_PATH = "./test_vectors"

settings.VEXINDEX_DB_PATH = TEST_DB_PATH
settings.VEXINDEX_VECTOR_PATH = TEST_VECTOR_PATH

from vexindex.vector import vector_store
from vexindex.db import init_db, insert_project, upsert_file, insert_chunk, search_hybrid

@pytest.fixture(autouse=True)
def setup_teardown():
    vector_store._client = None
    # Clean up test files before test
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    if os.path.exists(TEST_VECTOR_PATH):
        shutil.rmtree(TEST_VECTOR_PATH)
        
    yield
    
    # Clean up test files after test
    vector_store._client = None
    if os.path.exists(TEST_DB_PATH):
        try:
            os.remove(TEST_DB_PATH)
        except Exception:
            pass
    if os.path.exists(TEST_VECTOR_PATH):
        try:
            shutil.rmtree(TEST_VECTOR_PATH)
        except Exception:
            pass

@pytest.mark.asyncio
async def test_vector_store_operations():
    # Mock Ollama response
    from unittest.mock import MagicMock
    response_mock = MagicMock()
    response_mock.status_code = 200
    response_mock.json.return_value = {"embeddings": [[0.1] * 768]}
    
    async_post_mock = AsyncMock(return_value=response_mock)
    
    with patch("httpx.AsyncClient.post", new=async_post_mock):
        embedding = await vector_store.embed_text("test content")
        assert len(embedding) == 768
        assert embedding[0] == 0.1
        
        # Test upsert
        project_id = "test-proj-uuid"
        file_path = "/path/to/test_file.py"
        chunk_id = "c4ea487a-3605-4c07-9008-017e8c339ff0"
        
        chunks_data = [{
            "chunk_id": chunk_id,
            "embedding": embedding,
            "content": "def test_hello():\n    pass"
        }]
        
        await vector_store.upsert_chunks(project_id, file_path, chunks_data)
        
        # Test search
        results = await vector_store.search_vectors(embedding, project_id, limit=5)
        assert len(results) == 1
        assert results[0]["chunk_id"] == chunk_id
        assert results[0]["file_path"] == file_path
        
        # Test delete file chunks
        await vector_store.delete_chunks_for_file(project_id, file_path)
        results = await vector_store.search_vectors(embedding, project_id, limit=5)
        assert len(results) == 0

@pytest.mark.asyncio
async def test_hybrid_search_rrf():
    conn = await init_db(TEST_DB_PATH)
    try:
        project_id = "project-rrf-uuid"
        file_id = "file-rrf-uuid"
        file_path = "/project/src/main.py"
        
        await insert_project(conn, project_id, "Test Project", "/project")
        await upsert_file(conn, file_id, project_id, file_path, "some-hash")
        
        # Insert two chunks
        chunk1_id = "c1111111-1111-1111-1111-111111111111"
        chunk2_id = "c2222222-2222-2222-2222-222222222222"
        
        await insert_chunk(
            conn, chunk1_id, file_id, 1, 10, "function", "hello", 10,
            "def hello():\n    print('hello world')", project_id, file_path
        )
        await insert_chunk(
            conn, chunk2_id, file_id, 11, 20, "function", "greet", 10,
            "def greet(name):\n    print('hello', name)", project_id, file_path
        )
        
        # Mock vector store search to return chunk2 as best match and chunk1 as second best match
        # FTS5 search for "hello" will return chunk1 as best match and chunk2 as second best match
        mock_embed = AsyncMock(return_value=[0.1]*768)
        mock_search_vec = AsyncMock(return_value=[
            {"chunk_id": chunk2_id, "file_path": file_path, "content": "def greet(name):\n    print('hello', name)", "score": 0.9},
            {"chunk_id": chunk1_id, "file_path": file_path, "content": "def hello():\n    print('hello world')", "score": 0.8}
        ])
        
        with patch("vexindex.vector.vector_store.embed_text", new=mock_embed), \
             patch("vexindex.vector.vector_store.search_vectors", new=mock_search_vec):
            
            results = await search_hybrid(conn, "hello", project_id, limit=5)
            # RRF score combining:
            # chunk1: rank 0 in FTS5, rank 1 in Vector -> Score = 1/(60+0) + 1/(60+1) = 1/60 + 1/61 = 0.01666 + 0.01639 = 0.03305
            # chunk2: rank 1 in FTS5, rank 0 in Vector -> Score = 1/(60+1) + 1/(60+0) = 1/61 + 1/60 = 0.03305
            # They should be returned as top hits
            assert len(results) == 2
            assert {r["chunk_id"] for r in results} == {chunk1_id, chunk2_id}
            
    finally:
        await conn.close()
