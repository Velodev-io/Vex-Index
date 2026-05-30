import os
import ast
import uuid
import hashlib
import tiktoken
import asyncio
import logging
from pathlib import Path
from typing import Optional, Callable
import aiosqlite

logger = logging.getLogger(__name__)

from tree_sitter_language_pack import get_language, get_parser
from vexindex.config import settings
from vexindex.db import (
    upsert_file,
    delete_chunks_for_file,
    insert_chunk,
    get_file_hash,
    update_project_last_indexed,
    insert_symbol,
    insert_relation,
    delete_symbols_and_relations_for_file
)

# Common binary, media, compile, and lock files to ignore during codebase crawling
IGNORED_EXTENSIONS = {
    # Images/Assets
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.svg', '.pdf', '.zip', '.tar', '.gz',
    # Audio/Video
    '.mp3', '.mp4', '.wav', '.mov', '.avi',
    # Compiled/Binary
    '.pyc', '.o', '.a', '.so', '.dylib', '.dll', '.exe', '.bin', '.db', '.sqlite', '.sqlite3',
    # Fonts
    '.woff', '.woff2', '.ttf', '.eot',
    # Lockfiles
    '.lock', 'uv.lock', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml'
}

# Initialize tiktoken encoder
try:
    _encoder = tiktoken.get_encoding("cl100k_base")
except Exception:
    _encoder = None

def count_tokens(text: str) -> int:
    if _encoder:
        try:
            return len(_encoder.encode(text))
        except Exception:
            pass
    return len(text.split())

def compute_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()

def chunk_sliding_window(lines: list[str]) -> list[dict]:
    max_lines = settings.VEXINDEX_MAX_CHUNK_LINES
    overlap = settings.VEXINDEX_CHUNK_OVERLAP_LINES
    chunks = []
    total_lines = len(lines)
    
    if total_lines == 0:
        return []
        
    start = 0
    while start < total_lines:
        end = min(start + max_lines, total_lines)
        
        if end < total_lines:
            found_split = False
            for offset in range(0, 6):
                for direction in (-1, 1):
                    idx = end + (offset * direction)
                    if start < idx < total_lines:
                        if lines[idx].strip() == "":
                            end = idx
                            found_split = True
                            break
                if found_split:
                    break
        
        chunk_lines = lines[start:end]
        content = "\n".join(chunk_lines)
        
        chunks.append({
            "start_line": start + 1,
            "end_line": end,
            "chunk_type": "block",
            "name": None,
            "content": content
        })
        
        start = max(start + 1, end - overlap)
        
    return chunks

def chunk_python(source_code: str) -> list[dict]:
    try:
        tree = ast.parse(source_code)
    except Exception:
        # Fallback to sliding window if syntax error
        return chunk_sliding_window(source_code.splitlines())
    
    chunks = []
    lines = source_code.splitlines()
    
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start_line = node.lineno
            end_line = getattr(node, "end_lineno", len(lines))
            chunk_type = "class" if isinstance(node, ast.ClassDef) else "function"
            
            # Extract actual text lines
            content = "\n".join(lines[start_line - 1 : end_line])
            
            chunks.append({
                "start_line": start_line,
                "end_line": end_line,
                "chunk_type": chunk_type,
                "name": node.name,
                "content": content
            })
    
    # If no functions or classes were found, return the sliding window
    if not chunks:
        return chunk_sliding_window(lines)
        
    return chunks

