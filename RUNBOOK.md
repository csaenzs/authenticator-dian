# RUNBOOK — Autenticación DIAN en producción (Azure WAF)

Guía operativa para que el login a **producción** de la DIAN funcione, y para
replicar el arreglo en otros servidores.

---

## 1. El problema

Desde ~junio 2026 la DIAN protege el portal de **producción**
(`catalogo-vpfe.dian.gov.co`) con un **WAF de Azure** que presenta un
**JavaScript Challenge** antes de la página de login.

- Un **Chrome headless** es detectado y **bloqueado en duro**:
  `403 "Solicitud bloqueada por controles de seguridad"`. El login ni siquiera
  llega al formulario.
- Un **Chrome real (headed)** resuelve el reto JS solo (~3 s) y pasa.
- **Habilitación** (`catalogo-vpfe-hab.dian.gov.co`) **no** tiene este WAF, por
  eso seguía funcionando en headless y daba la falsa impresión de que "a veces sí".

Síntoma típico en el cliente: el login de producción falla mientras el de
habilitación funciona.

---

## 2. La solución (3 piezas, todas necesarias)

| # | Cambio | Sin él |
|---|--------|--------|
| 1 | `HEADLESS=false` en `.env` → navegador **headed** | Azure WAF bloquea (`403`), el login nunca arranca |
| 2 | Arrancar `uvicorn` bajo **`xvfb-run`** (display virtual) | El navegador headed no tiene pantalla → no abre |
| 3 | `Environment` apuntando a **OpenSSL 3** (`/opt/openssl3`) | El `.p12` legacy de la DIAN no carga → `Unsupported TLS certificate` |

> El punto 3 solo aplica en **Ubuntu 20.04** (su `openssl` de sistema es 1.1.1,
> sin el flag `-legacy` que el servicio usa para modernizar el `.p12`). En
> Ubuntu 22.04+ el OpenSSL 3 ya es de sistema y las rutas `/opt/openssl3` se
> ignoran sin efecto.

### Detalle de los cambios

**`.env`**
```ini
HEADLESS=false
```

**`/etc/systemd/system/tokendian.service`** (sección `[Service]`)
```ini
# OpenSSL 3 (Ubuntu 20.04: el de sistema es 1.1.1 sin flag -legacy)
Environment="PATH=/opt/openssl3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="LD_LIBRARY_PATH=/opt/openssl3/lib64"

# Chrome headed bajo Xvfb (evade el Azure WAF de producción)
ExecStart=/usr/bin/xvfb-run -a --server-args="-screen 0 1920x1080x24 -ac -nolisten tcp" /opt/tokendian/.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8765 --workers 1
```

Estos ajustes ya vienen en el `tokendian.service` del repo. El **`.env` real no
se versiona**, así que `HEADLESS=false` se pone por server.

---

## 3. Desplegar en un servidor nuevo

### Paso 0 — Verificar ANTES de tocar nada
```bash
lsb_release -rs ; openssl version          # ¿20.04? ¿openssl de sistema?
/opt/openssl3/bin/openssl version          # en 20.04 debe decir "OpenSSL 3.x"
```
- `OpenSSL 3.x` presente → continúa.
- **20.04 sin `/opt/openssl3`** → ⚠️ PARA. Instala OpenSSL 3 primero
  (sección "Instalación en Ubuntu 20.04" del README) o ajusta las rutas del unit.
- **22.04+** → no aplica, continúa tranquilo.

### Pasos 1-4 — Aplicar

**En un solo comando (instalaciones existentes):** `sudo bash /opt/tokendian/update-linux.sh`
hace todos los Pasos 1-4 de forma idempotente (pull + xvfb + fuerza HEADLESS=false
+ reinstala el unit + restart + health). Lo de abajo es el detalle manual equivalente.

```bash
sudo -u tokendian git -C /opt/tokendian pull
sudo apt-get install -y xvfb
sudo sed -i 's/^HEADLESS=.*/HEADLESS=false/' /opt/tokendian/.env
sudo cp /opt/tokendian/tokendian.service /etc/systemd/system/tokendian.service
sudo systemctl daemon-reload && sudo systemctl restart tokendian
```

