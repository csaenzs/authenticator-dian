#!/usr/bin/env bash
# Convierte un .p12 legacy de DIAN (RC2/3DES+SHA1) a .p12 moderno (AES-256)
# que OpenSSL 3 / Playwright pueden cargar sin error.
#
# Uso:
#   DIAN_CERT_PASSWORD='tu_password' bash convert-cert-linux.sh /ruta/al/cert-original.p12
#
# Resultado: /opt/tokendian/secrets/cert-modern.p12 con la misma contraseña original.
set -euo pipefail

SRC="${1:-}"
PWD="${DIAN_CERT_PASSWORD:-}"
DST="/opt/tokendian/secrets/cert-modern.p12"

if [[ -z "$SRC" ]]; then
    echo "Uso: DIAN_CERT_PASSWORD='...' bash convert-cert-linux.sh /ruta/al/cert.p12" >&2
    exit 1
fi
if [[ ! -f "$SRC" ]]; then
    echo "El .p12 origen no existe: $SRC" >&2
    exit 1
fi
if [[ -z "$PWD" ]]; then
    echo "Define DIAN_CERT_PASSWORD antes de ejecutar." >&2
    exit 1
fi

TMP="$(mktemp --suffix=.pem)"
trap 'rm -f "$TMP"' EXIT

echo "Extrayendo PEM (modo legacy)..."
openssl pkcs12 -in "$SRC" -nodes -legacy -passin "pass:$PWD" -out "$TMP"

echo "Re-empaquetando como .p12 moderno (AES-256)..."
openssl pkcs12 -export -in "$TMP" -out "$DST" -passin "pass:$PWD" -passout "pass:$PWD"

chmod 600 "$DST"
chown tokendian:tokendian "$DST" 2>/dev/null || true

echo "OK -> $DST"
