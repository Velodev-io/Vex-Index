import os
import httpx
import asyncio
import uuid
from typing import Optional, Any
from qdrant_client import QdrantClient
from qdrant_client.http import models

from vexindex.config import settings

class VectorStore:
    def __init__(self):
        self._client = None
        self._collection_name = "vexindex_chunks"

    def _get_client(self) -> QdrantClient:
        if self._client is None:
            url = settings.VEXINDEX_QDRANT_URL
            api_key = settings.VEXINDEX_QDRANT_API_KEY
            if url:
                self._client = QdrantClient(url=url, api_key=api_key)
            else:
                # Ensure path exists
                os.makedirs(settings.vector_path_abs, exist_ok=True)
                self._client = QdrantClient(path=settings.vector_path_abs)
        return self._client

    def _ensure_collection(self):
        client = self._get_client()
        try:
            client.get_collection(self._collection_name)
        except Exception:
            # Create collection with tuned HNSW parameters
            client.create_collection(
                collection_name=self._collection_name,
                vectors_config=models.VectorParams(
                    size=settings.VEXINDEX_EMBED_DIMENSIONS,
                    distance=models.Distance.COSINE,
                    hnsw_config=models.HnswConfigDiff(
                        m=settings.VEXINDEX_QDRANT_HNSW_M,
                        ef_construct=settings.VEXINDEX_QDRANT_HNSW_EF,
                    )
                )
            )
        # Ensure payload index exists on project_id for fast filter-based search
        try:
            client.create_payload_index(
                collection_name=self._collection_name,
                field_name="project_id",
                field_schema=models.PayloadSchemaType.KEYWORD
            )
        except Exception:
            # Index already exists — safe to ignore
            pass

    async def embed_text(self, text: str) -> list[float]:
        """
        Embed content via Ollama. Fallback to zero-vector on failure.
        """
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{settings.OLLAMA_BASE_URL}/api/embed",
                    json={"model": settings.VEXINDEX_EMBED_MODEL, "input": text},
                    timeout=10.0
                )
                if response.status_code == 200:
                    data = response.json()
                    if "embeddings" in data and len(data["embeddings"]) > 0:
                        emb = data["embeddings"][0]
                        if len(emb) == settings.VEXINDEX_EMBED_DIMENSIONS:
                            return emb
            except Exception:
                pass

            try:
                # Try legacy embeddings endpoint
                response = await client.post(
                    f"{settings.OLLAMA_BASE_URL}/api/embeddings",
                    json={"model": settings.VEXINDEX_EMBED_MODEL, "prompt": text},
                    timeout=10.0
                )
                if response.status_code == 200:
                    data = response.json()
                    if "embedding" in data:
                        emb = data["embedding"]
                        if len(emb) == settings.VEXINDEX_EMBED_DIMENSIONS:
                            return emb
            except Exception:
                pass

        # Zero-vector fallback
        return [0.0] * settings.VEXINDEX_EMBED_DIMENSIONS

    def upsert_chunks_sync(self, project_id: str, file_path: str, chunks_data: list[dict]):
        """
        Synchronous batch upsert of chunks with their embeddings to Qdrant.
        """
        client = self._get_client()
        self._ensure_collection()

        points = []
        for item in chunks_data:
            chunk_id = item["chunk_id"]
            embedding = item["embedding"]
            content = item["content"]

            points.append(
                models.PointStruct(
                    id=chunk_id, # Must be a valid UUID string or integer
                    vector=embedding,
                    payload={
                        "chunk_id": chunk_id,
                        "project_id": project_id,
                        "file_path": file_path,
                        "content": content
                    }
                )
            )

        if points:
            client.upsert(
                collection_name=self._collection_name,
                points=points
            )

    async def upsert_chunks(self, project_id: str, file_path: str, chunks_data: list[dict]):
        await asyncio.to_thread(self.upsert_chunks_sync, project_id, file_path, chunks_data)

    def delete_chunks_for_file_sync(self, project_id: str, file_path: str):
        """
        Synchronous deletion of all chunks belonging to a file path.
        """
        client = self._get_client()
        self._ensure_collection()

        client.delete(
            collection_name=self._collection_name,
            points_selector=models.Filter(
                must=[
                    models.FieldCondition(
                        key="project_id",
                        match=models.MatchValue(value=project_id)
                    ),
                    models.FieldCondition(
                        key="file_path",
                        match=models.MatchValue(value=file_path)
                    )
                ]
            )
        )

    async def delete_chunks_for_file(self, project_id: str, file_path: str):
        await asyncio.to_thread(self.delete_chunks_for_file_sync, project_id, file_path)

    def delete_chunks_for_project_sync(self, project_id: str):
        """
        Synchronous deletion of all chunks belonging to a project.
        """
        client = self._get_client()
        self._ensure_collection()

        client.delete(
            collection_name=self._collection_name,
            points_selector=models.Filter(
                must=[
                    models.FieldCondition(
                        key="project_id",
                        match=models.MatchValue(value=project_id)
                    )
                ]
            )
        )

    async def delete_chunks_for_project(self, project_id: str):
        await asyncio.to_thread(self.delete_chunks_for_project_sync, project_id)

    def search_vectors_sync(self, query_vector: list[float], project_id: Optional[str] = None, limit: int = 10) -> list[dict]:
        """
        Synchronous vector search on Qdrant.
        """
        client = self._get_client()
        self._ensure_collection()

        filter_cond = None
        if project_id:
            filter_cond = models.Filter(
                must=[
                    models.FieldCondition(
                        key="project_id",
                        match=models.MatchValue(value=project_id)
                    )
                ]
            )

        results = client.query_points(
            collection_name=self._collection_name,
            query=query_vector,
            query_filter=filter_cond,
            limit=limit
        ).points

        outputs = []
        for r in results:
            outputs.append({
                "chunk_id": r.payload["chunk_id"],
                "file_path": r.payload["file_path"],
                "content": r.payload["content"],
                "score": r.score
            })
        return outputs

    async def search_vectors(self, query_vector: list[float], project_id: Optional[str] = None, limit: int = 10) -> list[dict]:
        return await asyncio.to_thread(self.search_vectors_sync, query_vector, project_id, limit)

vector_store = VectorStore()
