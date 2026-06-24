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
