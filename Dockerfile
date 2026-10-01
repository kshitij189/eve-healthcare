FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app

RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

# Migrations are a separate step (the `migrate` service in docker-compose), not part of start-up.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
