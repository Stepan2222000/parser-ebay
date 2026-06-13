# Один образ, два entrypoint'а (worker / coordinator) — SPEC §7.
# Воркер — браузерный (cloakbrowser headless=False под Xvfb), поэтому образ несёт
# системные либы хромиума + Xvfb + шрифты; координатор их просто не использует.
FROM python:3.13-slim-bookworm

# git — для pip-установки ebay-library из коммита; остальное — рантайм хромиума.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates fonts-liberation xvfb \
        libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcairo2 \
        libcups2 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0 libnspr4 libnss3 \
        libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 \
        libxfixes3 libxkbcommon0 libxrandr2 libgtk-3-0 libxshmfence1 libxi6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Бинарь cloakbrowser — в build-слой (без рантайм-загрузки с GitHub на старте).
RUN cloakbrowser install

COPY config.yaml entrypoint.sh ./
COPY migrations ./migrations
COPY src ./src
RUN chmod +x entrypoint.sh

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    CLOAKBROWSER_AUTO_UPDATE=false

ENTRYPOINT ["./entrypoint.sh"]
