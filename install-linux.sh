#!/usr/bin/env bash
# Instalación del servicio tokendian en Linux (Debian/Ubuntu).
# Ejecutar como root: sudo bash install-linux.sh
set -euo pipefail

INSTALL_DIR="/opt/tokendian"
SERVICE_USER="tokendian"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "==> Instalando dependencias del sistema..."
apt-get update
apt-get install -y python3 python3-venv python3-pip openssl ca-certificates

echo "==> Creando usuario de servicio: $SERVICE_USER"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> Preparando directorios"
mkdir -p "$INSTALL_DIR/secrets" "$INSTALL_DIR/.browser-profile"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 700 "$INSTALL_DIR/secrets"

echo "==> Creando entorno virtual"
sudo -u "$SERVICE_USER" "$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

echo "==> Instalando Chromium para Playwright (~150MB)"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/playwright" install chromium
"$INSTALL_DIR/.venv/bin/playwright" install-deps chromium

echo "==> Instalando unit systemd"
cp "$INSTALL_DIR/tokendian.service" /etc/systemd/system/tokendian.service
systemctl daemon-reload

echo
echo "==> Listo. Ahora:"
echo "   1. Copia tu cert .p12 a $INSTALL_DIR/secrets/cert-modern.p12"
echo "      (si es legacy, conviértelo primero — ver convert-cert-linux.sh)"
echo "   2. Copia .env.example a .env y rellena los valores reales:"
echo "        cp $INSTALL_DIR/.env.example $INSTALL_DIR/.env"
echo "        chmod 600 $INSTALL_DIR/.env"
echo "        chown $SERVICE_USER:$SERVICE_USER $INSTALL_DIR/.env"
echo "   3. Arranca el servicio:"
echo "        systemctl enable --now tokendian"
echo "        systemctl status tokendian"
echo "   4. Verifica que responde:"
echo "        curl http://127.0.0.1:8765/health"
