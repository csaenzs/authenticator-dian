# tokendian

Servicio HTTP multi-tenant que mantiene sesiones autenticadas con la DIAN
(`certificate-vpfe[-hab].dian.gov.co`) usando certificado digital. Resuelve el
captcha Cloudflare Turnstile vía CapSolver y minimiza esas llamadas reusando
las cookies de sesión hasta donde DIAN lo permite.

Diseñado como microservicio: clientes (PHP, Node, Python, etc.) le piden
cookies vigentes y las usan para hablar directamente con DIAN.

## Características

- **Multi-tenant**: cada cliente envía sus credenciales en `/auth/login`. El
  servicio mantiene N sesiones simultáneas, una por `(env, NIT, cédula)`.
- **Sin almacenar credenciales sensibles**: `.p12`, password y CapSolver key
  viven solo en RAM durante el login. En disco solo quedan las cookies de
  sesión (que ya van a expirar).
- **Cache inteligente** que minimiza CapSolver:
  1. Cache en memoria por tenant.
  2. Si la última validación es reciente (TTL configurable) → usa cache directo.
  3. Si pasó el TTL → valida con `httpx` (rápido, sin browser).
  4. Si falló httpx → reintenta con browser headless.
  5. Solo si todo lo anterior falla → login completo + CapSolver.
- **Lock por tenant**: peticiones concurrentes del mismo cliente disparan un
  solo login (las demás esperan ese resultado).
- **`tenant_id` determinista**: `sha256(env|NIT|cédula)[:16]`. Mismas
  credenciales = mismo ID, siempre.

## Endpoints

Todos requieren header `X-API-Key: <SERVICE_API_KEY>` excepto `/health`.

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/health` | Ping (sin auth) |
| `POST` | `/auth/login` | Login fresco con browser + CapSolver |
| `POST` | `/auth/get_or_login` | Reusa cache si vive, sino login. **Endpoint recomendado** |
| `GET` | `/auth/cookies?tenant_id=X` | Lee cache. `410 Gone` si la sesión murió |
| `GET` | `/auth/cookies/netscape?tenant_id=X` | Cookies en formato Netscape (cURL/PHP) |
| `GET` | `/auth/status?tenant_id=X` | Estado sin tocar DIAN |
| `GET` | `/auth/tenant_id?env=&user_code=&company_code=` | Calcula el `tenant_id` determinista |

### Cuerpo de `/auth/login` y `/auth/get_or_login`

```json
{
  "certificado_base64": "MIIK...",
  "certificado_password": "secreto",
  "user_code": "1234567890",
  "company_code": "9001234567",
  "id_type": "10910094",
  "env": "hab",
  "capsolver_api_key": "CSAPI-..."
}
```

| Campo | Descripción |
|---|---|
| `certificado_base64` | `.p12` codificado en base64 |
| `certificado_password` | Contraseña del `.p12` |
| `user_code` | Cédula del representante legal |
| `company_code` | NIT empresa, sin dígito de verificación |
| `id_type` | Tipo identificación. `10910094` = Cédula CC |
| `env` | `hab` (habilitación) o `prod` (producción) |
| `capsolver_api_key` | API key de [capsolver.com](https://capsolver.com) — la paga el cliente |

### Respuesta de login

```json
{
  "tenant_id": "a3f5b9c2d8e1f4a7",
  "env": "hab",
  "cookies": [...],
  "reason": "full_login",
  "cookie_count": 8
}
```

`reason` indica qué hizo el servicio: `cache_recent_42s`, `validated_http`,
`validated_browser`, `full_login`, `force_refresh`.

## Requisitos previos

- **Python ≥ 3.9** — `patchright` requiere mínimo 3.9. Ubuntu 20.04 trae 3.8;
  ver [Instalación en Ubuntu 20.04](#instalación-en-ubuntu-2004).
- **OpenSSL ≥ 3.0** — el flag `-legacy` de `openssl pkcs12` (que el servicio
  usa para modernizar certificados `.p12` viejos) solo existe en OpenSSL 3.x.
  Ubuntu 20.04 trae 1.1.1; en ese caso compilar 3.x aparte.
- **Google Chrome** — patchright usa `channel="chrome"` (Chrome real, no
  chromium-headless-shell). El instalador lo descarga vía `patchright install
  chrome`.
- **build-essential, perl, zlib1g-dev** — solo si vas a compilar OpenSSL 3.

## Instalación

Tres caminos según tu escenario:

| Escenario | Cómo |
|---|---|
| Estás instalando **apidian** y quieres tokendian junto | `InstallAPILAMP.sh` o `InstallAPIDocker.sh` de apidian te preguntan al final si querés instalarlo. Más info en el README de apidian. |
| Querés tokendian **standalone** en LAMP/bare metal | Sección [Instalación en Linux](#instalación-en-linux-debianubuntu-2204--debian-12) más abajo |
| Querés tokendian en **Docker** standalone | Sección [Instalación con Docker](#instalación-con-docker) más abajo |

### Instalación en Linux (Debian/Ubuntu 22.04+ / Debian 12+)

Sistemas modernos donde Python ≥ 3.9 y OpenSSL ≥ 3.0 ya vienen de fábrica:

```bash
# 1. Clonar el repo
sudo git clone https://github.com/csaenzs/authenticator-dian.git /opt/tokendian
cd /opt/tokendian

