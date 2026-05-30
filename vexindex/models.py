from pydantic import BaseModel
from typing import Optional, Literal
from datetime import datetime

class ProjectCreate(BaseModel):
    name: str
    root_path: str

class ProjectResponse(BaseModel):
    id: str
    name: str
    root_path: str
    created_at: datetime
    last_indexed: Optional[datetime]
    file_count: int
    chunk_count: int

class SearchRequest(BaseModel):
    query: str
    project_id: Optional[str] = None
    limit: int = 10
    alpha: Optional[float] = None

class SearchResult(BaseModel):
    chunk_id: str
    file_path: str
    start_line: int
    end_line: int
    chunk_type: str
    name: Optional[str]
    content: str
    rank: float

class IndexStatusResponse(BaseModel):
    project_id: str
    status: Literal["idle", "indexing"]
    indexed_files: int
    total_files: int


class SymbolDefinitionResponse(BaseModel):
    name: str
    kind: str
    scope: Optional[str] = None
    line: int
    file_path: str
    chunk_id: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    content: Optional[str] = None


class SymbolCallerResponse(BaseModel):
    source_symbol: Optional[str] = None
    line: int
    file_path: str


class SymbolCalleeResponse(BaseModel):
    target_symbol: str
    line: int
    file_path: str


class FileImportResponse(BaseModel):
    target_symbol: str
    line: int


class SymbolRelationResponse(BaseModel):
    source_symbol: Optional[str] = None
    relation_type: str
    target_symbol: str
    line: int
    file_path: str
