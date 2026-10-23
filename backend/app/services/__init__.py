"""Прикладные сервисы: документы, индексация, оценка качества."""

from app.services.doc_validator import DocumentValidator, get_validator
from app.services.document_indexer import DocumentIndexer, get_indexer
from app.services.docx_generator import DocxGenerator, get_docx_generator
from app.services.template_registry import TemplateRegistry, get_template_registry

__all__ = [
    "DocxGenerator",
    "get_docx_generator",
    "DocumentValidator",
    "get_validator",
    "DocumentIndexer",
    "get_indexer",
    "TemplateRegistry",
    "get_template_registry",
]
