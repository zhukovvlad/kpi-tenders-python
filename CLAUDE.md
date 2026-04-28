# python-kpi-tenders — CLAUDE.md

## Проект

AI-воркер SaaS-платформы для анализа тендерной документации. Python-сервис обрабатывает документы (DOCX/XLSX/PDF) и возвращает структурированные результаты в Go-бэкенд.

**Stack:** Python 3.12, FastAPI, Celery 5.x, Redis 7, MinIO, PostgreSQL 16 + pgvector, Gemini (google-genai), Natasha (NER). Тулинг: ruff (format + lint), pytest + pytest-cov, Flower.

## Место в системе

```text
React (5173) ──HTTP──► Go (8080) ──LPUSH──► Redis ──► Python workers
                         ▲                               │
                         │                               ▼
                         └──────── Go internal API (PATCH status)
                                                         │
                                                         ▼
                                   MinIO (files)   PostgreSQL (AI/ML direct)
```

- React общается **только** с Go. Python **никогда** не отвечает в React напрямую.
- Go создаёт задачу в `document_tasks` и публикует Celery-сообщение **напрямую в Redis** (`LPUSH`).
- Python-воркеры забирают задачи из Redis и пишут обратно в Go через `PATCH /internal/worker/tasks/{id}/status` с `Authorization: Bearer <SERVICE_TOKEN>`.
- Python не хранит собственного состояния в Go-таблицах. Прямой `asyncpg` используется **только** для AI/ML-производных таблиц (эмбеддинги, чанки, кластеры).
- Python API (`/health`) нужен только для мониторинга воркеров.

## Layout

```text
app/
├── main.py               — FastAPI factory, логгер
├── celery_app.py         — Celery + Redis config, include workers
├── config.py             — Pydantic Settings, lru_cache get_settings()
│
├── api/
│   ├── routes.py         — GET /health
│   └── schemas.py        — HealthResponse, TaskStatusUpdate
│
├── workers/
│   ├── base.py           — run_document_task(): общий lifecycle (processing → download → handler → completed/failed)
│   ├── convert.py        — DOCX/XLSX → Markdown + MinIO upload ✅
│   ├── anonymize.py      — Natasha + stdnum + regex NER ✅
│   ├── resolve_keys.py   — Gemini Flash: вопросы → extraction keys (кастомный lifecycle, без MinIO) ✅
│   ├── extract.py        — Gemini Pro: извлечение значений из анонимизированного MD ✅
│   └── parse_invoice.py  — XLSX/PDF → позиции (stub)
│
├── parsers/
│   ├── docx_parser.py    — DOCX → Markdown ✅
│   └── xlsx_parser.py    — XLSX → Markdown ✅
├── nlp/
│   └── anonymizer.py     — NER pipeline: Natasha (PERSON) + stdnum (INN/OGRN/SNILS) + regex ✅
├── llm/
│   ├── gemini_client.py  — тонкая обёртка над google-genai, классификация ошибок ✅
│   ├── resolve_keys_llm.py — Gemini Flash Structured Outputs: вопросы → {new_keys, resolved_schema} ✅
│   └── extract_llm.py    — Gemini Pro + dynamic Pydantic model: MD → flat {key_name: value} ✅
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
- Go публикует задачи напрямую в Redis (через `LPUSH`, Celery protocol v2). Python-воркеры забирают их через `BRPOP`.

#### Очереди

Задачи маршрутизируются по характеру нагрузки через `task_routes`:

| Очередь | Задачи | Профиль | Дефолт concurrency |
|---|---|---|---|
| `io` | `convert`, `parse_invoice` | I/O-bound (docx/xlsx/pdf parsing) | 4 |
| `llm` | `anonymize`, `resolve_keys`, `extract` | CPU+LLM (Gemini, Natasha) | 2 |

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

### LLM-слой (`app/llm/`)

- `GeminiClient.generate(model, contents, response_schema, temperature=0.0)` — единая точка входа для Gemini API (Structured Outputs). Возвращает `response.parsed` (Pydantic-инстанс).
- `GeminiAPIError(ValueError)` — постоянная ошибка (неверный ключ, недоступная модель). **Не ретраится** — наследует `ValueError`, попадает в `_NO_RETRY`.
- `get_client()` — создаёт новый `GeminiClient` per task invocation (не синглтон).
- **Structured Outputs:** в `response_schema` передаётся Pydantic-класс напрямую. Для `extract` модель создаётся динамически через `pydantic.create_model()` из `extraction_schema` kwargs.
- **Все поля `extract` — `str | None`**: суммы вида "1 500 000 руб." нельзя надёжно парсить во float; Go хранит всё как текст.

### Кастомный lifecycle для `resolve_keys`

`resolve_keys_task` не скачивает файлы из MinIO — все входные данные в kwargs. Lifecycle реализован в `_run_resolve_keys()` напрямую (не через `run_document_task`):

1. `go.update_task(status="processing")`
2. `resolve_keys(client, raw_questions=..., existing_keys=...)`
3. `go.update_task(status="completed", result_payload=...)`

Kwargs: `raw_questions: list[str]`, `existing_keys: list[dict]`. Go не передаёт `md_document_id` — это не нужно. Go сам знает, какой документ отдать на `extract` после получения `result_payload`.

### Closure-паттерн для `extract`

`_handle()` нужен доступ к `extraction_schema` из kwargs, а `run_document_task` передаёт в handler только `file_bytes` и `storage_path`. Решение:

```python
def _bound_handle(file_bytes, sp):
    return _handle(file_bytes, sp, extraction_schema)

