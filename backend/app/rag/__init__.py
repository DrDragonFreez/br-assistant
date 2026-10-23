"""RAG-контур: загрузка, нарезка, векторизация, поиск, генерация."""

from app.rag.chunker import Chunk, ClauseAwareChunker
from app.rag.embedder import OllamaEmbedder, get_embedder
from app.rag.generator import AnswerGenerator, get_generator
from app.rag.loader import LoadedDocument, load_document
from app.rag.reranker import Reranker, get_reranker
from app.rag.retriever import RetrievedChunk, Retriever, get_retriever

__all__ = [
    "Chunk",
    "ClauseAwareChunker",
    "LoadedDocument",
    "load_document",
    "OllamaEmbedder",
    "get_embedder",
    "Retriever",
    "RetrievedChunk",
    "get_retriever",
    "Reranker",
    "get_reranker",
    "AnswerGenerator",
    "get_generator",
]
