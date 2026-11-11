FROM python:3.12-slim
WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir aiohttp psutil

EXPOSE 8080

CMD ["python", "-u", "app.py"]