return run_document_task(self, task_id, document_id, storage_path, _bound_handle)
```

## Эндпоинты

### Python HTTP API (только мониторинг)

```text
GET  /health         — { status: "ok", celery: "ok" | "degraded" }
```

Go публикует задачи напрямую в Redis (Celery protocol v2). Именованные задачи: `app.workers.<module>.<module>_task`. Маппинг модуль → очередь — через `task_routes` в `celery_app.py`.

### Go internal (Python вызывает Go)

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
make worker-llm       # воркер на очереди llm (anonymize, resolve_keys, extract)

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
make ci               # format + check + test (перед push)
```

Инфраструктура (Postgres, Redis, MinIO) поднимается из `go-kpi-tenders/docker-compose.yml`.

Персональные переопределения (`HOST`, `PORT`, `CELERY_LOGLEVEL`, concurrency) — через `Makefile.local` (см. `Makefile.local.example`, в git не попадает).

## Env

См. `.env.example`. Критичные переменные:

- `SERVICE_TOKEN` — должен **точно** совпадать с `SERVICE_TOKEN` на Go-стороне.
- `GO_SERVICE_URL` — базовый URL Go-бэкенда (без trailing `/`).
- `REDIS_URL` — брокер Celery.
- `MINIO_*` — креды MinIO из docker-compose Go-сервиса.
- `GEMINI_API_KEY` — нужен для `resolve_keys` (Gemini Flash), `extract` (Gemini Pro) и `parse_invoice` (PDF).
- `GEMINI_LIGHT_MODEL` — модель для `resolve_keys`, дефолт `gemini-2.0-flash`.
- `GEMINI_HEAVY_MODEL` — модель для `extract`, дефолт `gemini-2.5-pro`.

## Что НЕ делает Python

- Не хранит пользователей/организации/объекты.
- Не занимается JWT клиентов.
- Не отдаёт данные в React напрямую.
- Не загружает файлы от пользователей (это React → MinIO presigned).
- Не пишет в бизнес-таблицы Go: `documents`, `document_tasks`, `users`, `organizations`, `sites`.

## Текущее состояние

### Реализовано

- FastAPI с `/health` (мониторинг). `/process` удалён — Go публикует задачи напрямую в Redis.
- Celery app с регистрацией 5 воркеров (включая `resolve_keys`).
- `MinIOClient.download(storage_path)` и `MinIOClient.upload(object_name, data)`.
- `GoClient.update_task(...)` с ретраями.
- Общий lifecycle `run_document_task()` в `workers/base.py`.
- Модуль `convert`: `docx_parser.py`, `xlsx_parser.py`, `workers/convert.py` — полностью реализован.
- Модуль `anonymize`: `nlp/anonymizer.py`, `workers/anonymize.py` — полностью реализован.
- LLM-слой: `llm/gemini_client.py`, `llm/resolve_keys_llm.py`, `llm/extract_llm.py` — реализован.
- Модуль `resolve_keys`: Gemini Flash Structured Outputs, кастомный lifecycle (без MinIO download).
- Модуль `extract`: Gemini Pro + dynamic Pydantic model, closure-паттерн для передачи `extraction_schema`.

### Заглушки (`NotImplementedError`)

- `workers/parse_invoice._handle` — XLSX/PDF счета

### Go-сторона: зависимости

Для полноценной работы Python требует от Go:

1. `PATCH /internal/worker/tasks/{id}/status` — обновить `document_tasks`.
2. `POST /documents/:id/presigned-upload` — для React-загрузок в MinIO.

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
