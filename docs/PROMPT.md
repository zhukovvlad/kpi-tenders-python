# ТЗ: Python-сервис `python-kpi-tenders`

## Контекст проекта

SaaS-платформа для анализа тендерной документации строительных объектов.
Три компонента:

- **go-kpi-tenders** — Go/Gin бэкенд-оркестратор (`localhost:8080`)
- **react-kpi-tenders** — Vite/React фронтенд (`localhost:5173`)
- **python-kpi-tenders** — этот сервис, Python AI-воркер (`localhost:8000`)

React общается **только с Go**. Go создаёт задачи и вызывает Python.
Python пишет результаты обратно в Go через internal API.

---

## Роль Python-сервиса

Python — асинхронный воркер для тяжёлой обработки документов.
Он **не хранит собственное состояние** — всё состояние живёт в PostgreSQL Go-стороны.

Флоу:
```
Go                Python              PostgreSQL (shared)     MinIO
─────────────────────────────────────────────────────────────────
1. POST /process ──►
                  2. Celery task.delay()
                  3. Скачать файл ◄──────────────────────────── read
                  4. Обработать
                  5. UPDATE task ──────────────────────────────►
                     (через Go internal API)
```

---

## Инфраструктура (docker-compose в go-kpi-tenders)

```
PostgreSQL  postgres://kpi:kpi_secret@localhost:5432/kpi_tenders
Redis       redis://localhost:6379
MinIO       localhost:9000  user=minioadmin  password=minioadmin  bucket=tenders
```

Python **читает файлы из MinIO** (не загружает их — MinIO presigned URL выдаёт Go).

### Правило: когда Python пишет через Go API, а когда напрямую в PostgreSQL

| Тип данных | Куда пишет Python | Причина |
|---|---|---|
| Статус задачи (`document_tasks.status`) | → Go internal API | Go владеет state machine, tenant isolation |
| Результат задачи (`result_payload`) | → Go internal API | Задача принадлежит Go-стороне |
| Векторные эмбеддинги (`catalog_positions.embedding`) | → PostgreSQL напрямую | Тысячи записей за раз, HTTP round-trip неприемлем |
| Чанки для RAG, кластеры | → PostgreSQL напрямую | Pure ML pipeline, Go здесь ничего не добавляет |

**Правило:** Python пишет через Go API только тогда, когда Go контролирует бизнес-логику или tenant isolation. Для AI/ML производных таблиц (`catalog_positions` и аналогичных) — прямое подключение через `asyncpg`.

---

## Аутентификация Python → Go

Go защищает internal-эндпоинты через `ServiceBearerAuth()` middleware.
Python должен передавать в каждом запросе к Go:

```
Authorization: Bearer <SERVICE_TOKEN>
```

`SERVICE_TOKEN` берётся из `.env` Python-сервиса.

---

## Схема БД (только для понимания, Python в БД напрямую не пишет)

```sql
-- Документы (файлы в MinIO)
documents (
    id              UUID PK,
    organization_id UUID,
    site_id         UUID,
    uploaded_by     UUID,
    parent_id       UUID,  -- если документ получен в результате обработки
    file_name       VARCHAR(255),
    storage_path    TEXT,  -- путь в MinIO: "bucket/prefix/uuid.ext"
    mime_type       VARCHAR(100),
    file_size_bytes BIGINT,
    created_at, updated_at TIMESTAMPTZ
)

-- Задачи AI-воркера
document_tasks (
    id              UUID PK,
    document_id     UUID FK → documents,
    module_name     TEXT,    -- маршрутизатор: 'convert' | 'anonymize' | 'extract' | 'parse_invoice'
    status          VARCHAR  -- 'pending' | 'processing' | 'completed' | 'failed'
    celery_task_id  VARCHAR(255),  -- UUID задачи Celery (Python заполняет)
    result_payload  JSONB,   -- результат (схема зависит от module_name)
    error_message   TEXT,    -- при status='failed'
    created_at, updated_at TIMESTAMPTZ
)
```

---

## Go Internal API (Python вызывает эти эндпоинты)

### PATCH /internal/worker/tasks/{task_id}/status

Обновление статуса и результата задачи.

**Headers:** `Authorization: Bearer <SERVICE_TOKEN>`

**Request body:**
```json
{
  "status": "processing" | "completed" | "failed",
  "celery_task_id": "uuid-celery-task",   // при переходе в processing
  "result_payload": { ... },              // при status=completed
  "error_message": "..."                  // при status=failed
}
```

**Response:** 200 OK

> Этот эндпоинт — заглушка `/internal/worker/*` с TODO в server.go.
> Его нужно реализовать на Go-стороне. Python вызывает его при завершении задачи.

