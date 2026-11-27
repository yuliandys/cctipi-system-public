FROM python:3.12-slim

ARG INSTALL_AI=0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=9999

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-ai.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_AI" = "1" ]; then pip install --no-cache-dir -r requirements-ai.txt; fi

COPY . .

RUN if [ "$INSTALL_AI" = "1" ]; then python ai.py --download-model; fi \
    && python tools/build_frontend.py --check --minify \
    && useradd --create-home --shell /usr/sbin/nologin cctipi \
    && mkdir -p /app/data /app/models \
    && chown -R cctipi:cctipi /app

USER cctipi
EXPOSE 9999

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:9999/api/health || exit 1

# Catatan: Saat HOST=0.0.0.0, wajib set CCTIPI_PASS baru (bukan cctipi123)
# atau siapkan volume /app/data & auth.json yang sudah diganti password-nya.
CMD ["python", "server.py"]
