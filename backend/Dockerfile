FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app/backend

COPY backend/pyproject.toml backend/README.md ./
COPY backend/app ./app
COPY backend/config ./config
COPY config/portfolio.json /app/config/portfolio.json
COPY config/stock-ratio-catalog.json /app/config/stock-ratio-catalog.json
COPY config/financial-indicators.json /app/config/financial-indicators.json
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./

COPY packages/news-collector /app/packages/news-collector
RUN python -m pip install --no-cache-dir . /app/packages/news-collector

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