### Paso 5 — Verificar
```bash
systemctl is-active tokendian                        # → active
journalctl -u tokendian -n 10 | grep -i headless     # → headless=False
systemctl status tokendian | grep Xvfb               # → Xvfb :99 corriendo
curl -s http://127.0.0.1:8765/health                 # → {"status":"ok",...}

# Entorno del proceso (PATH debe empezar con /opt/openssl3/bin):
PID=$(pgrep -f "uvicorn server:app" | head -1)
sudo tr '\0' '\n' < /proc/$PID/environ | grep -E "^PATH=|^LD_LIBRARY_PATH=|^HEADLESS="
```

### Paso 6 — Prueba real
Una consulta de **producción** (no habilitación) desde apidian. Si pasa, listo.

---

## 4. Los 2 "gotchas" por servidor

1. **`HEADLESS=false` en el `.env` real** — no está en git, va a mano (lo hace el `sed`).
2. **OpenSSL 3 en `/opt/openssl3`** en los 20.04 — verificar en el Paso 0.

---

## 5. Troubleshooting

| Síntoma (journal) | Causa | Arreglo |
|---|---|---|
| `403 "Solicitud bloqueada por controles de seguridad"` | Corriendo headless | `HEADLESS=false` + `xvfb-run` (reiniciar) |
| `Unsupported TLS certificate` / `algorithm ... deprecated by OpenSSL` | `.p12` legacy no modernizado: falta OpenSSL 3 | Verificar `Environment` PATH→`/opt/openssl3/bin` y que exista `/opt/openssl3` |
| `openssl ... Unrecognized flag legacy` | Está usando openssl 1.1.1 del sistema | Mismo que arriba: el proceso debe ver OpenSSL 3 |
| `xvfb-run: command not found` | Falta el paquete | `apt-get install xvfb` |
| `headless=True` en el log de arranque | El `.env` no se actualizó | `sed -i 's/^HEADLESS=.*/HEADLESS=false/' .env` + restart |
| `DianLoginRejected: Sesión no quedó establecida. Tras visitar dashboard redirigió a login` | **No es rechazo del login** (ver §7): el submit pasó y la sesión se pierde en la navegación siguiente | §7 — perfil persistente, luego versiones |
| `Login completó (URL OK) pero DIAN no emitió .AspNet.ApplicationCookie` | Misma causa que la fila anterior, cortando un paso más tarde | §7 |

---

## 6. Notas

- Los cambios de OpenSSL 3 están **scopeados solo al proceso de tokendian** (vía
  `Environment=` del unit). **No afectan** el OpenSSL del sistema ni el de PHP,
  por lo tanto **no afectan la firma de documentos electrónicos** (que apidian
  hace por su cuenta con su propio openssl y certificado).
- El `.p12` que tokendian moderniza es una **copia temporal** en `/tmp` usada
  solo para la conexión TLS del navegador; se borra al terminar y no toca el
  certificado original.
- Habilitación funciona con o sin estos cambios (headed funciona igual; headless
  también, porque hab no tiene el WAF).

---

## 7. Divergencia de versiones entre servidores (Chrome / patchright)

Caso EMSSANAR, 2026-09-24: import DIAN fallando siempre, determinista, con
`DianLoginRejected: Sesión no quedó establecida. Tras visitar dashboard redirigió
a login`. Mismo commit de tokendian que un servidor que sí autentica. Descartados
reloj, certificado y versión del repo.

### Leer bien el error antes de buscar culpables

Ese mensaje es `dian_login.py:301-304`, y para llegar ahí el flujo ya pasó dos
guardas anteriores:

- `:284` — `"/User/Login" in page.url or "/User/CertificateLogin" in page.url`
  tras el submit. **No disparó.**
- `:289` — `not page.url.startswith(base_cert)`. **No disparó.**

Es decir: **certificado, Turnstile/CapSolver y submit funcionaron**, y la URL
final ya estaba dentro de `base_cert`. Lo que falla es el **segundo**
`goto(base_cert + "/")` de `:299`. La sesión se establece y se pierde en la
navegación siguiente. No perder tiempo revisando cert, contraseña, NIT o
CapSolver: el propio código ya los descartó.

### Por qué ese paso depende del navegador