# 2. Ejecutar instalador (instala deps, Chrome real, crea unit systemd)
sudo bash install-linux.sh

# 3. Configurar
sudo cp .env.example .env
echo "SERVICE_API_KEY=$(openssl rand -hex 32)" | sudo tee -a .env
sudo chmod 600 .env
sudo chown tokendian:tokendian .env

# 4. Arrancar
sudo systemctl enable --now tokendian
sudo systemctl status tokendian
sudo journalctl -u tokendian -f       # logs en vivo

# 5. Verificar
curl http://127.0.0.1:8765/health
# {"status":"ok","service":"tokendian"}
```

El servicio escucha en `127.0.0.1:8765` por defecto. Para exponerlo a otra
máquina, ponle Nginx/Apache delante con TLS — ver [Exponer en LAN](#exponer-en-lan-con-reverse-proxy).

#### Opciones de `install-linux.sh`

```
sudo bash install-linux.sh [--auto-key] [--apidian-env <ruta>] [--start]
```

| Flag | Qué hace |
|---|---|
| `--auto-key` | Genera un `SERVICE_API_KEY` aleatorio y lo escribe en `.env`. Si se omite, hay que crear el `.env` manualmente. |
| `--apidian-env <ruta>` | Escribe `TOKENDIAN_URL` y `TOKENDIAN_API_KEY` en el `.env` de apidian. Útil cuando este script lo invoca el instalador de apidian. Requiere `--auto-key` también. |
| `--start` | Habilita y arranca el servicio al final (`systemctl enable --now tokendian`). |

Ejemplo "todo en uno" para integrar con apidian instalado en `/var/www/html/apidian`:
```bash
sudo bash install-linux.sh --auto-key \
                           --apidian-env /var/www/html/apidian/.env \
                           --start
```

### Instalación con Docker

Construye la imagen y levantá el contenedor:

```bash
git clone https://github.com/csaenzs/authenticator-dian.git tokendian
cd tokendian
docker build -t tokendian:latest .

docker run -d --name tokendian \
  -p 127.0.0.1:8765:8765 \
  -e SERVICE_API_KEY=$(openssl rand -hex 32) \
  -e HEADLESS=true \
  -e VALIDATION_TTL_SECONDS=1200 \
  -v tokendian_sessions:/opt/tokendian/sessions \
  -v tokendian_profiles:/opt/tokendian/.browser-profiles \
  --restart unless-stopped \
  tokendian:latest
```

Volúmenes persistidos:
- `tokendian_sessions`: cache de cookies por tenant. Sobrevive a `docker restart`.
- `tokendian_profiles`: perfiles de Chrome por tenant.

Si vas a integrar con apidian-Docker, tokendian se monta como otro servicio en
`docker-compose.yml`. Ver el README de apidian.

## Instalación en Ubuntu 20.04 (legacy)

> ⚠️ Ubuntu 20.04 está al final de su vida. El instalador integrado de apidian
> requiere Ubuntu 22.04+. Esta sección queda como referencia para deployments
> antiguos que no pueden migrar.

Ubuntu 20.04 (focal) trae Python 3.8 y OpenSSL 1.1.1, que **no son
suficientes**. Hay que instalar Python 3.11 y OpenSSL 3 manualmente. Estos
pasos **no destruyen** los binarios de fábrica — todo queda en paths
paralelos (`/opt/uv-python`, `/opt/openssl3`).

### 1. Python 3.11 vía uv

El PPA `deadsnakes` ya no publica para focal (focal llegó a EOL). Lo más
limpio es usar `uv` (Astral), que descarga builds estándar de Python:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
mkdir -p /opt/uv-python
UV_PYTHON_INSTALL_DIR=/opt/uv-python /root/.local/bin/uv python install 3.11
chmod -R o+rX /opt/uv-python   # para que el usuario 'tokendian' pueda leerlo
```

