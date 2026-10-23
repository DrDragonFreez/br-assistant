"""Агрегатор маршрутов первой версии API.

Проверки состояния и экспорт метрик в этот роутер намеренно не включены:
они подключаются в ``app.main`` в корень приложения.

* ``/health`` опрашивают docker healthcheck и оркестратор — им не нужен
  префикс версии, и ломать их при переходе на ``/api/v2`` нельзя;
* ``/metrics`` раскрывает характер нагрузки и состав базы знаний. Nginx
  отдаёт наружу только ``/api/``, поэтому метрики, подключённые с префиксом
  версии, оказались бы доступны из интернета. В корне приложения они
  достаются только Prometheus по внутренней сети.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import admin, auth, chat, documents

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(chat.router)
api_router.include_router(documents.router)
api_router.include_router(admin.router)

__all__ = ["api_router"]