```python
await page.goto(f"{base_cert}/", wait_until="networkidle", timeout=30000)
await page.wait_for_timeout(500)
```

El submit dispara una cadena de redirects (catálogo → certificate →
certificate/User/Authenticated → …) y `.AspNet.ApplicationCookie` es HttpOnly y
se emite tras el último. `networkidle` es una heurística de tiempo (~500 ms sin
requests) y el buffer de 500 ms es fijo: **el corte cae en un momento distinto
según el build del navegador**. Si cae antes de la cookie, el `goto` termina en
`/User/Login` (`:303`); si cae justo después de la URL pero antes de la cookie,
muere en el doble check de `:313`. Son dos ramas del mismo defecto.

### Las dos versiones que divergen sin control

| Componente | Quién la fija | Quién la actualiza |
|---|---|---|
| Google Chrome | `install-linux.sh:101` → `patchright install chrome` (instala el Chrome del sistema vía apt, por eso corre como root) | **nadie** — queda la del día de instalación o la del último `apt upgrade` |
| patchright | `requirements.txt` → `patchright>=1.50.0`, **sin pin** | **nadie** — `update-linux.sh` no corre `pip install -r` |

`update-linux.sh` hace pull, xvfb, `HEADLESS=false`, unit y restart: **ni Chrome
ni venv**. Así que dos servidores instalados en fechas distintas corren código
idéntico sobre pilas distintas, y nada lo delata. patchright pesa aquí tanto como
Chrome: es quien implementa `client_certificates` (`dian_login.py:225-229`), por
donde entra el `.p12`.

### Orden de diagnóstico (de lo barato a lo caro)

1. **Perfil persistente.** `auth_service.py:284` usa `launch_persistent_context`
   con un `user_data_dir` por tenant bajo `.browser-profiles`. Un perfil con
   cookies viejas reproduce este síntoma exacto, es determinista y **sobrevive a
   todos los `restart` de `update-linux.sh`** — por eso "el update no arregla
   nada". Reversible en 2 minutos:
   ```bash
   systemctl stop tokendian
   mv /opt/tokendian/.browser-profiles/<tenant> /opt/tokendian/.browser-profiles/<tenant>.bak
   systemctl start tokendian
   ```
   Si pasa, no era versión de nada.
2. **Censo de las dos máquinas** (la que falla y una que funcione):
   ```bash
   google-chrome --version
   /opt/tokendian/.venv/bin/pip show patchright | grep -i version
   git -C /opt/tokendian rev-parse --short HEAD
   journalctl -u tokendian -n 80 --no-pager | grep -i "DianLoginRejected\|ApplicationCookie"
   ```
3. Solo entonces, alinear versiones.

### Criterio sobre fijar versiones

- **patchright: sí se pinea.** Es dependencia nuestra y el pin es lo único que
  impide que los servidores diverjan.
- **Chrome: no se pinea.** Contra un WAF que se mueve, quedarse clavado en un
  build viejo es el incidente del mes siguiente: lo que hoy pasa el filtro, en
  tres meses es la firma rara. Que siga el estable, pero **sincronizado por el
  update**, no por azar.
- Una diferencia de versión entre dos servidores es **correlación**, no causa,
  mientras no se reproduzca. Si tras alinear Chrome el fallo sigue, la hipótesis
  muere y el arreglo es en `dian_login.py`: reintentar el `goto` comprobando
  `.AspNet.ApplicationCookie` en `context.cookies()` entre intentos, en vez de
  confiar en `networkidle` + 500 ms. Determinista y sin depender del build.

### Lo que falta en los scripts

- `update-linux.sh`: `pip install -r requirements.txt` y `patchright install
  chrome` (el mismo comando que la instalación, idempotente), más un censo
  impreso al final (`google-chrome --version`, versión de patchright, commit,
  `HEADLESS`). Ese censo es lo que habría reducido este caso de dos días de
  descarte a cinco segundos.
- `/health` (`server.py:190-192`) devuelve `{"status":"ok","service":"tokendian"}`:
  no permite auditar una flota sin entrar a cada servidor. Debería reportar
  Chrome, patchright, `headless` y commit — versiones, no un mínimo exigido, que
  nadie ha medido.
