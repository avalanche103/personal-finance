FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN sed -i 's/\r$//' deploy/gcp/entrypoint.sh deploy/gcp/job-loaddata.sh \
    && chmod +x deploy/gcp/entrypoint.sh deploy/gcp/job-loaddata.sh \
    && mkdir -p media data/raw data/processed data/cache staticfiles

EXPOSE 8080

CMD ["deploy/gcp/entrypoint.sh"]
