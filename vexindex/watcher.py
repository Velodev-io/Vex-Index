import os
import asyncio
import logging
from pathlib import Path
from watchfiles import awatch, Change, DefaultFilter
import aiosqlite

from vexindex.indexer import index_single_file
from vexindex.db import delete_file
from vexindex.config import settings

logger = logging.getLogger(__name__)

_watcher_tasks: dict[str, asyncio.Task] = {}
_watcher_status: dict[str, dict] = {}

def get_watcher_status(project_id: str) -> dict:
    """
    Returns the current watcher status, error state, and retry context for a project.
    """
    return _watcher_status.get(project_id, {"status": "idle", "error": None})

async def watch_project(project_id: str, root_path: str, conn: aiosqlite.Connection):
    """
    Background file watcher using watchfiles.awatch.
    Increments or purges file index chunks on filesystem events.
    """
    root_path = os.path.abspath(os.path.expanduser(root_path))
    skip_dirs = settings.skip_dirs_set
    watch_filter = DefaultFilter(ignore_dirs=skip_dirs)
    
    retry_delay = 1.0
    max_delay = 60.0
    
    while True:
        try:
            logger.info(f"Watcher: starting watch task for project {project_id} at {root_path}")
            _watcher_status[project_id] = {"status": "running", "error": None}
            async for changes in awatch(root_path, watch_filter=watch_filter):
                # Reset retry delay on successful events
                retry_delay = 1.0
                _watcher_status[project_id] = {"status": "running", "error": None}
                for change_type, file_path in changes:
                    # Check if file is inside a skip directory
                    parts = Path(file_path).parts
                    if any(p in skip_dirs for p in parts):
                        continue
                        
                    if change_type in (Change.added, Change.modified):
                        is_file = await asyncio.to_thread(os.path.isfile, file_path)
                        if not is_file:
                            continue
                        indexed = await index_single_file(conn, project_id, file_path)
                        if indexed:
                            logger.info(f"Watcher: indexed/re-indexed {file_path}")
                            
                    elif change_type == Change.deleted:
                        from vexindex.vector import vector_store
                        await delete_file(conn, project_id, file_path)
                        await vector_store.delete_chunks_for_file(project_id, file_path)
                        logger.info(f"Watcher: purged index for {file_path}")
                        
        except asyncio.CancelledError:
            logger.info(f"Watcher: watch task cancelled for project {project_id}")
            _watcher_status.pop(project_id, None)
            break
        except Exception as e:
            _watcher_status[project_id] = {"status": "failed", "error": str(e)}
            logger.exception(f"Watcher: error in watch loop for project {project_id}: {e}")
            logger.info(f"Watcher: retrying watch loop for project {project_id} in {retry_delay}s...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_delay)

def start_watcher(project_id: str, root_path: str, conn: aiosqlite.Connection):
    if project_id in _watcher_tasks:
        stop_watcher(project_id)
        
    task = asyncio.create_task(watch_project(project_id, root_path, conn))
    _watcher_tasks[project_id] = task

def stop_watcher(project_id: str):
    task = _watcher_tasks.pop(project_id, None)
    if task:
        task.cancel()
    _watcher_status.pop(project_id, None)
