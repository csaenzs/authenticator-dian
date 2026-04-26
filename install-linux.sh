#!/usr/bin/env bash
# Instalación del servicio tokendian en Linux (Debian/Ubuntu).
# Ejecutar como root: sudo bash install-linux.sh
#
# Requisitos previos:
#   - Python 3.9 o superior  (Ubuntu 20.04 trae 3.8: ver README)
#   - OpenSSL 3.x            (Ubuntu 20.04 trae 1.1.1: ver README)
#   - build-essential         (para algunos wheels nativos de Python)
set -euo pipefail

INSTALL_DIR="/opt/tokendian"
SERVICE_USER="tokendian"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "==> Verificando requisitos previos"

# Python >= 3.9
if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY_VERSION=$("$PYTHON_BIN" --version 2>&1 || echo "no encontrado")
    echo "ERROR: $PYTHON_BIN reporta '$PY_VERSION'. Se requiere Python >= 3.9."
    echo "       En Ubuntu 20.04 (Python 3.8) instala 3.11 con uv:"
    echo "         curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "         uv python install --install-dir /opt/uv-python 3.11"
    echo "         export PYTHON_BIN=/opt/uv-python/cpython-3.11-linux-x86_64-gnu/bin/python3.11"
    echo "       Luego re-ejecuta este script."
    exit 1
fi

# OpenSSL >= 3.0 (warning, no error: el .p12 modernizado por el usuario podría
# pasar igual; solo advertimos del posible fallo en certs legacy)
OPENSSL_VER=$(openssl version 2>/dev/null | awk '{print $2}' || echo "")
case "$OPENSSL_VER" in
    3.*) ;;
    *)
        echo "AVISO: openssl version='$OPENSSL_VER'. Se recomienda OpenSSL >= 3.0"
        echo "      para que el servicio modernice automáticamente certificados"
        echo "      .p12 legacy. En Ubuntu 20.04 conviene compilar 3.x aparte"
        echo "      en /opt/openssl3 y exponerlo al servicio vía systemd"
        echo "      'Environment=PATH=/opt/openssl3/bin:...' (ver README)."
        ;;
esac

echo "==> Instalando dependencias del sistema..."
apt-get update
apt-get install -y python3-venv python3-pip openssl ca-certificates

echo "==> Creando usuario de servicio: $SERVICE_USER"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> Preparando directorios"
mkdir -p "$INSTALL_DIR/sessions" "$INSTALL_DIR/.browser-profiles" \
         "$INSTALL_DIR/.config" "$INSTALL_DIR/.cache"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 700 "$INSTALL_DIR/sessions"

echo "==> Creando entorno virtual"
sudo -u "$SERVICE_USER" "$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

echo "==> Instalando Google Chrome para patchright (~280MB)"
# patchright requiere el canal 'chrome' (Google Chrome real), no chromium.
# El install-deps necesita root para apt; los downloads van al cache del usuario.
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/patchright" install chromium
"$INSTALL_DIR/.venv/bin/patchright" install chrome
"$INSTALL_DIR/.venv/bin/patchright" install-deps chromium

echo "==> Instalando unit systemd"
cp "$INSTALL_DIR/tokendian.service" /etc/systemd/system/tokendian.service
systemctl daemon-reload

echo
echo "==> Listo. Ahora:"
echo "   1. Copia .env.example a .env y rellena SERVICE_API_KEY:"
echo "        cp $INSTALL_DIR/.env.example $INSTALL_DIR/.env"
echo "        # genera un API key largo:"
echo "        echo \"SERVICE_API_KEY=\$(openssl rand -hex 32)\" >> $INSTALL_DIR/.env"
echo "        chmod 600 $INSTALL_DIR/.env"
echo "        chown $SERVICE_USER:$SERVICE_USER $INSTALL_DIR/.env"
echo "   2. Arranca el servicio:"
echo "        systemctl enable --now tokendian"
echo "        systemctl status tokendian"
echo "   3. Verifica que responde:"
echo "        curl http://127.0.0.1:8765/health"
echo
echo "Las credenciales DIAN (cert, password, NIT, cédula, CapSolver key)"
echo "las envía el cliente en cada /auth/login. NO se guardan en el server."