def chunk_js_ts(source_code: str, lang_name: str) -> list[dict]:
    try:
        # Get language and parser from tree-sitter-language-pack
        language = get_language(lang_name)
        parser = get_parser(lang_name)
    except Exception as e:
        logger.warning(f"Failed to load tree-sitter grammar for {lang_name}: {e}. Falling back to sliding window.")
        return chunk_sliding_window(source_code.splitlines())
    
    # In tree-sitter 0.21+, parse() expects str when configured as such, not bytes
    try:
        tree = parser.parse(source_code)
    except Exception as e:
        logger.warning(f"Tree-sitter parse error: {e}. Falling back to sliding window.")
        return chunk_sliding_window(source_code.splitlines())
    
    chunks = []
    source_bytes = source_code.encode("utf-8")
    
    def walk_nodes(node):
        node_kind = node.kind()
        if node_kind in ("function_declaration", "class_declaration", "method_definition"):
            chunk_type = "class" if node_kind == "class_declaration" else "function"
            
            name_node = node.child_by_field_name("name")
            name = None
            if name_node:
                try:
                    name_bytes = source_bytes[name_node.start_byte():name_node.end_byte()]
                    name = name_bytes.decode("utf-8", errors="replace")
                except Exception:
                    pass
            
            start_line = node.start_position().row + 1
            end_line = node.end_position().row + 1
            
            start_b = node.start_byte()
            end_b = node.end_byte()
            content = source_bytes[start_b:end_b].decode("utf-8", errors="replace")
            
            chunks.append({
                "start_line": start_line,
                "end_line": end_line,
                "chunk_type": chunk_type,
                "name": name,
                "content": content
            })
            
        elif node_kind == "variable_declarator":
            # Check if variable has an arrow function assignment
            arrow_node = None
            for i in range(node.child_count()):
                child = node.child(i)
                if child.kind() == "arrow_function":
                    arrow_node = child
                    break
            
            if arrow_node:
                name_node = node.child_by_field_name("name") or node.child_by_field_name("id")
                if not name_node and node.child_count() > 0:
                    name_node = node.child(0)
                
                name = None
                if name_node:
                    try:
                        name_bytes = source_bytes[name_node.start_byte():name_node.end_byte()]
                        name = name_bytes.decode("utf-8", errors="replace")
                    except Exception:
                        pass
                
                start_line = node.start_position().row + 1
                end_line = node.end_position().row + 1
                
                start_b = node.start_byte()
                end_b = node.end_byte()
                content = source_bytes[start_b:end_b].decode("utf-8", errors="replace")
                
                chunks.append({
                    "start_line": start_line,
                    "end_line": end_line,
                    "chunk_type": "function",
                    "name": name,
                    "content": content
                })
        
        for i in range(node.child_count()):
            walk_nodes(node.child(i))
            
    # root_node is a method in tree-sitter 0.21+
    walk_nodes(tree.root_node())
    
    if not chunks:
        return chunk_sliding_window(source_code.splitlines())
        
    return chunks

from vexindex.vector import vector_store

def chunk_file(file_path: str) -> list[dict]:
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        logger.error(f"Failed to read file {file_path}: {e}")
        return []
        
    ext = Path(file_path).suffix.lower()
    
    if ext == ".py":
        return chunk_python(content)
    elif ext in (".js", ".jsx"):
        return chunk_js_ts(content, "javascript")
    elif ext in (".ts", ".tsx"):
        return chunk_js_ts(content, "typescript")
    else:
        return chunk_sliding_window(content.splitlines())

class PythonSymbolVisitor(ast.NodeVisitor):
    def __init__(self):
        self.symbols = []
        self.relations = []
        self.current_scope = []
        
    def visit_ClassDef(self, node):
        class_name = node.name
        self.symbols.append({
            "name": class_name,
            "kind": "class",
            "line": node.lineno,
            "scope": ".".join(self.current_scope) if self.current_scope else None
        })
        
        for base in node.bases:
            base_name = None
            if isinstance(base, ast.Name):
                base_name = base.id
            elif isinstance(base, ast.Attribute):
                base_name = self._resolve_attribute(base)
            if base_name:
                self.relations.append({
                    "source_symbol": class_name,
                    "relation_type": "INHERITS",
                    "target_symbol": base_name,
                    "line": base.lineno
                })
                
        self.current_scope.append(class_name)
        self.generic_visit(node)
        self.current_scope.pop()
        
    def visit_FunctionDef(self, node):
        self._visit_func(node)
        
    def visit_AsyncFunctionDef(self, node):
        self._visit_func(node)
        
    def _visit_func(self, node):
        func_name = node.name
        full_scope = ".".join(self.current_scope) if self.current_scope else None
        symbol_name = f"{full_scope}.{func_name}" if full_scope else func_name
        
        self.symbols.append({
            "name": func_name,
            "kind": "function" if not self.current_scope else "method",
            "line": node.lineno,
            "scope": full_scope
        })
        
        prev_relations = len(self.relations)
        
        self.current_scope.append(func_name)
        self.generic_visit(node)
        self.current_scope.pop()
        
        for i in range(prev_relations, len(self.relations)):
            if self.relations[i]["source_symbol"] is None:
                self.relations[i]["source_symbol"] = symbol_name
                
    def visit_Call(self, node):
        called_name = None
        if isinstance(node.func, ast.Name):
            called_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            called_name = self._resolve_attribute(node.func)
            
        if called_name:
            self.relations.append({
                "source_symbol": None,
                "relation_type": "CALLS",
                "target_symbol": called_name,
                "line": node.lineno
            })
        self.generic_visit(node)
        
    def visit_Import(self, node):
        for name in node.names:
            local_name = name.asname or name.name
            self.symbols.append({
                "name": local_name,
                "kind": "import",
                "line": node.lineno,
                "scope": None
            })
            self.relations.append({
                "source_symbol": None,
                "relation_type": "IMPORTS",
                "target_symbol": name.name,
                "line": node.lineno
            })
            
    def visit_ImportFrom(self, node):
        module = node.module or ""
        for name in node.names:
            local_name = name.asname or name.name
            full_target = f"{module}.{name.name}" if module else name.name
            self.symbols.append({
                "name": local_name,
                "kind": "import",
                "line": node.lineno,
                "scope": None
            })
            self.relations.append({
                "source_symbol": None,
                "relation_type": "IMPORTS",
                "target_symbol": full_target,
                "line": node.lineno
            })
            
    def _resolve_attribute(self, node: ast.Attribute) -> str:
        parts = []
        curr = node
        while isinstance(curr, ast.Attribute):
            parts.append(curr.attr)
            curr = curr.value
        if isinstance(curr, ast.Name):
            parts.append(curr.id)
            return ".".join(reversed(parts))
        return node.attr