Verifica:
```bash
ls /opt/uv-python/cpython-3.11-linux-x86_64-gnu/bin/python3.11
```

### 2. OpenSSL 3 desde fuente

```bash
sudo apt-get install -y build-essential perl zlib1g-dev
cd /tmp
curl -LO https://www.openssl.org/source/openssl-3.0.15.tar.gz
tar xzf openssl-3.0.15.tar.gz && cd openssl-3.0.15
./Configure linux-x86_64 --prefix=/opt/openssl3 --openssldir=/opt/openssl3 shared zlib
make -j$(nproc)
sudo make install_sw   # solo binarios + libs (sin manpages)

# Registra la lib en el linker dinámico (no rompe OpenSSL 1.1: filenames
# distintos: libssl.so.1.1 vs libssl.so.3)
echo "/opt/openssl3/lib64" | sudo tee /etc/ld.so.conf.d/openssl3.conf
sudo ldconfig
```

Verifica:
```bash
/opt/openssl3/bin/openssl version
# OpenSSL 3.0.15 ...
/opt/openssl3/bin/openssl pkcs12 -help 2>&1 | grep legacy
# -legacy             Use legacy encryption: 3DES_CBC for keys, RC2_CBC for certs
```

### 3. Ejecutar el instalador apuntando al Python 3.11

```bash
sudo git clone <repo-url> /opt/tokendian
cd /opt/tokendian

export PYTHON_BIN=/opt/uv-python/cpython-3.11-linux-x86_64-gnu/bin/python3.11
sudo PYTHON_BIN="$PYTHON_BIN" bash install-linux.sh
```

### 4. Decirle al servicio que use OpenSSL 3

Edita `/etc/systemd/system/tokendian.service` y añade dentro de `[Service]`:

```ini
Environment="PATH=/opt/openssl3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="LD_LIBRARY_PATH=/opt/openssl3/lib64"
```

Y añade a `ReadWritePaths` los directorios que Chrome necesita para su
crashpad (sin esto Chrome muere al arrancar bajo systemd con `ProtectSystem=strict`):

```ini
ReadWritePaths=/opt/tokendian/sessions /opt/tokendian/.browser-profiles /opt/tokendian/.config /opt/tokendian/.cache /tmp
```

Recarga y arranca:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tokendian
curl http://127.0.0.1:8765/health
```

## Configuración (`.env`)

| Variable | Descripción | Default |
|---|---|---|
| `SERVICE_API_KEY` | Token requerido en header `X-API-Key` | (requerido) |
| `HEADLESS` | Si `true`, browser sin UI | `true` |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO` |
| `VALIDATION_TTL_SECONDS` | Segundos sin re-validar la sesión | `300` |
| `SESSIONS_DIR` | Carpeta para cookies persistidas | `/opt/tokendian/sessions` |
| `BROWSER_PROFILES_ROOT` | Carpeta raíz de perfiles Playwright | `/opt/tokendian/.browser-profiles` |

## Uso desde el cliente

### Flujo recomendado (PHP, ejemplo)

**Una vez al inicio** (o tras un 410):

```bash
curl -X POST http://tokendian:8765/auth/get_or_login \
  -H "X-API-Key: $SERVICE_API_KEY" \
  -H "Content-Type: application/json" \
  -d @login.json
# Guardar el tenant_id de la respuesta
```

**En cada consulta** (rápido, no envía cert):

```bash
curl http://tokendian:8765/auth/cookies/netscape?tenant_id=a3f5b9c2d8e1f4a7 \
  -H "X-API-Key: $SERVICE_API_KEY" \
  > cookies.txt

# Si responde 410, la sesión expiró → vuelve al paso 1
# Si responde 200 → usa cookies.txt con CURLOPT_COOKIEFILE
```

Con esto, una empresa que consulta 1000 veces en una hora gasta solo **1 captcha**
(el del primer login). El resto son requests de ~50ms sin browser ni CapSolver.

### Preparar el certificado base64

Si tu `.p12` es legacy (DIAN suele entregarlos en RC2/3DES+SHA1), conviértelo
primero — Playwright/OpenSSL 3 no leen los legacy:

```bash
# Linux
DIAN_CERT_PASSWORD='tu_pwd' bash convert-cert-linux.sh /ruta/al/cert-original.p12

# Codifica a base64 (pasa al campo certificado_base64)
base64 -w 0 cert-modern.p12
```

```powershell
# Windows
$bytes = [IO.File]::ReadAllBytes("cert-modern.p12")
[Convert]::ToBase64String($bytes) | Set-Clipboard
```

## Seguridad

- Credenciales (`.p12`, password, CapSolver key) **nunca tocan disco**.
- El `.p12` se materializa en `/tmp/dian_cert_*.p12` con permisos `0600`
  durante el login y se borra siempre (incluso si Playwright falla).
- Cookies y metadatos se guardan en `sessions/{tenant_id}.json` con permisos `0600`.
- Cada tenant tiene su perfil de browser aislado en `.browser-profiles/{tenant_id}/`.
- El `SERVICE_API_KEY` autentica todos los endpoints sensibles. Genera uno
  largo (`openssl rand -hex 32`) y rota periódicamente.
- El servicio escucha en `127.0.0.1` por defecto. **No** lo expongas
  directamente a internet — ponle reverse proxy con TLS.

## Troubleshooting

| Síntoma | Causa probable |
|---|---|
| `502 DianLoginRejected: ... cert, contraseña, NIT y cédula` | Datos incorrectos o `.p12` legacy sin convertir |
| `502 CapSolverError: createTask falló` | API key de CapSolver inválida o sin saldo |
| `410 Gone: tenant_not_found` | Es la primera vez para ese cliente. Llama `/auth/login` |
| `410 Gone: cloudflare_blocked` | Cloudflare rechazó la validación httpx. Reintenta — el servicio caerá a validación con browser |
| `500 SERVICE_API_KEY no configurada` | Falta el `.env` o no se cargó al arrancar |
| Login se cuelga en headless | Probar con `HEADLESS=false` localmente para ver qué pasa |

## Modo desarrollo / debug local

```bash
cd /opt/tokendian
source .venv/bin/activate
export SERVICE_API_KEY=dev
uvicorn server:app --host 127.0.0.1 --port 8765 --reload
```

O mantén el script Python en su modo CLI (compatibilidad con la versión 1.x):

```bash
export DIAN_CERT_PATH=/ruta/cert-modern.p12
export DIAN_CERT_PASSWORD=...
export DIAN_USER_CODE=1234567890
export DIAN_COMPANY_CODE=9001234567
export CAPSOLVER_API_KEY=CSAPI-...
python dian_login.py
```

## Exponer en LAN con reverse proxy

Por seguridad, el servicio escucha solo en `127.0.0.1:8765`. Para que
clientes en otra máquina lo consuman, ponle un reverse proxy delante.

### Ejemplo con Apache

Habilita los módulos necesarios y crea un vhost:

```bash
sudo a2enmod proxy proxy_http headers
```

`/etc/apache2/sites-available/tokendian.conf`:

```apache
<VirtualHost *:80>
    ServerName tokendian.local

    ProxyRequests Off
    ProxyPreserveHost On
    ProxyTimeout 120

    RequestHeader set X-Forwarded-Proto "http"
    RequestHeader set X-Real-IP "%{REMOTE_ADDR}s"

    ProxyPass        / http://127.0.0.1:8765/
    ProxyPassReverse / http://127.0.0.1:8765/

    ErrorLog  ${APACHE_LOG_DIR}/tokendian-error.log
    CustomLog ${APACHE_LOG_DIR}/tokendian-access.log combined
</VirtualHost>
```

```bash
sudo a2ensite tokendian.conf
sudo systemctl reload apache2
```

### Ejemplo con Nginx

`/etc/nginx/sites-available/tokendian`:

```nginx
server {
    listen 80;
    server_name tokendian.local;

    location / {
        proxy_pass         http://127.0.0.1:8765;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/tokendian /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### Recomendaciones

- **TLS**: en LAN privada con IP interna no aplica Let's Encrypt; usa cert
  auto-firmado o tu CA interna. Añade `listen 443 ssl;` (nginx) o `<VirtualHost
  *:443>` con `SSLEngine on` (apache).
- **Restricción por IP**: si solo una máquina debe consumir, agrega
  `Require ip 192.168.10.0/24` (apache) o `allow 192.168.10.0/24; deny all;`
  (nginx).
- **Firewall**: `ufw allow from 192.168.10.0/24 to any port 80`.

## Licencia

Privado / interno.
