FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock
COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./alembic.ini
COPY backend/pyproject.toml ./pyproject.toml
COPY backend/tests ./tests
COPY data ./data
COPY evals/datasets ./evals/datasets
COPY scripts ./scripts
EXPOSE 8000

