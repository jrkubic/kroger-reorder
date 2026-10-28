# Kroger Reorder in a container. Photos need ANTHROPIC_API_KEY (no Claude Code inside the image).
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY kroger_reorder ./kroger_reorder
RUN pip install --no-cache-dir .

ENV KROGER_REORDER_DATA=/data \
    PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8000

CMD ["kroger-reorder", "--no-browser"]
