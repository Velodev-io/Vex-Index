import os
import uuid
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Literal, Optional

from vexindex.config import settings
from vexindex.models import (
    ProjectCreate,
    ProjectResponse,
    SearchRequest,
    SearchResult,
    IndexStatusResponse,
    SymbolDefinitionResponse,
    SymbolCallerResponse,
    SymbolCalleeResponse,
    FileImportResponse,
    SymbolRelationResponse
)
from vexindex.db import (
    init_db,
    get_all_projects,
    get_project,
    insert_project,
    delete_project,
    get_file_and_chunk_counts,
    search_fts,
    search_hybrid,
    get_symbol_definitions,
    get_symbol_callers,
    get_symbol_callees,
    get_file_imports,
    get_symbol_relations
)
from vexindex.indexer import index_project
from vexindex.watcher import start_watcher, stop_watcher, _watcher_tasks

logger = logging.getLogger(__name__)

# Status dictionary tracking active indexing runs
_index_status: dict[str, dict] = {}

async def run_index_task(project_id: str, root_path: str, conn):
    logger.info(f"Indexing started for project {project_id} at {root_path}")
    _index_status[project_id] = {
        "project_id": project_id,
        "status": "indexing",
        "indexed_files": 0,
        "total_files": 0
    }
    
    def on_progress(indexed, total):
        _index_status[project_id]["indexed_files"] = indexed
        _index_status[project_id]["total_files"] = total
        
    try:
        await index_project(conn, project_id, root_path, on_progress)
        logger.info(f"Indexing completed for project {project_id}")
    except Exception as e:
        logger.exception(f"Background indexing error for {project_id}: {e}")
    finally:
        _index_status[project_id]["status"] = "idle"

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    
    # 1. Initialize SQLite Database
    conn = await init_db(settings.db_path_abs)
    app.state.db = conn
    
    # 2. Start watchers for existing projects
    projects = await get_all_projects(conn)
    for p in projects:
        start_watcher(p["id"], p["root_path"], conn)
        # Seed initial status
        _index_status[p["id"]] = {
            "project_id": p["id"],
            "status": "idle",
            "indexed_files": 0,
            "total_files": 0
        }
        
    logger.info(f"VexIndex: loaded {len(projects)} projects and started watchers.")
    yield
    
    # 3. Shutdown: Cancel all watchtasks, close DB
    tasks_to_await = []
    for pid in list(_watcher_tasks.keys()):
        task = _watcher_tasks.get(pid)
        stop_watcher(pid)
        if task:
            tasks_to_await.append(task)
    if tasks_to_await:
        await asyncio.gather(*tasks_to_await, return_exceptions=True)
    await conn.close()
    logger.info("VexIndex: shutdown completed.")

app = FastAPI(
    title="VexIndex — Codebase Indexing & Search Daemon",
    description="Local-first codebase indexing and full-text search daemon for Vexon OS.",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware config matching VexCTX
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8765", 
        "http://127.0.0.1:8765",
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost"
    ],
    allow_origin_regex="^(chrome-extension|moz-extension|tauri)://.*$|^https?://(localhost|127\\.0\\.0\\.1)(:[0-9]+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/projects", response_model=ProjectResponse, status_code=201)
async def create_project_route(payload: ProjectCreate, background_tasks: BackgroundTasks):
    conn = app.state.db
    root_path = os.path.abspath(os.path.expanduser(payload.root_path))
    
    if not os.path.exists(root_path):
        raise HTTPException(status_code=400, detail="Provided root path does not exist.")
        
    project_id = str(uuid.uuid4())
    try:
        await insert_project(conn, project_id, payload.name, root_path)
        logger.info(f"Project registered: {payload.name} (id: {project_id}, path: {root_path})")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to register project (perhaps root path is already registered): {e}")
        
    start_watcher(project_id, root_path, conn)
    
    # Kick off initial indexing
    background_tasks.add_task(run_index_task, project_id, root_path, conn)
    
    p = await get_project(conn, project_id)
    file_count, chunk_count = await get_file_and_chunk_counts(conn, project_id)
    
    return {
        "id": p["id"],
        "name": p["name"],
        "root_path": p["root_path"],
        "created_at": p["created_at"],
        "last_indexed": p["last_indexed"],
        "file_count": file_count,
        "chunk_count": chunk_count
    }

