import pytest
from vexindex.indexer import chunk_python, chunk_js_ts, chunk_sliding_window, count_tokens

def test_chunk_python():
    source = (
        "class MyClass:\n"
        "    def method_one(self):\n"
        "        pass\n"
        "\n"
        "def helper_func():\n"
        "    return 42\n"
    )
    
    chunks = chunk_python(source)
    
    # We expect 3 chunks: MyClass, method_one, and helper_func
    names = [c["name"] for c in chunks]
    assert "MyClass" in names
    assert "method_one" in names
    assert "helper_func" in names
    
    # Verify line ranges
    my_class_chunk = next(c for c in chunks if c["name"] == "MyClass")
    assert my_class_chunk["start_line"] == 1
    assert my_class_chunk["chunk_type"] == "class"
    
    helper_chunk = next(c for c in chunks if c["name"] == "helper_func")
    assert helper_chunk["start_line"] == 5
    assert helper_chunk["chunk_type"] == "function"

def test_chunk_js():
    source = (
        "class Animal {\n"
        "    speak() {\n"
        "        console.log('woof');\n"
        "    }\n"
        "}\n"
        "function makeNoise() {}\n"
        "const arrowFunc = () => { return true; };\n"
    )
    
    chunks = chunk_js_ts(source, "javascript")
    names = [c["name"] for c in chunks]
    
    assert "Animal" in names
    assert "speak" in names
    assert "makeNoise" in names
    assert "arrowFunc" in names
    
    animal_chunk = next(c for c in chunks if c["name"] == "Animal")
    assert animal_chunk["chunk_type"] == "class"
    
    arrow_chunk = next(c for c in chunks if c["name"] == "arrowFunc")
    assert arrow_chunk["chunk_type"] == "function"

def test_chunk_sliding_window():
    # Generate 120 lines
    lines = [f"Line {i}" for i in range(1, 121)]
    # Put empty lines to test boundary splitting
    lines[49] = ""  # index 49 is Line 50
    lines[99] = ""  # index 99 is Line 100
    
    chunks = chunk_sliding_window(lines)
    
    # With max_lines = 50 and overlap = 10, we expect around 3 chunks
    assert len(chunks) >= 3
    for c in chunks:
        assert c["chunk_type"] == "block"
        assert c["name"] is None
        assert len(c["content"].splitlines()) <= 55

def test_count_tokens():
    text = "import os\nfrom datetime import datetime"
    tokens = count_tokens(text)
    assert tokens > 0


def test_is_sensitive_file():
    from vexindex.indexer import is_sensitive_file
    assert is_sensitive_file(".env") is True
    assert is_sensitive_file(".env.production") is True
    assert is_sensitive_file("id_rsa") is True
    assert is_sensitive_file("id_ed25519") is True
    assert is_sensitive_file("server.key") is True
    assert is_sensitive_file("server.pem") is True
    assert is_sensitive_file("secrets.json") is True
    assert is_sensitive_file("app_credentials.yaml") is True
    assert is_sensitive_file("auth_token.txt") is True
    
    assert is_sensitive_file("main.py") is False
    assert is_sensitive_file("utils.js") is False
    assert is_sensitive_file("secrets_not_really.py") is False


@pytest.mark.asyncio
async def test_index_single_file_skips():
    import os
    import tempfile
    from vexindex.db import init_db
    from vexindex.indexer import index_single_file
    from vexindex.config import settings

    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "test.db")
        conn = await init_db(db_path)
        try:
            project_id = "test-proj"
            await conn.execute(
                "INSERT INTO projects (id, name, root_path) VALUES (?, ?, ?)",
                (project_id, "Test Project", temp_dir)
            )
            await conn.commit()
            
            # Test sensitive file skip
            sensitive_file = os.path.join(temp_dir, ".env")
            with open(sensitive_file, "w") as f:
                f.write("SECRET_KEY=123456\n")
            
            indexed = await index_single_file(conn, project_id, sensitive_file)
            assert indexed is False
            
            # Test large file skip
            large_file = os.path.join(temp_dir, "large.py")
            with open(large_file, "w") as f:
                f.write("a" * (60 * 1024))  # 60KB
                
            original_limit = settings.VEXINDEX_MAX_FILE_SIZE_KB
            settings.VEXINDEX_MAX_FILE_SIZE_KB = 50  # Limit is 50KB
            try:
                indexed = await index_single_file(conn, project_id, large_file)
                assert indexed is False
            finally:
                settings.VEXINDEX_MAX_FILE_SIZE_KB = original_limit
                
            # Test ignored extension skip
            ignored_file = os.path.join(temp_dir, "image.png")
            with open(ignored_file, "w") as f:
                f.write("dummy image data")
            indexed = await index_single_file(conn, project_id, ignored_file)
            assert indexed is False
            
        finally:
            await conn.close()

