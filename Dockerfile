FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir uv==0.13.7 \
    && useradd --create-home --uid 10001 app

COPY pyproject.toml uv.lock README.md ./
COPY *.py ./
COPY static ./static

RUN uv sync --frozen --no-dev \
    && chown -R app:app /app

USER app

EXPOSE 8015

CMD ["uv", "run", "--frozen", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8015"]
