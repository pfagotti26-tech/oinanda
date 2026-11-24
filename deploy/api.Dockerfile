# API do Oi Nanda (FastAPI)
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
# ffmpeg: converte o áudio gravado no navegador em mensagem de voz do WhatsApp
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY backend/ /app/

# testes automáticos: o update.sh roda antes de trocar a versão no ar (docker build --target test)
FROM base AS test
RUN pip install --no-cache-dir -r /app/requirements-test.txt
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-W", "ignore"]

# o que atende os clientes (última etapa: é a que o docker compose monta)
FROM base AS api
RUN rm -rf /app/tests /app/__pycache__ /app/requirements-test.txt
EXPOSE 8001
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
