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
  "user_code": "1117488256",
  "company_code": "9015591465",
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

## Instalación en Linux (Debian/Ubuntu)

```bash
# 1. Clonar el repo
sudo git clone <repo-url> /opt/tokendian
cd /opt/tokendian

# 2. Ejecutar instalador (instala Python, deps, Chromium, crea unit systemd)
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
máquina, ponle Nginx delante con TLS.

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
export DIAN_USER_CODE=1117488256
export DIAN_COMPANY_CODE=9015591465
export CAPSOLVER_API_KEY=CSAPI-...
python dian_login.py
```

## Licencia

Privado / interno.