def extract_symbols_relations_python(source_code: str) -> tuple[list[dict], list[dict]]:
    try:
        tree = ast.parse(source_code)
    except Exception:
        return [], []
    visitor = PythonSymbolVisitor()
    visitor.visit(tree)
    return visitor.symbols, visitor.relations

def extract_symbols_relations_js_ts(source_code: str, lang_name: str) -> tuple[list[dict], list[dict]]:
    try:
        parser = get_parser(lang_name)
        tree = parser.parse(source_code)
    except Exception:
        return [], []
        
    source_bytes = source_code.encode("utf-8")
    symbols = []
    relations = []
    
    scope_stack = []
    
    def get_node_text(node) -> str:
        return source_bytes[node.start_byte():node.end_byte()].decode("utf-8", errors="replace")
        
    def walk(node):
        kind = node.kind()
        
        if kind in ("class_declaration", "function_declaration", "method_definition", "arrow_function"):
            name = None
            sym_kind = "class" if kind == "class_declaration" else ("method" if "method" in kind or scope_stack else "function")
            
            name_node = node.child_by_field_name("name")
            if not name_node and kind == "arrow_function":
                parent = node.parent()
                if parent and parent.kind() == "variable_declarator":
                    name_node = parent.child_by_field_name("name") or parent.child_by_field_name("id")
            
            if name_node:
                name = get_node_text(name_node)
                
            if name:
                scope = ".".join(scope_stack) if scope_stack else None
                symbols.append({
                    "name": name,
                    "kind": sym_kind,
                    "line": node.start_position().row + 1,
                    "scope": scope
                })
                
                if kind == "class_declaration":
                    for i in range(node.child_count()):
                        child = node.child(i)
                        if child.kind() == "class_heritage":
                            for j in range(child.child_count()):
                                sub = child.child(j)
                                if sub.kind() in ("identifier", "member_expression"):
                                    base_class = get_node_text(sub)
                                    relations.append({
                                        "source_symbol": name,
                                        "relation_type": "INHERITS",
                                        "target_symbol": base_class,
                                        "line": sub.start_position().row + 1
                                    })
                                    
                scope_stack.append(name)
                
            for i in range(node.child_count()):
                walk(node.child(i))
                
            if name:
                scope_stack.pop()
                
        elif kind == "import_statement":
            module_name = ""
            for i in range(node.child_count()):
                child = node.child(i)
                if child.kind() == "string":
                    module_name = get_node_text(child).strip("'\"")
                    break
                    
            for i in range(node.child_count()):
                child = node.child(i)
                if child.kind() == "import_clause":
                    def find_imports(n):
                        n_kind = n.kind()
                        if n_kind == "import_specifier":
                            ids = []
                            for j in range(n.child_count()):
                                sub = n.child(j)
                                if sub.kind() == "identifier":
                                    ids.append(get_node_text(sub))
                            if len(ids) >= 1:
                                imported = ids[0]
                                local = ids[-1]
                                symbols.append({
                                    "name": local,
                                    "kind": "import",
                                    "line": n.start_position().row + 1,
                                    "scope": None
                                })
                                relations.append({
                                    "source_symbol": None,
                                    "relation_type": "IMPORTS",
                                    "target_symbol": f"{module_name}.{imported}" if module_name else imported,
                                    "line": n.start_position().row + 1
                                })
                        elif n_kind == "namespace_import":
                            for j in range(n.child_count()):
                                sub = n.child(j)
                                if sub.kind() == "identifier":
                                    local = get_node_text(sub)
                                    symbols.append({
                                        "name": local,
                                        "kind": "import",
                                        "line": n.start_position().row + 1,
                                        "scope": None
                                    })
                                    relations.append({
                                        "source_symbol": None,
                                        "relation_type": "IMPORTS",
                                        "target_symbol": module_name,
                                        "line": n.start_position().row + 1
                                    })
                                    break
                        elif n_kind == "identifier":
                            local = get_node_text(n)
                            symbols.append({
                                "name": local,
                                "kind": "import",
                                "line": n.start_position().row + 1,
                                "scope": None
                            })
                            relations.append({
                                "source_symbol": None,
                                "relation_type": "IMPORTS",
                                "target_symbol": module_name,
                                "line": n.start_position().row + 1
                            })
                        else:
                            for j in range(n.child_count()):
                                find_imports(n.child(j))
                                
                    find_imports(child)
            
            for i in range(node.child_count()):
                walk(node.child(i))
                
        elif kind == "call_expression":
            func_node = node.child_by_field_name("function")
            if not func_node and node.child_count() > 0:
                func_node = node.child(0)
                
            if func_node:
                target_symbol = get_node_text(func_node)
                caller = ".".join(scope_stack) if scope_stack else None
                relations.append({
                    "source_symbol": caller,
                    "relation_type": "CALLS",
                    "target_symbol": target_symbol,
                    "line": node.start_position().row + 1
                })
                
            for i in range(node.child_count()):
                walk(node.child(i))
                
        else:
            for i in range(node.child_count()):
                walk(node.child(i))
                
    walk(tree.root_node())
    return symbols, relations

