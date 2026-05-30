# VexIndex — Codebase Indexing & Search Daemon

VexIndex is a local-first codebase indexing and full-text search (FTS5) daemon designed for Vexon OS. It watches local directories, parses source files into structural code chunks (classes, functions, sliding windows), indexes them in a lightweight SQLite database, and exposes a REST API for codebase query injection.

---

## Features

- **AST-Based Code Chunking**:
  - **Python**: Native AST parser to locate top-level classes and functions.
  - **JavaScript/TypeScript/TSX**: Tree-sitter parsing for accurate extraction of functions, methods, classes, and arrow functions.
  - **Fallback**: Sliding line window chunker for config files, Markdown, and plaintext.
- **SQLite + FTS5**: Superfast full-text search with porter-stemmer tokenization. No vector embeddings or memory-heavy engines required.
- **Background Filesystem Watcher**: Uses `watchfiles` to track creations, modifications, and deletions, incremental re-indexing only dirty files (via SHA-256 content hashes).
- **FastAPI REST API**: Endpoints for project registration, status monitoring, manual re-indexing, and full-text code search.

---

## Directory Structure

```text
vex-index/
├── pyproject.toml         # Project dependencies and configurations
├── README.md              # Project documentation
├── DESIGN.md              # Architecture and parsing specifications
├── BUG_FIX_AGENT.md       # Custom agent instruction for VexIndex bug fixing
├── CODE_REVIEW_AGENT.md   # Custom agent instruction for VexIndex code review
├── DECOMPOSE_AGENT.md     # Custom agent instruction for VexIndex logic decomposition
├── .env.example           # Default configuration settings
├── tests/                 # Unit and integration tests
│   ├── __init__.py
│   ├── test_indexer.py
│   └── test_api.py
└── vexindex/              # Main package
    ├── __init__.py
    ├── main.py            # FastAPI daemon entrypoint & lifecycle
    ├── config.py          # Pydantic-settings config validation
    ├── db.py              # SQLite Schema definition, migration, and FTS5 search queries
    ├── indexer.py         # Chunker dispatching and file indexing loop
    ├── watcher.py         # File watcher daemon for automatic updates
    └── models.py          # Pydantic request/response schemas
```

---

## REST API Specification

### 1. Project Management
- `POST /projects`: Register a directory to index.
  ```json
  {
    "name": "Vexon Core",
    "root_path": "/Users/binova/Documents/Projects/Suru/Vexon/vexon-os"
  }
  ```
- `GET /projects`: List all registered projects with file and chunk counts.
- `DELETE /projects/{project_id}`: Purge indices and stop background watcher.

### 2. Search & Retrieval
- `POST /search`: Perform lexical search over chunks.
  ```json
  {
    "query": "async context manager",
    "project_id": "optional-filter-id",
    "limit": 10
  }
  ```

### 3. Index Operations
- `POST /index/run?project_id={id}`: Force manual re-index in a background task.
- `GET /index/status/{id}`: Get indexing progress status.

---

## Getting Started

### Prerequisites

Ensure you have `uv` installed.

### Installation

```bash
# Clone or move to project directory
cd vex-index

# Sync dependencies
uv sync
```

### Running the Daemon

```bash
# Copy example env
cp .env.example .env

# Run FastAPI service
uv run python -m vexindex.main
```
The daemon runs by default on `http://127.0.0.1:8766`.

---


## Future Roadmap

The following design considerations and feature expansions are planned for future versions of VexIndex:

### v2: Hybrid Retrieval (Lexical + Semantic)
FTS5 lexical search provides excellent precision for exact code tokens and identifiers, but lacks conceptual understanding. v2 will introduce:
- **Vector Embeddings**: Indexing codebase chunks using high-performance local vector models (e.g., `nomic-embed-text` via Ollama).
- **Reciprocal Rank Fusion (RRF)**: A rank-merging algorithm to combine lexical FTS5 rankings and vector similarity rankings, achieving >92% Recall@5.

### v2: MCP Server Surface
To allow any agent (inside Vexon OS or external) to search and interact with the codebase index natively:
- **Model Context Protocol (MCP)**: Implement an MCP host surface exposing tool endpoints like `codebase_search` or `find_symbol_definition` directly to client LLMs and development environments.

### v3: Structured Knowledge Graph Layer
Modern agents benefit from structured relationship traversal rather than flat text chunking. v3 will add:
- **AST Symbol Resolution**: Delineate definitions, imports, callers, and callees.
- **Queryable Symbol Graph**: Expose symbol relationships and call-graph traversal directly to agent contexts to support complex codebase reasoning.

