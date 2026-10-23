"""Расписание периодических задач (Celery Beat).

Состав расписания отражает эксплуатационные потребности:

* метрики базы знаний обновляются часто — они выводятся на дашборд;
* очистка просроченных черновиков идёт ночью, когда нагрузки нет;
* еженедельная архивация неактивных диалогов освобождает место.

Время задано в часовом поясе из настроек, поэтому «ночные» задачи
выполняются ночью по местному времени, а не по UTC.

Еженедельный прогон RAGAS намеренно не в расписании: он занимает GPU на
десятки минут и во время демонстрации помешает. Запускается вручную через
``make eval`` или из админки.
"""

from __future__ import annotations

from celery.schedules import crontab

from app.workers.celery_app import celery_app

celery_app.conf.beat_schedule = {
    "refresh-kb-metrics": {
        "task": "app.workers.tasks.refresh_kb_metrics",
        "schedule": crontab(minute="*/10"),
        "options": {"queue": "default", "expires": 540},
    },
    "cleanup-expired-documents": {
        "task": "app.workers.tasks.cleanup_expired_documents",
        "schedule": crontab(hour=3, minute=20),
        "options": {"queue": "default", "expires": 3600},
    },
    "archive-stale-sessions": {
        "task": "app.workers.tasks.archive_stale_sessions",
        "schedule": crontab(hour=3, minute=40, day_of_week=0),
        "kwargs": {"days": 30},
        "options": {"queue": "default", "expires": 3600},
    },
}

celery_app.conf.timezone = celery_app.conf.timezone or "Europe/Moscow"

__all__ = ["celery_app"]
