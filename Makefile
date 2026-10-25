.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose
BACKEND := $(COMPOSE) exec -T backend

# Python хоста для сквозной проверки и прогрева кеша. На Windows
# `python` в PATH бывает заглушкой Microsoft Store, которая ничего не
# запускает, поэтому сначала берётся интерпретатор из .venv. Обоим
# скриптам хватает стандартной библиотеки.
HOST_PY := $(firstword $(wildcard .venv/Scripts/python.exe .venv/bin/python) python3)

.PHONY: help env up up-cpu down restart build rebuild logs logs-backend ps \
        seed index status templates templates-check migrate migration \
        shell-backend shell-db smoke warmup test test-integration test-cov \
        models eval clean nuke up-rerank

help: ## Показать список команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

env: ## Создать .env со сгенерированными секретами (если его ещё нет)
	# Скрипт подставляет пароли во все связанные переменные сразу:
	# пароль PostgreSQL входит ещё в DATABASE_URL и DATABASE_URL_SYNC,
	# пароль Redis — в REDIS_URL и две переменные Celery. Простое
	# копирование шаблона оставляло заглушки, и контур падал на первом
	# обращении к базе. Существующий .env не перезаписывается.
	@python scripts/init_env.py

build: env ## Собрать образы
	$(COMPOSE) build --parallel

rebuild: env ## Пересобрать образы без кеша
	$(COMPOSE) build --no-cache --parallel

up: env ## Поднять весь стек (GPU)
	$(COMPOSE) up -d
	@echo "→ UI:      http://localhost"
	@echo "→ API doc: http://localhost/api/v1/docs"
	@echo "→ Grafana: http://localhost/grafana/"

up-cpu: env ## Поднять стек без GPU (ноутбук без NVIDIA)
	$(COMPOSE) -f docker-compose.yml -f docker-compose.cpu.yml up -d

up-rerank: env ## Поднять стек вместе с реранкером (нужно от 12 ГБ памяти ВМ)
	$(COMPOSE) --profile reranker up -d
	@echo "→ не забудь RERANKER_ENABLED=true в .env"

down: ## Остановить стек (данные сохраняются)
	$(COMPOSE) down

restart: ## Перезапустить backend + воркеры
	$(COMPOSE) restart backend celery-worker celery-beat

ps: ## Статус и healthcheck сервисов
	$(COMPOSE) ps

logs: ## Логи всех сервисов
	$(COMPOSE) logs -f --tail=120

logs-backend: ## Логи backend + celery
	$(COMPOSE) logs -f --tail=200 backend celery-worker

models: ## Загрузить модели и собрать br-assistant из Modelfile
	# --exit-code-from обязателен: без него docker compose возвращает 0,
	# даже когда контейнер загрузки упал, и сбой остаётся незамеченным
	# до первого обращения к модели.
	$(COMPOSE) up --exit-code-from ollama-init ollama-init

migrate: ## Применить миграции Alembic
	$(BACKEND) alembic upgrade head

migration: ## Создать миграцию: make migration m="add docs table"
	$(BACKEND) alembic revision --autogenerate -m "$(m)"

seed: ## Создать админа, роли и демо-пользователей
	$(BACKEND) python -m app.db.seed

index: ## Проиндексировать data/instructions в Qdrant
	# Путь не передаётся намеренно: CLI берёт его из INSTRUCTIONS_DIR.
	# Аргумент, начинающийся со слеша, в Git Bash на Windows
	# переписывается в путь Windows («C:/Program Files/Git/app/...»),
	# и индексация сообщает, что файлов нет.
	$(BACKEND) python -m app.workers.cli reindex

templates: ## Собрать шаблоны docxtpl из модулей организаторов
	# Выполняется в контейнере: сборке нужны docxtpl и python-docx,
	# а зависимости бэкенда в локальный .venv не ставятся.
	# Каталог scripts смонтирован только для чтения.
	$(BACKEND) python scripts/build_templates.py

templates-check: ## Проверить собранные шаблоны пробным заполнением
	$(BACKEND) python scripts/build_templates.py --check

status: ## Состояние базы знаний
	$(BACKEND) python -m app.workers.cli status

eval: ## Оценка качества. Быстрый прогон: make eval ARGS=--skip-ragas
	# Путь к набору не передаётся: он берётся из DATA_DIR. Аргумент со
	# слеша в Git Bash на Windows превращается в путь Windows.
	$(BACKEND) python -m app.services.ragas_eval $(ARGS)

smoke: ## Сквозная проверка поднятого контура (вход, ответ, цитаты, отказ)
	$(HOST_PY) scripts/smoke_test.py

warmup: ## Прогреть кеш демонстрационными вопросами (перед защитой)
	# Ответы кешируются на час. Генератор даёт разброс между запусками,
	# и примерно в одном прогоне из нескольких критик отклоняет ответ:
	# скрипт повторяет такой вопрос, чтобы в кеше остался удачный.
	$(HOST_PY) scripts/warmup_demo.py

test: ## Запустить тесты (интеграционные пропускаются)
	# -p no:cacheprovider: каталог /app принадлежит root, и pytest
	# не может создать .pytest_cache — предупреждения на весь экран
	# при полностью успешном прогоне.
	$(BACKEND) pytest -q -p no:cacheprovider

test-integration: ## Запустить все тесты, включая требующие сервисов
	$(BACKEND) env RUN_INTEGRATION_TESTS=1 pytest -p no:cacheprovider -q

test-cov: ## Тесты с покрытием
	$(BACKEND) pytest -p no:cacheprovider --cov=app --cov-report=term-missing

shell-backend: ## Bash внутри backend
	$(COMPOSE) exec backend bash

shell-db: ## psql внутри postgres
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER} -d $${POSTGRES_DB}

clean: ## Остановить и удалить контейнеры + сети
	$(COMPOSE) down --remove-orphans

nuke: ## ⚠ Удалить ВСЁ вместе с данными и моделями
	$(COMPOSE) down -v --remove-orphans
