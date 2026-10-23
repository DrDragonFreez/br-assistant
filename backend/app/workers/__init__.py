"""Фоновые задачи: индексация, генерация документов, оценка качества."""

from app.workers.celery_app import celery_app

__all__ = ["celery_app"]
