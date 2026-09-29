FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
# Cache mount keeps downloaded wheels across builds (and across failed builds on flaky networks).
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 120 --retries 10 -r requirements.txt
COPY alembic.ini ./
COPY migrations ./migrations
COPY ./app /app/app
# Same image runs the API (default), the workers (`python -m app.worker ...`) and migrations.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