---

## Python API (Go вызывает эти эндпоинты)

### POST /process

Запуск обработки документа.

**Request body:**
```json
{
  "task_id": "uuid-document-task",
  "document_id": "uuid-document",
  "module_name": "convert" | "anonymize" | "extract" | "parse_invoice",
  "storage_path": "tenders/docs/2024/uuid.docx"
}
```

**Response:** 202 Accepted
```json
{
  "task_id": "uuid-document-task",
  "celery_task_id": "celery-uuid"
}
```

### GET /health

```json
{ "status": "ok", "celery": "ok" | "degraded" }
```

---

## Модули (module_name)

### 1. `convert`

**Задача:** Конвертировать DOCX или XLSX в Markdown для последующей подачи в LLM.

**Входные данные:** `storage_path` → файл из MinIO

**Логика:**

DOCX → Markdown:
- Заголовки (`Heading 1/2/3`) → `#` / `##` / `###`
- Параграфы → обычный текст
- Таблицы Word → Markdown-таблицы (`| col | col |`)
- Жирный/курсив → `**bold**` / `*italic*` (сохранять для смысловых акцентов)
- Нумерованные и маркированные списки → `1.` / `-`

XLSX → Markdown:
- Каждый лист → отдельный раздел `## Лист: <name>`
- Каждая таблица → Markdown-таблица со строкой заголовка
- Пустые строки и служебные ячейки пропускать
- Числа сохранять как есть (не округлять)

Тип файла определять по расширению `storage_path`, не по MIME.

**result_payload:**
```json
{
  "format": "markdown",
  "content": "# Договор генерального подряда\n\n## 1. Предмет договора\n\n...",
  "char_count": 45200,
  "section_count": 12,
  "sheet_count": 3
}
```

`sheet_count` — только для XLSX, `section_count` — количество заголовков H1/H2.

**Библиотеки:** `python-docx`, `openpyxl`

---

### 2. `anonymize`

**Задача:** Удалить персональные данные из текстового документа (NER-анонимизация).

**Входные данные:** `storage_path` → DOCX (или результат модуля `convert`)

**Логика:**
1. Если входящий файл DOCX — извлечь текст
2. Запустить русскоязычные NER-модели: **Natasha**, **Presidio** (с русским языком)
3. Заменить найденные сущности плейсхолдерами: `[PERSON_1]`, `[ORG_1]`, `[INN_1]`, `[PHONE_1]`, `[ADDRESS_1]`
4. Сохранить карту замен

**result_payload:**
```json
{
  "anonymized_text": "Договор между [ORG_1] и [ORG_2]...",
  "replacement_map": {
    "[ORG_1]": "ООО «СтройГрупп»",
    "[ORG_2]": "ЗАО «МегаСтрой»",
    "[PERSON_1]": "Иванов Иван Иванович"
  },
  "entities_found": 12
}
```

**Библиотеки:** `natasha`, `presidio-analyzer`, `presidio-anonymizer`, `spacy`, `pymorphy2`

---

### 3. `extract`

**Задача:** Извлечь структурированные данные из документа по запросам пользователя.

**Входные данные:** `storage_path` + `queries` из JSONB-поля задачи

**Двухэтапный LLM-флоу:**

**Этап 1 — Лёгкая LLM (Gemini Flash):**
- Нормализовать пользовательские запросы на русском языке
- Генерировать короткие snake_case ключи: `{"total_square_m2": "Какова общая площадь объекта?"}`
- Проверить дубли по существующим ключам

**Этап 2 — Тяжёлая LLM (Gemini Pro / 2.5 Pro):**
- Отправить анонимизированный текст документа + нормализованные запросы
- Получить JSON: `{"total_square_m2": "12500 м²", "advance_percent": "30%"}`

**result_payload:**
```json
{
  "queries": {
    "total_square_m2": {
      "user_query": "Какова общая площадь объекта в м2?",
      "value": "12 500 м²",
      "confidence": "high"
    },
    "advance_percent": {
      "user_query": "Каков размер аванса?",
      "value": "30%",
      "confidence": "medium"
    }
  },
  "model_used": "gemini-2.5-pro",
  "tokens_used": 4200
}
```

**Библиотеки:** `google-genai`, `tenacity` (retry на API ошибки)

---

### 4. `parse_invoice`

**Задача:** Распознать счёт-фактуру и агрегировать данные по материалам (бетон, арматура).

**Входные данные:** `storage_path` → XLSX или PDF счёт-фактуры

