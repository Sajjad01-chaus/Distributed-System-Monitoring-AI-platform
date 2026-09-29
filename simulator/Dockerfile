FROM python:3.12-slim
WORKDIR /sim
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --timeout 120 --retries 10 -r requirements.txt
COPY *.py ./
ENTRYPOINT ["python", "fleet.py"]
