#!/usr/bin/env bash
# Instalación del servicio tokendian en Linux (Debian/Ubuntu).
# Ejecutar como root: sudo bash install-linux.sh [opciones]
#
# Requisitos previos del sistema:
#   - Python >= 3.9   (Ubuntu 22.04+ trae 3.10; 24.04 trae 3.12)
#   - OpenSSL >= 3.0  (Ubuntu 22.04+ trae 3.0+ de fábrica)
#   - build-essential (para algunos wheels nativos)
#
# Si vienes desde Ubuntu 20.04 (focal), usa los pasos manuales del README
# ("Instalación en Ubuntu 20.04") antes de correr este script.
#
# Opciones:
#   --auto-key             Genera un SERVICE_API_KEY aleatorio y escribe el .env.
#                          Si se omite, deja al usuario crearlo manualmente.
#   --apidian-env <ruta>   Escribe TOKENDIAN_URL/TOKENDIAN_API_KEY en el .env
#                          de apidian (típicamente /var/www/html/apidian/.env).
#                          Útil cuando este script lo invoca el instalador de apidian.
#   --start                Arranca el servicio al final (systemctl enable --now).
#   --help                 Muestra esta ayuda.
set -euo pipefail

INSTALL_DIR="/opt/tokendian"
SERVICE_USER="tokendian"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Defaults de los flags
AUTO_KEY=false
APIDIAN_ENV=""
START_SERVICE=false

# Parseo de flags
while [ $# -gt 0 ]; do
    case "$1" in
        --auto-key)        AUTO_KEY=true; shift ;;
        --apidian-env)     APIDIAN_ENV="$2"; shift 2 ;;
        --start)           START_SERVICE=true; shift ;;
        --help|-h)
            sed -n '2,/^set/p' "$0" | sed 's/^# \?//' | head -n -1
            exit 0
            ;;
        *)
            echo "ERROR: flag desconocido '$1'. Usa --help."
            exit 1
            ;;
    esac
done

echo "==> Verificando requisitos previos"

# Python >= 3.9
if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY_VERSION=$("$PYTHON_BIN" --version 2>&1 || echo "no encontrado")
    echo "ERROR: $PYTHON_BIN reporta '$PY_VERSION'. Se requiere Python >= 3.9."
    echo "       Si estás en Ubuntu 20.04, ver sección 'Instalación en Ubuntu 20.04'"
    echo "       del README — requiere instalar Python 3.11 vía uv antes."
    exit 1
fi

# OpenSSL >= 3.0
OPENSSL_VER=$(openssl version 2>/dev/null | awk '{print $2}' || echo "")
case "$OPENSSL_VER" in
    3.*) ;;
    *)
        echo "ERROR: openssl version='$OPENSSL_VER'. Se requiere OpenSSL >= 3.0"
        echo "       para que el flag '-legacy' de pkcs12 funcione (necesario para"
        echo "       modernizar certificados .p12 que la DIAN entrega en formato viejo)."
        echo "       Si estás en Ubuntu 20.04, ver sección 'Instalación en Ubuntu 20.04'"
        echo "       del README — requiere compilar OpenSSL 3 aparte."
        exit 1
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

# ─── Configuración automática del .env ──────────────────────────────────────
if [ "$AUTO_KEY" = "true" ]; then
    echo "==> Generando .env con SERVICE_API_KEY automático"
    if [ ! -f "$INSTALL_DIR/.env" ]; then
        cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    fi
    SERVICE_API_KEY=$(openssl rand -hex 32)
    if grep -q "^SERVICE_API_KEY=" "$INSTALL_DIR/.env"; then
        sed -i "s|^SERVICE_API_KEY=.*|SERVICE_API_KEY=${SERVICE_API_KEY}|" "$INSTALL_DIR/.env"
    else
        echo "SERVICE_API_KEY=${SERVICE_API_KEY}" >> "$INSTALL_DIR/.env"
    fi
    chmod 600 "$INSTALL_DIR/.env"
    chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/.env"
    echo "    SERVICE_API_KEY generado: ${SERVICE_API_KEY:0:8}... (guardado en $INSTALL_DIR/.env)"
fi