@app.get("/projects", response_model=list[ProjectResponse])
async def list_projects_route():
    conn = app.state.db
    projects = await get_all_projects(conn)
    
    res = []
    for p in projects:
        file_count, chunk_count = await get_file_and_chunk_counts(conn, p["id"])
        res.append({
            "id": p["id"],
            "name": p["name"],
            "root_path": p["root_path"],
            "created_at": p["created_at"],
            "last_indexed": p["last_indexed"],
            "file_count": file_count,
            "chunk_count": chunk_count
        })
    return res

@app.delete("/projects/{project_id}")
async def delete_project_route(project_id: str):
    conn = app.state.db
    p = await get_project(conn, project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found.")
        
    stop_watcher(project_id)
    await delete_project(conn, project_id)
    _index_status.pop(project_id, None)
    
    return {"status": "success", "message": f"Project {project_id} deleted."}

@app.post("/index/run")
async def run_index_route(project_id: str = Query(...), background_tasks: BackgroundTasks = None):
    conn = app.state.db
    p = await get_project(conn, project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found.")
        
    # Trigger parsing background task
    background_tasks.add_task(run_index_task, project_id, p["root_path"], conn)
    return {"status": "started", "project_id": project_id}

@app.get("/index/status/{project_id}", response_model=IndexStatusResponse)
async def get_index_status_route(project_id: str):
    conn = app.state.db
    p = await get_project(conn, project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found.")
        
    status_info = _index_status.get(project_id, {
        "project_id": project_id,
        "status": "idle",
        "indexed_files": 0,
        "total_files": 0
    })
    
    from vexindex.watcher import get_watcher_status
    w_status = get_watcher_status(project_id)
    
    response_data = dict(status_info)
    response_data["watcher_status"] = w_status.get("status")
    response_data["watcher_error"] = w_status.get("error")
    return response_data

@app.post("/search", response_model=list[SearchResult])
async def search_route(payload: SearchRequest):
    conn = app.state.db
    results = await search_hybrid(
        conn=conn,
        query=payload.query,
        project_id=payload.project_id,
        limit=payload.limit,
        alpha=payload.alpha
    )
    return results

@app.get("/graph/definitions", response_model=list[SymbolDefinitionResponse])
async def graph_definitions_route(name: str = Query(...), project_id: Optional[str] = None):
    conn = app.state.db
    return await get_symbol_definitions(conn, name, project_id)

@app.get("/graph/callers", response_model=list[SymbolCallerResponse])
async def graph_callers_route(name: str = Query(...), project_id: Optional[str] = None):
    conn = app.state.db
    return await get_symbol_callers(conn, name, project_id)

@app.get("/graph/callees", response_model=list[SymbolCalleeResponse])
async def graph_callees_route(name: str = Query(...), project_id: Optional[str] = None):
    conn = app.state.db
    return await get_symbol_callees(conn, name, project_id)

@app.get("/graph/imports", response_model=list[FileImportResponse])
async def graph_imports_route(file_path: str = Query(...), project_id: Optional[str] = None):
    conn = app.state.db
    return await get_file_imports(conn, file_path, project_id)

@app.get("/graph/relations", response_model=list[SymbolRelationResponse])
async def graph_relations_route(name: str = Query(...), project_id: Optional[str] = None):
    conn = app.state.db
    return await get_symbol_relations(conn, name, project_id)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("vexindex.main:app", host=settings.VEXINDEX_HOST, port=settings.VEXINDEX_PORT, reload=True)
