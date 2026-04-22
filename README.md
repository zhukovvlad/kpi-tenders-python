# python-kpi-tenders

Python AI-воркер для анализа тендерной документации. Получает задачи от Go-бэкенда, обрабатывает документы (DOCX/XLSX/PDF) через парсеры и LLM и возвращает структурированные результаты.

## Место в системе

```
React (5173) ──HTTP──► Go (8080) ──HTTP──► Python (8000)
                          ▲                     │
                          │                     ▼
                          └──── PATCH status   Celery
                                               │   │
                                             Redis MinIO
```

- **Go** создаёт запись `document_tasks`, вызывает `POST /process` на Python.
- **Python** ставит задачу в очередь Celery, скачивает файл из MinIO, обрабатывает и отправляет результат обратно в Go через `PATCH /internal/worker/tasks/{id}/status`.
- **React** общается только с Go, Python никогда не отвечает клиенту напрямую.

## Стек

| Слой | Технология |
|---|---|
| HTTP API | FastAPI + uvicorn |
| Очереди | Celery 5.x, Redis 7 |
| Хранилище файлов | MinIO (S3-совместимый) |
| DOCX/XLSX парсинг | python-docx, openpyxl |
| NER анонимизация | Natasha, Presidio |
| LLM | Google Gemini (google-genai) |
| БД (AI/ML таблицы) | PostgreSQL 16 + pgvector, asyncpg |
| Линтер / форматтер | ruff |
| Тесты | pytest, pytest-cov |

## Быстрый старт

Инфраструктура (PostgreSQL, Redis, MinIO) поднимается из `go-kpi-tenders/docker-compose.yml`.

```bash
# 1. Создать виртуальное окружение и установить зависимости
make install

# 2. Скопировать и заполнить переменные окружения
cp .env.example .env
# Обязательно: SERVICE_TOKEN, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, DATABASE_URL

# 3. Запустить API
make run

# 4. Запустить воркер (dev — обе очереди в одном процессе)
make worker
```

## Конфигурация

Все настройки читаются из `.env` (или переменных окружения). Критичные переменные:

| Переменная | Описание |
|---|---|
| `SERVICE_TOKEN` | Токен для аутентификации запросов от Go. Должен **точно совпадать** с Go-стороной. Обязателен — сервис не запустится без него. |
| `GO_SERVICE_URL` | Базовый URL Go-бэкенда, например `http://localhost:8080` |
| `REDIS_URL` | URL брокера Celery, например `redis://localhost:6379/0` |
| `MINIO_ENDPOINT` | Адрес MinIO, например `localhost:9000` |
| `MINIO_ACCESS_KEY` | Логин MinIO |
| `MINIO_SECRET_KEY` | Пароль MinIO |
| `DATABASE_URL` | PostgreSQL DSN (`postgresql+asyncpg://...`). Используется только для AI/ML-таблиц. |
| `GEMINI_API_KEY` | Нужен только для модулей `extract` и `parse_invoice` (PDF через LLM). |

Персональные переопределения (`HOST`, `PORT`, `CELERY_LOGLEVEL`, concurrency) — в `Makefile.local` (файл в `.gitignore`, не коммитится).

## Модули обработки

| Модуль | Очередь | Входной формат | Что делает |
|---|---|---|---|
| `convert` | `io` | DOCX, XLSX | Конвертирует в Markdown, загружает в MinIO, возвращает `md_storage_path` |
| `anonymize` | `llm` | DOCX | NER через Natasha + Presidio, заменяет персональные данные на `[PERSON_1]`, `[ORG_1]` |
| `extract` | `llm` | DOCX | Двухэтапное извлечение ключей/значений через Gemini Flash + Pro |
| `parse_invoice` | `llm` | XLSX, PDF | Парсинг позиций сметы в структурированный список |

## HTTP API

### `POST /process`

Принимает задачу от Go и ставит её в очередь Celery.

**Заголовки:** `Authorization: Bearer <SERVICE_TOKEN>`

**Тело запроса:**
```json
{
  "task_id": "uuid",
  "document_id": "uuid",
  "module_name": "convert",
  "storage_path": "tenders/docs/uuid.docx"
}
```

**Ответ `202`:**
```json
{
  "task_id": "uuid",
  "celery_task_id": "celery-task-id"
}
```

### `GET /health`

```json
{ "status": "ok", "celery": "ok" }
```

`celery` может быть `"degraded"` если воркеры недоступны.

### Обратный вызов в Go

Python вызывает `PATCH /internal/worker/tasks/{task_id}/status` на Go-стороне:

```json
{
  "status": "completed",
  "celery_task_id": "...",
  "result_payload": { ... }
}
```

При ошибке — `"status": "failed"` + `"error_message": "..."`.

## Celery и очереди

Задачи разбиты на две очереди, чтобы быстрый парсинг не блокировался долгими LLM-запросами:

| Очередь | Задачи | Дефолтная concurrency |
|---|---|---|
| `io` | `convert`, `parse_invoice` | 4 |
| `llm` | `anonymize`, `extract` | 2 |

```bash
# Dev: один воркер на обе очереди
make worker

# Prod-like: раздельные воркеры
make worker-io
make worker-llm

# Мониторинг
make celery-flower    # Flower UI на :5555
make celery-status    # ping воркеров
make celery-tasks     # активные задачи
```

## Разработка

```bash
make test             # все тесты
make test-fast        # без интеграционных
make test-cov         # с отчётом о покрытии

make format           # ruff format + ruff check --fix
make lint             # ruff check (без записи)
```

Локальная проверка модуля `convert` без инфраструктуры:

```bash
# Сохраняет .md рядом с файлом
python scripts/convert_check.py path/to/document.docx

# Только превью в консоль
python scripts/convert_check.py path/to/document.docx --no-save
```

## Структура проекта

```
app/
├── api/          — FastAPI роутер, схемы запросов/ответов
├── celery_app.py — Celery + конфигурация очередей
├── config.py     — Pydantic Settings (lru_cache)
├── go_client/    — HTTP-клиент к Go с ретраями (tenacity)
├── llm/          — Gemini обёртки и промпты
├── nlp/          — NER анонимизатор (Natasha + Presidio)
├── parsers/      — Парсеры DOCX и XLSX
├── storage/      — MinIOClient (download + upload)
└── workers/      — Celery-задачи и общий lifecycle (base.py)
```

## Документация

- [`CLAUDE.md`](CLAUDE.md) — архитектурный контекст, паттерны, решения
- [`docs/PROMPT.md`](docs/PROMPT.md) — полное ТЗ с контрактами result_payload
- [`docs/devlog/`](docs/devlog/) — лог принятых решений по сессиям
