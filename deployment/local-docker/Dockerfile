FROM python:3.14-slim

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src
COPY scripts /app/scripts
COPY migrations /app/migrations
COPY alembic.ini /app/alembic.ini

RUN pip install --no-cache-dir -e .

CMD ["python", "scripts/bootstrap.py", "status"]
