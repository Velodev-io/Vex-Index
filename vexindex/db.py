import os
import re
import logging
import aiosqlite
from typing import Optional, Any

logger = logging.getLogger(__name__)

async def init_db(db_path: str) -> aiosqlite.Connection:
    """
    Initializes the SQLite database, creates all schemas including FTS5 tables,
    and returns a connection.
    """
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    
    conn = await aiosqlite.connect(db_path)
    
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.execute("PRAGMA foreign_keys=ON")
    
    # 1. Projects Table
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            root_path TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_indexed TIMESTAMP
        )
    """)
    
    # 2. Files Table
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            last_indexed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            UNIQUE(project_id, path)
        )
    """)
    
    # 3. Chunks Table
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            chunk_type TEXT NOT NULL,
            name TEXT,
            tokens INTEGER NOT NULL,
            FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
        )
    """)
    
    # 4. FTS5 Virtual Table for fast search
    await conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            chunk_id,
            project_id UNINDEXED,
            file_path UNINDEXED,
            content,
            tokenize='porter unicode61'
        )
    """)
    
    # 5. Symbols Table
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS symbols (
            id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            scope TEXT,
            line INTEGER NOT NULL,
            chunk_id TEXT,
            FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE,
            FOREIGN KEY(chunk_id) REFERENCES chunks(id) ON DELETE SET NULL
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name)")

    # 6. Relations Table
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS relations (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            file_id TEXT NOT NULL,
            source_symbol TEXT,
            relation_type TEXT NOT NULL,
            target_symbol TEXT NOT NULL,
            line INTEGER NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(target_symbol)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(source_symbol)")
    
    await conn.commit()
    return conn

async def get_all_projects(conn: aiosqlite.Connection) -> list[dict]:
    conn.row_factory = aiosqlite.Row
    async with conn.execute("SELECT id, name, root_path, created_at, last_indexed FROM projects") as cursor:
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

async def get_project(conn: aiosqlite.Connection, project_id: str) -> Optional[dict]:
    conn.row_factory = aiosqlite.Row
    async with conn.execute("SELECT id, name, root_path, created_at, last_indexed FROM projects WHERE id = ?", (project_id,)) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None

async def insert_project(conn: aiosqlite.Connection, project_id: str, name: str, root_path: str):
    await conn.execute(
        "INSERT INTO projects (id, name, root_path) VALUES (?, ?, ?)",
        (project_id, name, root_path)
    )
    await conn.commit()

async def delete_project(conn: aiosqlite.Connection, project_id: str):
    from vexindex.vector import vector_store
    
    # 1. Delete all FTS entries for the project in one query
    await conn.execute("""
        DELETE FROM chunks_fts WHERE chunk_id IN (
            SELECT c.id FROM chunks c
            JOIN files f ON c.file_id = f.id
            WHERE f.project_id = ?
        )
    """, (project_id,))
    
    # Delete from Qdrant vector store
    try:
        await vector_store.delete_chunks_for_project(project_id)
    except Exception as e:
        logger.error(f"Failed to delete vectors for project {project_id}: {e}")
    
    # 2. Deleting the project will cascade delete files and chunks
    await conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    await conn.commit()

async def update_project_last_indexed(conn: aiosqlite.Connection, project_id: str, timestamp_str: str):
    await conn.execute("UPDATE projects SET last_indexed = ? WHERE id = ?", (timestamp_str, project_id))
    await conn.commit()

async def get_file_hash(conn: aiosqlite.Connection, project_id: str, path: str) -> Optional[str]:
    async with conn.execute("SELECT content_hash FROM files WHERE project_id = ? AND path = ?", (project_id, path)) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else None

async def upsert_file(conn: aiosqlite.Connection, file_id: str, project_id: str, path: str, content_hash: str):
    await conn.execute(
        """INSERT OR REPLACE INTO files (id, project_id, path, content_hash, last_indexed)
           VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)""",
        (file_id, project_id, path, content_hash)
    )
    await conn.commit()

async def delete_file(conn: aiosqlite.Connection, project_id: str, path: str):
    async with conn.execute("SELECT id FROM files WHERE project_id = ? AND path = ?", (project_id, path)) as cursor:
        row = await cursor.fetchone()
    if row:
        file_id = row[0]
        # 1. Delete FTS entries
        await conn.execute("""
            DELETE FROM chunks_fts WHERE chunk_id IN (
                SELECT id FROM chunks WHERE file_id = ?
            )
        """, (file_id,))
        # 2. Deleting file cascades to chunks
        await conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        await conn.commit()

async def delete_chunks_for_file(conn: aiosqlite.Connection, file_id: str):
    # 1. Delete FTS entries
    await conn.execute("""
        DELETE FROM chunks_fts WHERE chunk_id IN (
            SELECT id FROM chunks WHERE file_id = ?
        )
    """, (file_id,))
    # 2. Delete chunks
    await conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
    await conn.commit()

async def insert_chunk(
    conn: aiosqlite.Connection,
    chunk_id: str,
    file_id: str,
    start_line: int,
    end_line: int,
    chunk_type: str,
    name: Optional[str],
    tokens: int,
    content: str,
    project_id: str,
    file_path: str
):
    await conn.execute(
        """INSERT INTO chunks (id, file_id, start_line, end_line, chunk_type, name, tokens)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (chunk_id, file_id, start_line, end_line, chunk_type, name, tokens)
    )
    await conn.execute(
        """INSERT INTO chunks_fts (chunk_id, project_id, file_path, content)
           VALUES (?, ?, ?, ?)""",
        (chunk_id, project_id, file_path, content)
    )
    await conn.commit()

async def get_file_and_chunk_counts(conn: aiosqlite.Connection, project_id: str) -> tuple[int, int]:
    async with conn.execute("SELECT COUNT(*) FROM files WHERE project_id = ?", (project_id,)) as cursor:
        row = await cursor.fetchone()
        file_count = row[0] if row else 0
        
    async with conn.execute(
        "SELECT COUNT(*) FROM chunks c JOIN files f ON c.file_id = f.id WHERE f.project_id = ?",
        (project_id,)
    ) as cursor:
        row = await cursor.fetchone()
        chunk_count = row[0] if row else 0
        
    return file_count, chunk_count

async def search_fts(conn: aiosqlite.Connection, query: str, project_id: Optional[str] = None, limit: int = 10) -> list[dict]:
    # Sanitize search term to extract only safe alphanumeric/underscore tokens,
    # preventing syntax errors in MATCH queries from punctuation like ( ) : - . , " ' / \
    words = re.findall(r'[a-zA-Z0-9_]+', query)
    if not words:
        return []

    # Two-tier FTS5 query strategy:
    # ≤ 4 tokens  → NEAR(w1 w2 ..., 5)  for proximity-aware matching (high precision)
    # > 4 tokens  → phrase for first 4 + OR for the rest  (high recall, still ranked by phrase hits)
    if len(words) <= 4:
        fts_query = f'NEAR({" ".join(words)}, 5)'
    else:
        phrase_part = '"' + " ".join(words[:4]) + '"'
        extra_part  = " OR ".join(words[4:])
        fts_query   = f"{phrase_part} OR {extra_part}"

    sql = """
        SELECT
            c.id as chunk_id,
            f.path as file_path,
            c.start_line,
            c.end_line,
            c.chunk_type,
            c.name,
            fts.content,
            fts.rank
        FROM chunks_fts fts
        JOIN chunks c ON c.id = fts.chunk_id
        JOIN files f ON f.id = c.file_id
        WHERE chunks_fts MATCH ?
    """
    params: list[Any] = [fts_query]

    if project_id:
        sql += " AND fts.project_id = ?"
        params.append(project_id)

    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    conn.row_factory = aiosqlite.Row
    try:
        async with conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.exception(f"FTS5 Search Query Error: {e}")
        return []

async def search_hybrid(
    conn: aiosqlite.Connection,
    query: str,
    project_id: Optional[str] = None,
    limit: int = 10,
    alpha: Optional[float] = None
) -> list[dict]:
    """
    Hybrid FTS5 + vector search merged with weighted Reciprocal Rank Fusion (RRF).

    Args:
        alpha: FTS5 weight in [0.0, 1.0]. Defaults to settings.VEXINDEX_HYBRID_ALPHA.
               1.0 = pure FTS5, 0.0 = pure vector, 0.6 = recommended for code corpora.
    """
    import asyncio as _asyncio
    from vexindex.vector import vector_store
    from vexindex.config import settings

    if alpha is None:
        alpha = settings.VEXINDEX_HYBRID_ALPHA

    # Fetch more candidates than needed so RRF has a rich pool to merge from
    fts_limit = limit * 3

    # --- Safe vector search helper (returns [] on any failure) ---
    async def _safe_vector_search() -> list[dict]:
        try:
            query_vector = await vector_store.embed_text(query)
            if any(v != 0.0 for v in query_vector):
                return await vector_store.search_vectors(query_vector, project_id, limit=fts_limit)
        except Exception as e:
            logger.error(f"Hybrid Search: Vector search failed: {e}")
        return []

    # 1. Run FTS5 and vector searches IN PARALLEL
    fts_results, vector_results = await _asyncio.gather(
        search_fts(conn, query, project_id, limit=fts_limit),
        _safe_vector_search()
    )

    if not fts_results and not vector_results:
        return []

    # 2. Weighted Reciprocal Rank Fusion
    # Score(c) = alpha * 1/(K + rank_fts) + (1-alpha) * 1/(K + rank_vec)
    k = settings.VEXINDEX_HYBRID_RRF_K
    rrf_scores: dict[str, float] = {}

    for rank_idx, r in enumerate(fts_results):
        chunk_id = r["chunk_id"]
        rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0.0) + alpha * (1.0 / (k + rank_idx))

    for rank_idx, r in enumerate(vector_results):
        chunk_id = r["chunk_id"]
        rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0.0) + (1.0 - alpha) * (1.0 / (k + rank_idx))

    if not rrf_scores:
        return []

    # Fetch chunk_type and file_path for all candidate chunks to apply prose penalty
    candidate_ids = list(rrf_scores.keys())
    placeholders = ",".join("?" for _ in candidate_ids)
    meta_sql = f"""
        SELECT c.id as chunk_id, f.path as file_path, c.chunk_type
        FROM chunks c
        JOIN files f ON c.file_id = f.id
        WHERE c.id IN ({placeholders})
    """
    old_row_factory = conn.row_factory
    conn.row_factory = aiosqlite.Row
    try:
        async with conn.execute(meta_sql, candidate_ids) as cursor:
            rows = await cursor.fetchall()
            chunk_meta = {r["chunk_id"]: {"file_path": r["file_path"], "chunk_type": r["chunk_type"]} for r in rows}
    except Exception as e:
        logger.exception(f"Hybrid Search: Failed to query candidate metadata: {e}")
        chunk_meta = {}
    finally:
        conn.row_factory = old_row_factory

    # Apply prose penalty to prose block chunks
    PROSE_EXTENSIONS = {'.md', '.txt', '.rst', '.egg-info', '.json', '.toml', '.cfg', '.ini', '.yaml', '.yml', '.lock', '.mdx'}
    for chunk_id, score in rrf_scores.items():
        meta = chunk_meta.get(chunk_id, {})
        fp = meta.get('file_path', '')
        ct = meta.get('chunk_type', '')
        if fp:
            ext = os.path.splitext(fp)[1].lower()
            is_prose = (
                ext in PROSE_EXTENSIONS 
                or ".egg-info/" in fp 
                or fp.endswith(".egg-info")
                or "PKG-INFO" in fp
            )
            if ct == 'block' and is_prose:
                rrf_scores[chunk_id] = score * settings.VEXINDEX_HYBRID_PROSE_PENALTY

    # Sort by combined score descending, take top `limit`
    sorted_chunk_ids = sorted(rrf_scores, key=lambda cid: rrf_scores[cid], reverse=True)[:limit]

    # 3. Retrieve full metadata for the winning chunk IDs from SQLite
    placeholders = ",".join("?" for _ in sorted_chunk_ids)
    sql = f"""
        SELECT
            c.id as chunk_id,
            f.path as file_path,
            c.start_line,
            c.end_line,
            c.chunk_type,
            c.name,
            fts.content
        FROM chunks c
        JOIN files f ON c.file_id = f.id
        JOIN chunks_fts fts ON fts.chunk_id = c.id
        WHERE c.id IN ({placeholders})
    """

    conn.row_factory = aiosqlite.Row
    async with conn.execute(sql, sorted_chunk_ids) as cursor:
        rows = await cursor.fetchall()
        meta_map = {r["chunk_id"]: dict(r) for r in rows}

    # Re-order per RRF rank and inject the combined score
    final_results = []
    for chunk_id in sorted_chunk_ids:
        if chunk_id in meta_map:
            res = meta_map[chunk_id]
            res["rank"] = rrf_scores[chunk_id]  # expose combined RRF score
            final_results.append(res)

    return final_results


async def insert_symbol(
    conn: aiosqlite.Connection,
    symbol_id: str,
    file_id: str,
    name: str,
    kind: str,
    scope: Optional[str],
    line: int,
    chunk_id: Optional[str]
):
    await conn.execute(
        "INSERT INTO symbols (id, file_id, name, kind, scope, line, chunk_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (symbol_id, file_id, name, kind, scope, line, chunk_id)
    )
    await conn.commit()


async def insert_relation(
    conn: aiosqlite.Connection,
    relation_id: str,
    project_id: str,
    file_id: str,
    source_symbol: Optional[str],
    relation_type: str,
    target_symbol: str,
    line: int
):
    await conn.execute(
        "INSERT INTO relations (id, project_id, file_id, source_symbol, relation_type, target_symbol, line) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (relation_id, project_id, file_id, source_symbol, relation_type, target_symbol, line)
    )
    await conn.commit()


async def delete_symbols_and_relations_for_file(conn: aiosqlite.Connection, file_id: str):
    await conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
    await conn.execute("DELETE FROM relations WHERE file_id = ?", (file_id,))
    await conn.commit()


async def get_symbol_definitions(conn: aiosqlite.Connection, name: str, project_id: Optional[str] = None) -> list[dict]:
    sql = """
        SELECT s.name, s.kind, s.scope, s.line, f.path as file_path, c.id as chunk_id, c.start_line, c.end_line, fts.content
        FROM symbols s
        JOIN files f ON s.file_id = f.id
        LEFT JOIN chunks c ON s.chunk_id = c.id
        LEFT JOIN chunks_fts fts ON c.id = fts.chunk_id
        WHERE (s.name = ? OR (s.scope || '.' || s.name) = ?)
    """
    params = [name, name]
    if project_id:
        sql += " AND f.project_id = ?"
        params.append(project_id)
        
    old_row_factory = conn.row_factory
    conn.row_factory = aiosqlite.Row
    try:
        async with conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
    finally:
        conn.row_factory = old_row_factory


async def get_symbol_callers(conn: aiosqlite.Connection, name: str, project_id: Optional[str] = None) -> list[dict]:
    sql = """
        SELECT r.source_symbol, r.line, f.path as file_path
        FROM relations r
        JOIN files f ON r.file_id = f.id
        WHERE r.relation_type = 'CALLS' AND (r.target_symbol = ? OR r.target_symbol LIKE '%.' || ?)
    """
    params = [name, name]
    if project_id:
        sql += " AND r.project_id = ?"
        params.append(project_id)
        
    old_row_factory = conn.row_factory
    conn.row_factory = aiosqlite.Row
    try:
        async with conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
    finally:
        conn.row_factory = old_row_factory


async def get_symbol_callees(conn: aiosqlite.Connection, name: str, project_id: Optional[str] = None) -> list[dict]:
    sql = """
        SELECT r.target_symbol, r.line, f.path as file_path
        FROM relations r
        JOIN files f ON r.file_id = f.id
        WHERE r.relation_type = 'CALLS' AND r.source_symbol = ?
    """
    params = [name]
    if project_id:
        sql += " AND r.project_id = ?"
        params.append(project_id)
        
    old_row_factory = conn.row_factory
    conn.row_factory = aiosqlite.Row
    try:
        async with conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
    finally:
        conn.row_factory = old_row_factory


async def get_file_imports(conn: aiosqlite.Connection, file_path: str, project_id: Optional[str] = None) -> list[dict]:
    sql = """
        SELECT r.target_symbol, r.line
        FROM relations r
        JOIN files f ON r.file_id = f.id
        WHERE r.relation_type = 'IMPORTS' AND f.path = ?
    """
    params = [file_path]
    if project_id:
        sql += " AND r.project_id = ?"
        params.append(project_id)
        
    old_row_factory = conn.row_factory
    conn.row_factory = aiosqlite.Row
    try:
        async with conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
    finally:
        conn.row_factory = old_row_factory


async def get_symbol_relations(conn: aiosqlite.Connection, name: str, project_id: Optional[str] = None) -> list[dict]:
    sql = """
        SELECT r.source_symbol, r.relation_type, r.target_symbol, r.line, f.path as file_path
        FROM relations r
        JOIN files f ON r.file_id = f.id
        WHERE (r.source_symbol = ? OR r.target_symbol = ? OR r.target_symbol LIKE '%.' || ?)
    """
    params = [name, name, name]
    if project_id:
        sql += " AND r.project_id = ?"
        params.append(project_id)
        
    old_row_factory = conn.row_factory
    conn.row_factory = aiosqlite.Row
    try:
        async with conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
    finally:
        conn.row_factory = old_row_factory
