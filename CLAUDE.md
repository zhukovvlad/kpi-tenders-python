# python-kpi-tenders — CLAUDE.md

## Проект

AI-воркер SaaS-платформы для анализа тендерной документации. Python-сервис обрабатывает документы (DOCX/XLSX/PDF) и возвращает структурированные результаты в Go-бэкенд.

**Stack:** Python 3.12, FastAPI, Celery 5.x, Redis 7, MinIO, PostgreSQL 16 + pgvector, Gemini (google-genai), Natasha + Presidio (NER). Тулинг: ruff (format + lint), pytest + pytest-cov, Flower.

## Место в системе

```text
React (5173) ──HTTP──► Go (8080) ──HTTP──► Python (8000)
                         ▲                     │
                         │                     ▼
                         └──── Go internal API (PATCH status)
                                               │
                                               ▼
                        Redis (broker)   MinIO (files)   PostgreSQL (AI/ML direct)
```

- React общается **только** с Go. Python **никогда** не отвечает в React напрямую.
- Go создаёт задачу в `document_tasks` и зовёт Python через `POST /process`.
- Python пишет обратно в Go через `PATCH /internal/worker/tasks/{id}/status` с `Authorization: Bearer <SERVICE_TOKEN>`.
- Python не хранит собственного состояния в Go-таблицах. Прямой `asyncpg` используется **только** для AI/ML-производных таблиц (эмбеддинги, чанки, кластеры).

## Layout

```text
app/
├── main.py               — FastAPI factory, логгер
├── celery_app.py         — Celery + Redis config, include workers
├── config.py             — Pydantic Settings, lru_cache get_settings()
│
├── api/
│   ├── routes.py         — POST /process, GET /health
│   └── schemas.py        — ProcessRequest/Response, HealthResponse, TaskStatusUpdate
│
├── workers/
│   ├── router.py         — MODULE_TASKS + dispatch(module_name) → AsyncResult
│   ├── base.py           — run_document_task(): общий lifecycle (processing → download → handler → completed/failed)
│   ├── convert.py        — DOCX/XLSX → Markdown (stub)
│   ├── anonymize.py      — Natasha + Presidio NER (stub)
│   ├── extract.py        — 2-stage Gemini Flash/Pro (stub)
│   └── parse_invoice.py  — XLSX/PDF → позиции (stub)
│
├── parsers/              — DOCX/XLSX парсеры (не реализованы)
├── nlp/                  — анонимизатор (не реализован)
├── llm/                  — Gemini wrappers + промпты (не реализованы)
│
├── storage/
│   └── minio_client.py   — MinIOClient.download(storage_path) -> bytes
│                           MinIOClient.upload(object_name, data) -> storage_path
│
└── go_client/
    └── client.py         — GoClient.update_task(...) с ретраями (tenacity)

main.py                   — uvicorn entry point
tests/                    — pytest + pytest-asyncio + respx
```

## Паттерны

### Жизненный цикл задачи (workers/base.py)

Все 4 модуля делят один каркас `run_document_task()`:

1. `go.update_task(status="processing", celery_task_id=self.request.id)`
2. `minio.download(storage_path) -> bytes`
3. `handler(file_bytes, storage_path, minio) -> result_payload: dict`
4. `go.update_task(status="completed", result_payload=...)`
5. При любом исключении: `status="failed"`, `error_message=...` и `self.retry(exc)` (до 3 раз, backoff 30 c).

Модуль-специфичная логика живёт в `_handle(file_bytes, storage_path, minio)` внутри `workers/<name>.py`.
`minio` передаётся в хэндлер, чтобы модули, производящие выходные файлы (например, `convert`),
могли загрузить их в MinIO напрямую, не создавая второго подключения.

### Разделение ответственности записи

| Данные | Куда пишет Python | Почему |
|---|---|---|
| `document_tasks.status`, `result_payload`, `error_message` | Go internal API | Go владеет state machine, tenant isolation |
| `catalog_positions.embedding`, чанки, кластеры | `asyncpg` напрямую | Тысячи записей, HTTP неприемлем |
| Всё остальное бизнес-состояние | — | Python не пишет |

