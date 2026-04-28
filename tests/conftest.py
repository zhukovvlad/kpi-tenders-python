import os

# Set required env vars before any app module is imported.
# Field(...) validators run at import time via Settings() in create_app().
os.environ.setdefault("SERVICE_TOKEN", "test-service-token")
os.environ.setdefault("MINIO_ACCESS_KEY", "minioadmin")
os.environ.setdefault("MINIO_SECRET_KEY", "minioadmin")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")


TEST_TOKEN = "test-service-token"
AUTH_HEADER = f"Bearer {TEST_TOKEN}"
