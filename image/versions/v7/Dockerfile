FROM python:3.12-slim
WORKDIR /app

# 复制构建上下文中的应用文件到镜像（构建时请将上下文设为包含 app-v7.py 的目录）
COPY . /app

# 安装第三方依赖（asyncio 是标准库，无需安装）
RUN pip install --no-cache-dir aiohttp

EXPOSE 8080

CMD ["python", "-u", "app-v7.py"]