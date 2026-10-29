FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8

COPY requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

COPY app/ ./app/
COPY run.py ./
COPY config/ ./config/

RUN mkdir -p logs data

EXPOSE 5050

HEALTHCHECK --interval=30s --timeout=8s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5050/health', timeout=5).status==200 else 1)"

# 必须单进程运行：内置 APScheduler 与钉钉长连接都是进程内单例
CMD ["python", "run.py"]
