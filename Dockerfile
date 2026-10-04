FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

COPY requirements.txt requirements-payments.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# x402 lead-package payments pull in web3/solana, so they're opt-in.
ARG WITH_PAYMENTS=0
RUN if [ "$WITH_PAYMENTS" = "1" ]; then pip install --no-cache-dir -r requirements-payments.txt; fi

COPY . .

# All data lives in the PostgreSQL service (DATABASE_URL). The app creates any
# missing tables on startup and loads campaign files from the database.
CMD ["sh", "-c", "uvicorn web.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
