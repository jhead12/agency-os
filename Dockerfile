FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create the data directory for the SQLite DB and campaign files.
# Railway attaches a persistent volume at /data (see railway.json).
# The DB and campaign YAMLs live there so they survive redeploys.
RUN mkdir -p /data

# On startup: initialize the DB (creates tables if missing) and seed
# campaign files to the volume if they don't already exist, then start the server.
CMD ["sh", "-c", "python3 -c \"import os, shutil; from core.db import Database; vol=os.environ.get('RAILWAY_VOLUME_MOUNT_PATH','/data'); dbp=os.environ.get('AGENCY_OS_DB', os.path.join(vol,'db.sqlite')); Database(dbp); camp=os.path.join(vol,'campaigns'); os.makedirs(camp, exist_ok=True) if not os.path.exists(camp) else None; shutil.copytree('campaigns', camp, dirs_exist_ok=True) if not os.path.exists(os.path.join(camp,'voter-guide-cbo','campaign.yaml')) else None; print(f'DB ready at {dbp}, campaigns at {camp}')\" && uvicorn web.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]