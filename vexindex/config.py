import os
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    VEXINDEX_PORT: int = 8766
    VEXINDEX_HOST: str = "127.0.0.1"
    VEXINDEX_DB_PATH: str = "~/.vexindex/index.db"
    VEXINDEX_MAX_CHUNK_LINES: int = 50
    VEXINDEX_CHUNK_OVERLAP_LINES: int = 10
    VEXINDEX_SKIP_DIRS: str = ".git,node_modules,__pycache__,.venv,dist,build,.next,target,.pytest_cache,venv"
    VEXINDEX_EMBED_PROVIDER: str = "ollama"
    VEXINDEX_EMBED_MODEL: str = "nomic-embed-text"
    VEXINDEX_EMBED_DIMENSIONS: int = 768
    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    VEXINDEX_VECTOR_PATH: str = "~/.vexindex/vectors"
    VEXINDEX_QDRANT_URL: Optional[str] = None
    VEXINDEX_QDRANT_API_KEY: Optional[str] = None
    VEXINDEX_HYBRID_RRF_K: int = 60
    # Weight for FTS5 vs vector in RRF: 1.0 = pure FTS5, 0.0 = pure vector, 0.6 = recommended for code
    VEXINDEX_HYBRID_ALPHA: float = 0.6
    # Qdrant HNSW index parameters (applied at collection creation)
    VEXINDEX_QDRANT_HNSW_EF: int = 128  # Higher = better recall, slower search
    VEXINDEX_QDRANT_HNSW_M: int = 16    # Connections per graph node
    VEXINDEX_HYBRID_PROSE_PENALTY: float = 0.3
    VEXINDEX_MIN_MATCH_LENGTH: int = 2  # Fragments shorter than this are ignored in matching
    VEXINDEX_MAX_FILE_SIZE_KB: int = 1024  # Skip files larger than this (default 1MB)

    @property
    def db_path_abs(self) -> str:
        return os.path.abspath(os.path.expanduser(self.VEXINDEX_DB_PATH))

    @property
    def vector_path_abs(self) -> str:
        return os.path.abspath(os.path.expanduser(self.VEXINDEX_VECTOR_PATH))

    @property
    def skip_dirs_set(self) -> set[str]:
        return {d.strip() for d in self.VEXINDEX_SKIP_DIRS.split(",") if d.strip()}

settings = Settings()