### Celery

- **Брокер и backend:** Redis (`REDIS_URL`).
- **Serializer:** только JSON (`task_serializer=json`, `accept_content=[json]`).
- **`task_acks_late=True`, `worker_prefetch_multiplier=1`** — задача остаётся в очереди, пока воркер не подтвердит её завершение; воркер берёт по одной.
- **Именованные задачи:** `app.workers.<module>.<module>_task` — явно `name=` на декораторе, чтобы не зависеть от пути импорта.
- `dispatch(module_name)` в `workers/router.py` — единственная точка входа из API.

#### Очереди

Задачи маршрутизируются по характеру нагрузки через `task_routes`:

| Очередь | Задачи | Профиль | Дефолт concurrency |
|---|---|---|---|
| `io` | `convert`, `parse_invoice` | I/O-bound (docx/xlsx/pdf parsing) | 4 |
| `llm` | `anonymize`, `extract` | CPU+LLM (Gemini, Natasha, Presidio) | 2 |

`task_default_queue=io`. В dev можно поднять единый воркер на обе очереди (`make worker`), в prod-like разнести (`make worker-io` + `make worker-llm`) чтобы долгие LLM-задачи не блокировали быстрый парсинг.

### Конфиг

- `pydantic_settings.BaseSettings`, `.env` → `Settings`.
- `get_settings()` закеширован через `lru_cache`.
- Секреты **только** из env. `SERVICE_TOKEN` обязателен (`Field(..., min_length=1)`), приложение упадёт на старте при отсутствии.

### Go client

- `httpx.Client` синхронно (Celery-задачи синхронные).
- `tenacity.retry` на `NetworkError | TimeoutException | RemoteProtocolError`, 3 попытки, экспоненциальный backoff 0.5 → 4 c.
- 4xx/5xx от Go **не** ретраятся — это `GoClientError` с полным телом ответа.

### MinIO

- `storage_path` формата `bucket/prefix/uuid.ext` либо `prefix/uuid.ext`.
- Если префикс совпадает с `MINIO_BUCKET`, он отрезается; иначе используется default-бакет.
- Тип файла определять **по расширению `storage_path`**, не по MIME.

## Эндпоинты

### Python (вызывает Go)

```text
POST /process        — ProcessRequest{task_id, document_id, module_name, storage_path}
                       → 202 { task_id, celery_task_id }
GET  /health         — { status: "ok", celery: "ok" | "degraded" }
```

### Go internal (вызывает Python)

```text
PATCH /internal/worker/tasks/{task_id}/status
Authorization: Bearer <SERVICE_TOKEN>
Body: { status, celery_task_id?, result_payload?, error_message? }
```

## Команды

```bash
make                  # help (список всех команд)

make venv             # создать .venv (требует Python 3.12)
make install          # установить зависимости

make run              # uvicorn :8000 с --reload
make worker           # один Celery-воркер на обе очереди (dev)
make worker-io        # воркер на очереди io (convert, parse_invoice)
make worker-llm       # воркер на очереди llm (anonymize, extract)

make celery-status    # ping воркеров
make celery-tasks     # активные задачи
make celery-purge     # очистить очереди
make celery-flower    # Flower UI на :5555

make test             # pytest (все)
make test-fast        # без markers integration/slow
make test-integration # только @pytest.mark.integration
make test-cov         # с coverage (html + term)

make format           # ruff format + ruff check --fix
make lint             # ruff check
make check            # ruff --check без записи
```

Инфраструктура (Postgres, Redis, MinIO) поднимается из `go-kpi-tenders/docker-compose.yml`.

Персональные переопределения (`HOST`, `PORT`, `CELERY_LOGLEVEL`, concurrency) — через `Makefile.local` (см. `Makefile.local.example`, в git не попадает).

## Env

См. `.env.example`. Критичные переменные:

