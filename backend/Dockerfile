FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DANG_CONFIG_DIR=/app/config

WORKDIR /app

COPY pyproject.toml ./
COPY config ./config
COPY backend/app ./backend/app
COPY backend/migrations ./backend/migrations
COPY backend/alembic.ini ./backend/
COPY packages/news-collector ./packages/news-collector
RUN python -m pip install --no-cache-dir . ./packages/news-collector

WORKDIR /app/backend
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
