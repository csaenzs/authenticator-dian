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
mkdir -p "$INSTALL_DIR/sessions" "$INSTALL_DIR/.browser-profiles"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 700 "$INSTALL_DIR/sessions"

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