# ─── Sincronización con apidian ────────────────────────────────────────────
if [ -n "$APIDIAN_ENV" ]; then
    if [ ! -f "$APIDIAN_ENV" ]; then
        echo "AVISO: --apidian-env apuntó a '$APIDIAN_ENV' pero no existe el archivo."
        echo "       Salto este paso. Configura TOKENDIAN_API_KEY manualmente en apidian."
    elif [ "$AUTO_KEY" != "true" ]; then
        echo "AVISO: --apidian-env requiere también --auto-key (sin SERVICE_API_KEY"
        echo "       generado no hay nada que sincronizar). Salto este paso."
    else
        echo "==> Sincronizando TOKENDIAN_* en apidian's .env: $APIDIAN_ENV"
        # TOKENDIAN_URL
        if grep -q "^TOKENDIAN_URL=" "$APIDIAN_ENV"; then
            sed -i "s|^TOKENDIAN_URL=.*|TOKENDIAN_URL=http://127.0.0.1:8765|" "$APIDIAN_ENV"
        else
            echo "TOKENDIAN_URL=http://127.0.0.1:8765" >> "$APIDIAN_ENV"
        fi
        # TOKENDIAN_API_KEY (mismo valor que SERVICE_API_KEY)
        if grep -q "^TOKENDIAN_API_KEY=" "$APIDIAN_ENV"; then
            sed -i "s|^TOKENDIAN_API_KEY=.*|TOKENDIAN_API_KEY=${SERVICE_API_KEY}|" "$APIDIAN_ENV"
        else
            echo "TOKENDIAN_API_KEY=${SERVICE_API_KEY}" >> "$APIDIAN_ENV"
        fi
        # TOKENDIAN_TIMEOUT
        if ! grep -q "^TOKENDIAN_TIMEOUT=" "$APIDIAN_ENV"; then
            echo "TOKENDIAN_TIMEOUT=90" >> "$APIDIAN_ENV"
        fi
        echo "    TOKENDIAN_URL/API_KEY/TIMEOUT escritos en $APIDIAN_ENV"

        # ── Pre-crear el directorio de cookies que apidian necesita escribir ──
        # Si no existe + el proceso PHP (www-data) no puede crearlo en runtime,
        # la primera consulta falla con "file_put_contents: No such file or directory".
        # Mejor crearlo acá con perms correctos antes que dejarlo lazy.
        APIDIAN_DIR=$(dirname "$APIDIAN_ENV")
        APIDIAN_COOKIES_DIR="$APIDIAN_DIR/storage/app/dian_cookies"
        if [ -d "$APIDIAN_DIR/storage/app" ]; then
            mkdir -p "$APIDIAN_COOKIES_DIR"
            # Detectar usuario web (Ubuntu/Debian = www-data por defecto).
            WEB_USER="www-data"
            if id "nginx" &>/dev/null && ! id "www-data" &>/dev/null; then
                WEB_USER="nginx"
            fi
            chown -R "$WEB_USER:$WEB_USER" "$APIDIAN_COOKIES_DIR" 2>/dev/null || true
            chmod 775 "$APIDIAN_COOKIES_DIR"
            echo "    Directorio de cookies de apidian creado: $APIDIAN_COOKIES_DIR (owner=$WEB_USER)"
        else
            echo "    AVISO: $APIDIAN_DIR/storage/app no existe — apidian creará el dir de cookies en runtime."
        fi

        # ── Refrescar config cache de Laravel ─────────────────────────────────
        # Laravel cachea config (incluyendo TOKENDIAN_API_KEY) durante el install.
        # Si tokendian se instala DESPUÉS de ese cache, las nuevas vars no toman
        # efecto hasta el próximo deploy. config:clear las refresca ya.
        if [ -f "$APIDIAN_DIR/artisan" ]; then
            echo "    Refrescando config cache de Laravel (apidian)..."
            (cd "$APIDIAN_DIR" && php artisan config:clear 2>&1 | head -3) || \
                echo "    AVISO: config:clear falló. Hacelo a mano: cd $APIDIAN_DIR && php artisan config:clear"
        fi
    fi
fi

# ─── Arranque opcional ─────────────────────────────────────────────────────
if [ "$START_SERVICE" = "true" ]; then
    echo "==> Habilitando y arrancando el servicio"
    systemctl enable --now tokendian
    sleep 2
    if systemctl is-active --quiet tokendian; then
        echo "    tokendian.service activo. Verificando /health..."
        sleep 1
        curl -sf http://127.0.0.1:8765/health && echo || echo "    /health no respondió. Revisa: journalctl -u tokendian -n 50"
    else
        echo "    ERROR: el servicio no arrancó. Revisa: journalctl -u tokendian -n 50"
        exit 1
    fi
fi

echo
echo "==> Instalación completa."

if [ "$AUTO_KEY" != "true" ]; then
    echo "Próximos pasos manuales:"
    echo "   1. Configura .env y SERVICE_API_KEY:"
    echo "        cp $INSTALL_DIR/.env.example $INSTALL_DIR/.env"
    echo "        echo \"SERVICE_API_KEY=\$(openssl rand -hex 32)\" >> $INSTALL_DIR/.env"
    echo "        chmod 600 $INSTALL_DIR/.env"
    echo "        chown $SERVICE_USER:$SERVICE_USER $INSTALL_DIR/.env"
    echo "   2. systemctl enable --now tokendian"
fi

if [ "$START_SERVICE" != "true" ]; then
    echo "   Arranca con: systemctl enable --now tokendian"
fi

echo
echo "Las credenciales DIAN (cert, password, NIT, cédula, CapSolver key)"
echo "las envía el cliente en cada /auth/login. NO se guardan en el server."
