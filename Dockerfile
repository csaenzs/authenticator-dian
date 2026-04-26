# Dockerfile para tokendian — servicio HTTP de auth DIAN.
#
# Construye una imagen autocontenida con:
#   - Python 3.11
#   - OpenSSL 3 (vía base debian:bookworm)
#   - Google Chrome (canal "chrome" para patchright, ~280 MB)
#   - Las dependencias Python del requirements.txt
#
# Build:
#   docker build -t tokendian:latest .
#
# Run (típico, con .env montado):
#   docker run -d --name tokendian \
#     -p 127.0.0.1:8765:8765 \
#     -e SERVICE_API_KEY=$(openssl rand -hex 32) \
#     -e HEADLESS=true \
#     -v tokendian_sessions:/opt/tokendian/sessions \
#     -v tokendian_profiles:/opt/tokendian/.browser-profiles \
#     tokendian:latest
#
# Las credenciales DIAN (.p12, password, NIT, cédula, CapSolver key) las envía
# el cliente en cada /auth/login. NO se persisten en la imagen.

FROM python:3.11-slim-bookworm AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATCHRIGHT_BROWSERS_PATH=/opt/tokendian/.cache/ms-playwright

# Dependencias del sistema:
#   - openssl: para que el comando 'openssl pkcs12 -legacy' funcione (cert .p12 legacy)
#   - curl/ca-certificates: para que Chrome+patchright descarguen
#   - el resto: librerías que Chrome necesita (sin sandbox; corremos no-root)
RUN apt-get update && apt-get install -y --no-install-recommends \
        openssl \
        ca-certificates \
        curl \
        wget \
        gnupg \
    && rm -rf /var/lib/apt/lists/*

# Crear usuario de servicio (no-root) — Chrome no permite correr como root sin --no-sandbox
RUN groupadd --system tokendian && \
    useradd --system --gid tokendian --create-home --home-dir /opt/tokendian \
            --shell /usr/sbin/nologin tokendian

WORKDIR /opt/tokendian

# Requirements primero (cache layer)
COPY --chown=tokendian:tokendian requirements.txt /opt/tokendian/
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Código fuente
COPY --chown=tokendian:tokendian . /opt/tokendian/

# Directorios runtime que el servicio necesita escribir
RUN mkdir -p \
        /opt/tokendian/sessions \
        /opt/tokendian/.browser-profiles \
        /opt/tokendian/.config \
        /opt/tokendian/.cache && \
    chown -R tokendian:tokendian /opt/tokendian && \
    chmod 700 /opt/tokendian/sessions

# Instalar Google Chrome + deps de sistema (~280 MB).
# 'install chrome' descarga el .deb oficial de Google y corre 'apt install',
# por eso lo hacemos aún siendo root (después dropeamos a tokendian).
RUN /usr/local/bin/patchright install chromium && \
    /usr/local/bin/patchright install chrome && \
    /usr/local/bin/patchright install-deps chromium && \
    # El cache descargado por 'patchright install' como root vive en /root/.cache
    # — copiamos al cache del usuario tokendian para que pueda leerlo.
    cp -r /root/.cache/ms-playwright /opt/tokendian/.cache/ 2>/dev/null || true && \
    chown -R tokendian:tokendian /opt/tokendian/.cache

USER tokendian

EXPOSE 8765

# Healthcheck — /health no requiere auth
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8765/health || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8765", "--workers", "1"]
