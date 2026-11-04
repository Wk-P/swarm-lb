FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv
WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

# 先只装依赖，利用层缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY app.py ./

EXPOSE 8080

CMD ["python", "-u", "app.py"]