- `SERVICE_TOKEN` — должен **точно** совпадать с `SERVICE_TOKEN` на Go-стороне.
- `GO_SERVICE_URL` — базовый URL Go-бэкенда (без trailing `/`).
- `REDIS_URL` — брокер Celery.
- `MINIO_*` — креды MinIO из docker-compose Go-сервиса.
- `GEMINI_API_KEY` — нужен только для `extract` и `parse_invoice` (PDF).

## Что НЕ делает Python

- Не хранит пользователей/организации/объекты.
- Не занимается JWT клиентов.
- Не отдаёт данные в React напрямую.
- Не загружает файлы от пользователей (это React → MinIO presigned).
- Не пишет в бизнес-таблицы Go: `documents`, `document_tasks`, `users`, `organizations`, `sites`.

## Текущее состояние

### Реализовано

- Скелет FastAPI + `/health`, `/process` с диспетчером.
- Celery app с регистрацией 4 воркеров.
- `MinIOClient.download(storage_path)`.
- `GoClient.update_task(...)` с ретраями.
- Общий lifecycle `run_document_task()` в `workers/base.py`.

### Заглушки (`NotImplementedError`)

- `workers/convert._handle` — DOCX/XLSX → Markdown
- `workers/anonymize._handle` — Natasha + Presidio NER
- `workers/extract._handle` — Gemini Flash (keys) + Gemini Pro (values)
- `workers/parse_invoice._handle` — XLSX/PDF счета

### Go-сторона: зависимости

Для полноценной работы Python требует от Go:

1. `PATCH /internal/worker/tasks/{id}/status` — обновить `document_tasks`.
2. `POST /internal/worker/process` (или прямой вызов `POST /process` на Python) — старт обработки.
3. `POST /documents/:id/presigned-upload` — для React-загрузок в MinIO.

## Документация и девлог

### README.md

`README.md` — точка входа для любого человека или агента, который открывает репозиторий впервые. Он должен отражать **текущее** состояние проекта, а не то, каким он задумывался.

**Обновлять при каждом изменении:**
- нового модуля или эндпоинта
- изменения схемы `result_payload` любого воркера
- добавления новой зависимости или команды `make`
- изменения конфигурации (новые обязательные переменные окружения)

Устаревший README хуже отсутствующего: он создаёт ложные ожидания.

### Девлог (`docs/devlog/`)

Девлог фиксирует **решения и их обоснование**, а не просто факт изменений (для этого есть git log).

**Файл:** `docs/devlog/YYYY-MM-DD.md` — одна запись на рабочую сессию.

**Писать в конце каждой сессии, в которой:**
- реализован новый модуль или его значимая часть
- исправлен нетривиальный баг (особенно если обнаружен через тест)
- принято архитектурное решение (формат `result_payload`, разбивка очередей, интерфейс хэндлера и т.п.)
- обнаружена особенность внешней библиотеки, которая может снова укусить

**Структура записи:**
```markdown
# YYYY-MM-DD

## Что сделано
Краткий список — что реализовано, что исправлено.

## Решения и обоснования
Почему сделано именно так. Какие альтернативы отброшены.

## Ловушки и особенности
Баги библиотек, неочевидное поведение, места где легко ошибиться.

## Следующий шаг
Что делать в следующей сессии.
```

Девлог пишется **кратко**: 3–5 абзацев достаточно. Цель — чтобы следующий агент или разработчик понял контекст за 2 минуты, не читая весь git history.

## Порядок реализации модулей

1. `convert` — без LLM, чистый парсинг (docx/xlsx → Markdown). Первым, тестируется проще всего.
2. `anonymize` — NER pipeline. Нужен для безопасной передачи текста в `extract`.
3. `extract` — двухэтапный LLM. Зависит от `anonymize` на продовых документах.
4. `parse_invoice` — XLSX + PDF (через Gemini) → структурированные позиции.

Каждый модуль: парсер в `app/parsers/` или `app/nlp/`/`app/llm/` → чистый `_handle()` в воркере → тесты на парсер + интеграционный на воркер с моками Go/MinIO.