**Логика:**
1. Извлечь строки из XLSX (или распознать PDF через LLM)
2. Найти позиции бетона и арматуры по названию материала
3. Рассчитать: количество, цена за единицу, итоговая сумма, дата поставки

**result_payload:**
```json
{
  "invoice_date": "2024-03-15",
  "supplier": "[ANONYMIZED]",
  "positions": [
    {
      "material": "Бетон B25 W6",
      "unit": "м³",
      "quantity": 120.5,
      "unit_price": 5800.00,
      "total": 698900.00,
      "currency": "RUB"
    },
    {
      "material": "Арматура А500С Ø16",
      "unit": "т",
      "quantity": 15.2,
      "unit_price": 68000.00,
      "total": 1033600.00,
      "currency": "RUB"
    }
  ],
  "total_amount": 1732500.00
}
```

**Библиотеки:** `openpyxl`, `google-genai` (для PDF)

---

## Структура проекта

```
python-kpi-tenders/
├── app/
│   ├── main.py                   # FastAPI app factory
│   ├── celery_app.py             # Celery + Redis config
│   ├── config.py                 # Pydantic Settings из .env
│   ├── dependencies.py           # FastAPI DI (minio, go_client)
│   │
│   ├── api/
│   │   ├── routes.py             # POST /process, GET /health
│   │   └── schemas.py            # Pydantic request/response models
│   │
│   ├── workers/
│   │   ├── __init__.py
│   │   ├── router.py             # module_name → task dispatch
│   │   ├── convert.py            # Celery task: convert
│   │   ├── anonymize.py          # Celery task: anonymize
│   │   ├── extract.py            # Celery task: extract
│   │   └── parse_invoice.py      # Celery task: parse_invoice
│   │
│   ├── parsers/
│   │   ├── docx_parser.py        # DOCX → text
│   │   └── xlsx_parser.py        # XLSX → markdown / dict
│   │
│   ├── nlp/
│   │   └── anonymizer.py         # Natasha + Presidio NER pipeline
│   │
│   ├── llm/
│   │   ├── gemini_client.py      # Gemini API wrapper
│   │   ├── light.py              # Gemini Flash: query normalization
│   │   ├── heavy.py              # Gemini Pro: data extraction
│   │   └── prompts/
│   │       ├── normalize_query.txt
│   │       └── extract_data.txt
│   │
│   ├── storage/
│   │   └── minio_client.py       # MinIO: download file by storage_path
│   │
│   └── go_client/
│       └── client.py             # HTTP client → Go internal API
│
├── tests/
│   ├── conftest.py
│   ├── test_convert.py
│   ├── test_anonymize.py
│   └── test_parse_invoice.py
│
├── main.py                       # uvicorn entry point
├── requirements.txt
├── .env.example
├── Makefile
└── CLAUDE.md
```

---

## Конфигурация (.env)

```env
# App
APP_ENV=local
APP_PORT=8000

# Go internal API
GO_SERVICE_URL=http://localhost:8080
SERVICE_TOKEN=change-me-service-token   # должен совпадать с SERVICE_TOKEN в Go

# MinIO (берётся из docker-compose go-kpi-tenders)
MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
MINIO_BUCKET=tenders
MINIO_USE_SSL=false

# Redis (для Celery)
REDIS_URL=redis://localhost:6379/0

# PostgreSQL (только для записи AI/ML данных: эмбеддинги, catalog_positions)
DATABASE_URL=postgresql+asyncpg://kpi:kpi_secret@localhost:5432/kpi_tenders

# Gemini
GEMINI_API_KEY=...
GEMINI_LIGHT_MODEL=gemini-2.0-flash
GEMINI_HEAVY_MODEL=gemini-2.5-pro
```

---

## Технический стек

| Компонент | Библиотека | Версия |
|-----------|-----------|--------|
| API фреймворк | FastAPI | ≥0.115 |
| ASGI сервер | uvicorn | ≥0.34 |
| Конфиг | pydantic-settings | ≥2.0 |
| Очереди задач | celery | ≥5.5 |
| Брокер | redis | 7-alpine |
| MinIO клиент | minio | ≥7.2 |
| DOCX парсинг | python-docx | ≥1.1 |
| XLSX парсинг | openpyxl | ≥3.1 |
| NER (rus) | natasha | ≥1.6 |
| NER anonymizer | presidio-analyzer + presidio-anonymizer | ≥2.2 |
| spaCy модель | ru_core_news_sm | ≥3.7 |
| Морфология | pymorphy2 | ≥0.9 |
| LLM клиент | google-genai | ≥1.10 |
| Retry | tenacity | ≥8.2 |
| HTTP клиент | httpx | ≥0.28 |
| Валидация | pydantic | ≥2.0 |
| PostgreSQL (прямой доступ) | asyncpg | ≥0.30 |
| pgvector Python | pgvector | ≥0.3 |
| Тесты | pytest + pytest-asyncio | ≥8.0 |

