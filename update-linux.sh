#!/usr/bin/env bash
# Actualizacion del servicio tokendian en una instalacion EXISTENTE.
# Ejecutar como root:  sudo bash update-linux.sh
#
# Idempotente. Aplica el estado nuevo del repo a un server ya instalado:
#   - git pull (--ff-only) del repo
#   - asegura xvfb instalado (Chrome HEADED bajo Xvfb evade el WAF de Azure que
#     la DIAN puso en produccion; ver tokendian.service / RUNBOOK.md)
#   - FUERZA HEADLESS=false en el .env. (install-linux.sh solo lo deja bien en
#     instalaciones frescas via .env.example; en un .env YA existente no lo toca,
#     asi que un update sin este paso dejaria HEADLESS=true y el WAF bloquearia.)
#   - reinstala el unit systemd (puede traer cambios, p.ej. ExecStart con xvfb-run)
#   - daemon-reload + restart + verificacion /health + headless
#
# Para una instalacion FRESCA (server nuevo) usar install-linux.sh, que crea el
# usuario de servicio, el venv y baja Chrome. En Ubuntu 20.04 ademas hace falta
# OpenSSL 3 en /opt/openssl3 (ver README / RUNBOOK).
set -euo pipefail

INSTALL_DIR="/opt/tokendian"
SERVICE_USER="tokendian"
ENV_FILE="$INSTALL_DIR/.env"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: corre como root: sudo bash update-linux.sh"
    exit 1
fi
if [ ! -d "$INSTALL_DIR/.git" ]; then
    echo "ERROR: $INSTALL_DIR no es un repo git. Para instalar de cero usa install-linux.sh."
    exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: no existe $ENV_FILE. Instalacion incompleta — corre install-linux.sh primero."
    exit 1
fi

echo "==> git pull (--ff-only) como $SERVICE_USER"
sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" pull --ff-only

echo "==> Asegurando xvfb"
if command -v xvfb-run >/dev/null 2>&1; then
    echo "    xvfb-run ya presente"
else
    apt-get update
    apt-get install -y xvfb
fi

echo "==> Forzando HEADLESS=false en $ENV_FILE"
if grep -q "^HEADLESS=" "$ENV_FILE"; then
    sed -i "s|^HEADLESS=.*|HEADLESS=false|" "$ENV_FILE"
else
    echo "HEADLESS=false" >> "$ENV_FILE"
fi

echo "==> Reinstalando unit systemd"
cp "$INSTALL_DIR/tokendian.service" /etc/systemd/system/tokendian.service
systemctl daemon-reload

echo "==> Reiniciando tokendian"
systemctl restart tokendian
if ! systemctl is-active --quiet tokendian; then
    echo "    ERROR: el servicio no arranco. Revisa: journalctl -u tokendian -n 50"
    exit 1
fi
echo "    Esperando /health (tokendian bajo Xvfb tarda unos segundos)..."
for i in $(seq 1 12); do
    sleep 2
    if curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1; then
        echo "    /health OK: $(curl -s http://127.0.0.1:8765/health)"
        break
    fi
    [ "$i" -eq 12 ] && echo "    AVISO: /health no respondio en ~24s. Revisa: journalctl -u tokendian -n 50"
done
journalctl -u tokendian -n 25 --no-pager 2>/dev/null | grep -i "headless=" | tail -1 || true

echo
echo "==> Update completo. tokendian en $(sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" rev-parse --short HEAD)."