def extract_symbols_and_relations(file_path: str) -> tuple[list[dict], list[dict]]:
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        logger.error(f"Failed to read file {file_path} for symbol extraction: {e}")
        return [], []
        
    ext = Path(file_path).suffix.lower()
    if ext == ".py":
        return extract_symbols_relations_python(content)
    elif ext in (".js", ".jsx"):
        return extract_symbols_relations_js_ts(content, "javascript")
    elif ext in (".ts", ".tsx"):
        return extract_symbols_relations_js_ts(content, "typescript")
    else:
        return [], []

def discover_files_sync(root_path: str, skip_dirs: set[str]) -> list[str]:
    all_filepaths = []
    for root, dirs, files in os.walk(root_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in files:
            full_path = os.path.join(root, f)
            all_filepaths.append(full_path)
    return all_filepaths

def read_and_hash_file_sync(file_path: str) -> str:
    with open(file_path, "rb") as f:
        content_bytes = f.read()
    return compute_sha256(content_bytes)

def is_sensitive_file(file_path: str) -> bool:
    name = Path(file_path).name.lower()
    ext = Path(file_path).suffix.lower()
    
    # Exclude files by exact name or starting patterns
    if name == ".env" or name.startswith(".env."):
        return True
    if name in ("id_rsa", "id_ed25519"):
        return True
    
    # Exclude files by extension
    SENSITIVE_EXTENSIONS = {".pem", ".key", ".crt", ".p12", ".pfx"}
    if ext in SENSITIVE_EXTENSIONS:
        return True
        
    # Check for secret patterns in filename
    if "secret" in name or "token" in name or "credential" in name:
        if ext in (".json", ".yaml", ".yml", ".toml", ".txt", ""):
            return True
            
    return False

async def index_single_file(conn: aiosqlite.Connection, project_id: str, file_path: str) -> bool:
    """
    Checks ignoring rules, sensitive file rules, file size limits, computes hash, 
    and indexes the file if new or modified. Returns True if indexing was executed, else False.
    """
    ext = Path(file_path).suffix.lower()
    name = Path(file_path).name.lower()
    
    if ext in IGNORED_EXTENSIONS or name in IGNORED_EXTENSIONS:
        return False
        
    if is_sensitive_file(file_path):
        logger.info(f"Skipping sensitive file: {file_path}")
        return False
        
    try:
        file_size = os.path.getsize(file_path)
        max_size = settings.VEXINDEX_MAX_FILE_SIZE_KB * 1024
        if file_size > max_size:
            logger.warning(f"Skipping file {file_path} exceeding size limit ({file_size} > {max_size} bytes)")
            return False
    except Exception as e:
        logger.error(f"Failed to check file size for {file_path}: {e}")
        return False

    try:
        new_hash = await asyncio.to_thread(read_and_hash_file_sync, file_path)
    except Exception as e:
        logger.error(f"Failed to read/hash file {file_path}: {e}")
        return False
        
    old_hash = await get_file_hash(conn, project_id, file_path)
    if old_hash != new_hash:
        file_id = str(uuid.uuid4())
        await upsert_file(conn, file_id, project_id, file_path, new_hash)
        await index_file(conn, project_id, file_path, file_id)
        return True
        
    return False

async def index_file(conn: aiosqlite.Connection, project_id: str, file_path: str, file_id: str):
    """
    Chunks a single file, deletes existing chunks, and updates db tables and vector index.
    """
    chunks = await asyncio.to_thread(chunk_file, file_path)
    await delete_chunks_for_file(conn, file_id)
    await delete_symbols_and_relations_for_file(conn, file_id)
    await vector_store.delete_chunks_for_file(project_id, file_path)
    
    # Store chunk_name -> chunk_id to link symbols to chunks later
    name_to_chunk_id = {}
    
    batch_vectors = []
    for c in chunks:
        chunk_id = str(uuid.uuid4())
        tokens = count_tokens(c["content"])
        await insert_chunk(
            conn=conn,
            chunk_id=chunk_id,
            file_id=file_id,
            start_line=c["start_line"],
            end_line=c["end_line"],
            chunk_type=c["chunk_type"],
            name=c["name"],
            tokens=tokens,
            content=c["content"],
            project_id=project_id,
            file_path=file_path
        )
        if c["name"]:
            name_to_chunk_id[(c["name"], c["chunk_type"])] = chunk_id
            
        embedding = await vector_store.embed_text(c["content"])
        batch_vectors.append({
            "chunk_id": chunk_id,
            "embedding": embedding,
            "content": c["content"]
        })
        
    if batch_vectors:
        await vector_store.upsert_chunks(project_id, file_path, batch_vectors)

    # Extract and store symbols & relations
    try:
        symbols, relations = await asyncio.to_thread(extract_symbols_and_relations, file_path)
        for sym in symbols:
            # Try to associate with an inserted chunk
            sym_chunk_id = name_to_chunk_id.get((sym["name"], sym["kind"]))
            if not sym_chunk_id and sym["kind"] == "method":
                # Fallback to function kind in chunks table
                sym_chunk_id = name_to_chunk_id.get((sym["name"], "function"))
            
            await insert_symbol(
                conn, 
                str(uuid.uuid4()), 
                file_id, 
                sym["name"], 
                sym["kind"], 
                sym.get("scope"), 
                sym["line"], 
                sym_chunk_id
            )
            
        for rel in relations:
            await insert_relation(
                conn, 
                str(uuid.uuid4()), 
                project_id, 
                file_id, 
                rel.get("source_symbol"), 
                rel["relation_type"], 
                rel["target_symbol"], 
                rel["line"]
            )
    except Exception as e:
        logger.exception(f"Failed to extract/store symbols and relations for {file_path}: {e}")

async def index_project(
    conn: aiosqlite.Connection,
    project_id: str,
    root_path: str,
    on_progress: Optional[Callable[[int, int], None]] = None
):
    """
    Crawls project path, skip matches in settings, indexes added/modified files.
    """
    root_path = os.path.abspath(os.path.expanduser(root_path))
    if not os.path.exists(root_path):
        return
        
    skip_dirs = settings.skip_dirs_set
    
    # 1. Discover all candidate files
    all_filepaths = await asyncio.to_thread(discover_files_sync, root_path, skip_dirs)
            
    total_files = len(all_filepaths)
    indexed_count = 0
    
    if on_progress:
        on_progress(0, total_files)
        
    for path in all_filepaths:
        try:
            await index_single_file(conn, project_id, path)
        except Exception as e:
            logger.exception(f"Error indexing file {path}: {e}")
            
        indexed_count += 1
        if on_progress:
            on_progress(indexed_count, total_files)
            
    from datetime import datetime, timezone
    await update_project_last_indexed(conn, project_id, datetime.now(timezone.utc).isoformat())