---

## Детали реализации

### Celery: маршрутизация задач

```python
# workers/router.py
MODULE_TASKS = {
    "convert":       convert_task,
    "anonymize":     anonymize_task,
    "extract":       extract_task,
    "parse_invoice": parse_invoice_task,
}

def dispatch(task_id: str, document_id: str, module_name: str, storage_path: str):
    task_fn = MODULE_TASKS.get(module_name)
    if not task_fn:
        raise ValueError(f"Unknown module: {module_name}")
    return task_fn.delay(task_id, document_id, storage_path)
```

### Celery task: базовый паттерн

```python
# workers/convert.py
@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def convert_task(self, task_id: str, document_id: str, storage_path: str):
    go = GoClient()
    try:
        # 1. Сообщить Go: задача взята в обработку
        go.update_task(task_id, status="processing", celery_task_id=self.request.id)

        # 2. Скачать файл из MinIO
        file_bytes = minio_client.download(storage_path)

        # 3. Обработать
        result = run_conversion(file_bytes, storage_path)

        # 4. Сообщить Go: завершено
        go.update_task(task_id, status="completed", result_payload=result)

    except Exception as exc:
        go.update_task(task_id, status="failed", error_message=str(exc))
        raise self.retry(exc=exc)
```

### Go Client

```python
# go_client/client.py
class GoClient:
    def update_task(
        self,
        task_id: str,
        status: str,
        celery_task_id: str | None = None,
        result_payload: dict | None = None,
        error_message: str | None = None,
    ) -> None:
        """PATCH /internal/worker/tasks/{task_id}/status"""
        ...
```

### MinIO Client

```python
# storage/minio_client.py
class MinIOClient:
    def download(self, storage_path: str) -> bytes:
        """
        storage_path формат: "bucket/prefix/uuid.ext"
        Возвращает байты файла.
        """
        ...
```

---

## Порядок реализации

1. **Скелет** — FastAPI app, config, `/health`, `/process` endpoint (только dispatch без логики)
2. **Celery + Redis** — celery_app, базовый task, подключение к брокеру
3. **MinIO client** — download по storage_path
4. **Go client** — PATCH /internal/worker/tasks/{id}/status
5. **Module: convert** — DOCX и XLSX → text/Markdown (без LLM)
6. **Module: anonymize** — Natasha + Presidio NER pipeline
7. **Module: extract** — Gemini Flash (keys) + Gemini Pro (values)
8. **Module: parse_invoice** — счёт-фактуры XLSX + PDF
9. **Тесты** — unit для parsers/nlp, интеграционные для workers

---

## Go-сторона: что нужно доделать

> Это зоны ответственности Go-разработчика, необходимые для работы Python.

1. Реализовать `PATCH /internal/worker/tasks/{id}/status` в `handler_document_task.go`:
   - Принять `status`, `celery_task_id`, `result_payload`, `error_message`
   - Проверить `ServiceBearerAuth` (уже есть)
   - Обновить запись в `document_tasks`

2. Реализовать `POST /internal/worker/process`:
   - Принять `task_id`, `document_id`, `module_name`, `storage_path`
   - Вызвать Python `POST /process`
   - Ответить `{ "celery_task_id": "..." }`

3. Реализовать `POST /documents/:id/presigned-upload`:
   - Вернуть presigned URL для загрузки файла напрямую в MinIO через React

---

## Что НЕ делает Python-сервис

- Не хранит пользователей, организации, объекты строительства
- Не занимается JWT-аутентификацией клиентов
- Не отдаёт данные напрямую в React
- Не загружает файлы от пользователей (это делает React → MinIO)
- Не пишет в бизнес-таблицы Go-стороны напрямую (documents, document_tasks, users, organizations, sites)

---

## Тестовое задание для проверки реализации

После реализации сервис должен пройти следующий флоу:

1. Go создаёт документ с `storage_path="tenders/test/contract.docx"`
2. Файл загружен в MinIO по этому пути
3. Go создаёт `document_task` с `module_name="convert"`, `status="pending"`
4. Go вызывает `POST /process` на Python
5. Python запускает Celery-задачу, обновляет статус в Go: `processing`
6. Python скачивает файл из MinIO, конвертирует DOCX → Markdown
7. Python обновляет статус в Go: `completed`, `result_payload={ format: "markdown", content, ... }`
8. Go возвращает React результат через `GET /tasks/:id`
