FROM python:3.12-slim

# WeasyPrint (HTML-тело письма -> PDF) требует системных библиотек.
# Ставим сразу, чтобы почтовый коллектор не потребовал пересборки образа.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz0b \
        libffi8 \
        shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Буфер: файл ложится сюда до подтверждения источника, поэтому том
# обязан переживать docker compose down.
RUN mkdir -p /data/buffer
VOLUME ["/data/buffer"]

CMD ["python", "-m", "app.main"]
