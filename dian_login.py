"""Módulo de autenticación con DIAN usando certificado digital.

Expone:
- login()              : ejecuta el flujo completo (Playwright + CapSolver)
- validate_cookies_http(): valida cookies cacheadas SIN abrir browser (rápido)

Las funciones aceptan config explícita; si no se pasa, leen variables de entorno.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

import httpx
from patchright.async_api import async_playwright

URLS = {
    "hab":  {"catalogo": "https://catalogo-vpfe-hab.dian.gov.co",
             "cert":     "https://certificate-vpfe-hab.dian.gov.co"},
    "prod": {"catalogo": "https://catalogo-vpfe.dian.gov.co",
             "cert":     "https://certificate-vpfe.dian.gov.co"},
}

TURNSTILE_SITEKEYS = {
    "hab":  "0x4AAAAAAAg1WuNb-OnOa76z",
    "prod": "0x4AAAAAAAg1WuNb-OnOa76z",
}

ROOT             = Path(__file__).resolve().parent
DEFAULT_PROFILE  = str(ROOT / ".browser-profile")
DEFAULT_COOKIES  = ROOT / "secrets" / "cookies.json"

CAPSOLVER_BASE = "https://api.capsolver.com"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class TurnstileChallengeError(RuntimeError):
    pass


class CapSolverError(RuntimeError):
    pass


class DianLoginRejected(RuntimeError):
    """DIAN procesó el login pero lo rechazó (cert inválido, datos mal, etc.)."""


# ---------- persistencia de cookies ----------

def save_cookies(cookies: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")


def load_cookies(path: Path) -> Optional[list[dict]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------- validación HTTP (sin browser) ----------

def _cookies_to_httpx(cookies: list[dict]) -> httpx.Cookies:
    jar = httpx.Cookies()
    for c in cookies:
        try:
            jar.set(
                name=c["name"],
                value=c["value"],
                domain=c.get("domain", "").lstrip("."),
                path=c.get("path", "/"),
            )
        except Exception:
            continue
    return jar


def validate_cookies_http(cookies: list[dict], env: str = "hab") -> dict:
    """Valida una sesión sin abrir browser.

    Hace un GET ligero al portal del catalogo. Si redirige a /User/Login,
    la sesión expiró. Si Cloudflare bloquea, no podemos saber con certeza,
    devolvemos status='unknown'.
    """
    if env not in URLS:
        return {"valid": False, "status": "config_error", "detail": f"env inválido: {env}"}

    base_catalogo = URLS[env]["catalogo"]
    url = f"{base_catalogo}/"
    jar = _cookies_to_httpx(cookies)

    try:
        with httpx.Client(
            cookies=jar,
            follow_redirects=True,
            timeout=15.0,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
            },
        ) as client:
            r = client.get(url)
    except httpx.HTTPError as e:
        return {"valid": False, "status": "http_error", "detail": str(e)}

    final_url = str(r.url)
    body_lower = (r.text or "").lower()

    # Cloudflare bloqueó la validación: no podemos confirmar sesión
    if "cloudflare" in body_lower and ("attention required" in body_lower or "blocked" in body_lower):
        return {"valid": False, "status": "cloudflare_blocked", "final_url": final_url}

    # Redirige a login = sesión expirada
    if "/user/login" in final_url.lower() or "/user/certificatelogin" in final_url.lower():
        return {"valid": False, "status": "expired", "final_url": final_url}

    if r.status_code == 200 and base_catalogo in final_url:
        return {"valid": True, "status": "ok", "final_url": final_url}

    return {"valid": False, "status": "unknown", "final_url": final_url, "http_code": r.status_code}


# ---------- CapSolver ----------

async def _solve_turnstile(api_key: str, sitekey: str, page_url: str) -> str:
    async with httpx.AsyncClient(timeout=30.0) as client:
        create = await client.post(
            f"{CAPSOLVER_BASE}/createTask",
            json={
                "clientKey": api_key,
                "task": {
                    "type": "AntiTurnstileTaskProxyLess",
                    "websiteURL": page_url,
                    "websiteKey": sitekey,
                },
            },
        )
        data = create.json()
        if data.get("errorId") != 0:
            raise CapSolverError(f"createTask falló: {data.get('errorDescription', data)}")
        task_id = data["taskId"]

        for _ in range(60):
            await asyncio.sleep(2)
            res = await client.post(
                f"{CAPSOLVER_BASE}/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
            )
            r = res.json()
            if r.get("errorId") != 0:
                raise CapSolverError(f"getTaskResult falló: {r.get('errorDescription', r)}")
            status = r.get("status")
            if status == "ready":
                token = r["solution"]["token"]
                if not token:
                    raise CapSolverError("CapSolver devolvió token vacío.")
                return token
            if status == "failed":
                raise CapSolverError(f"CapSolver marcó la tarea como failed: {r}")

        raise CapSolverError("CapSolver: timeout esperando solución (>120s).")


# ---------- validación con browser real ----------

async def _validate_saved_cookies_browser(p, base_cert: str, cert_path: str, cert_pwd: str,
                                          cookies: list[dict], user_data_dir: str) -> Optional[list[dict]]:
    if not cookies:
        return None

    context = await p.chromium.launch_persistent_context(
        user_data_dir=user_data_dir + "_validate",
        channel="chrome",
        headless=True,
        no_viewport=True,
        client_certificates=[{
            "origin": base_cert,
            "pfxPath": cert_path,
            "passphrase": cert_pwd,
        }],
        locale="es-CO",
        timezone_id="America/Bogota",
    )
    try:
        await context.add_cookies(cookies)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(f"{base_cert}/", wait_until="domcontentloaded", timeout=20000)
        if "/User/Login" in page.url or page.url.rstrip("/").endswith("/Login"):
            return None
        if not page.url.startswith(base_cert):
            return None
        return await context.cookies()
    except Exception:
        return None
    finally:
        await context.close()


# ---------- login completo (browser + CapSolver) ----------

async def _login_with_capsolver(
    p, env: str, base_catalogo: str, base_cert: str,
    cert_path: str, cert_pwd: str,
    user_code: str, comp_code: str, id_type: str,
    capsolver_key: str, headless: bool, user_data_dir: str,
) -> list[dict]:
    context = await p.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        channel="chrome",
        headless=headless,
        no_viewport=True,
        client_certificates=[{
            "origin": base_cert,
            "pfxPath": cert_path,
            "passphrase": cert_pwd,
        }],
        locale="es-CO",
        timezone_id="America/Bogota",
    )
    page = context.pages[0] if context.pages else await context.new_page()

    try:
        await page.goto(f"{base_catalogo}/User/Login", wait_until="domcontentloaded")
        await page.click("text=Certificado")
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_selector("select[name=IdentificationType]", state="visible", timeout=15000)

        sitekey = await page.evaluate(
            """() => {
                const el = document.querySelector('[data-sitekey]');
                return el ? el.getAttribute('data-sitekey') : null;
            }"""
        ) or TURNSTILE_SITEKEYS[env]
        page_url = page.url

        token = await _solve_turnstile(capsolver_key, sitekey, page_url)

        await page.evaluate(
            """([token]) => {
                let el = document.querySelector('input[name="cf-turnstile-response"]');
                if (!el) {
                    el = document.createElement('input');
                    el.type = 'hidden';
                    el.name = 'cf-turnstile-response';
                    document.querySelector('form').appendChild(el);
                }
                el.value = token;
            }""",
            [token],
        )

        await page.select_option("select[name=IdentificationType]", value=id_type)
        await page.fill("input[name=UserCode]", user_code)

        company_readonly = await page.evaluate(
            """() => {
                const el = document.querySelector('input[name="CompanyCode"]');
                return !!(el && (el.readOnly || el.hasAttribute('readonly')));
            }"""
        )
        if not company_readonly:
            await page.fill("input[name=CompanyCode]", comp_code)

        submit_btn = page.locator(
            'button[type="submit"], input[type="submit"], button:has-text("Entrar")'
        ).first
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=45000):
            await submit_btn.click()

        if "/User/Login" in page.url or "/User/CertificateLogin" in page.url:
            raise DianLoginRejected(
                f"Login rechazado por DIAN tras submit. URL final: {page.url}. "
                "Verifica cert, contraseña, NIT y cédula."
            )
        if not page.url.startswith(base_cert):
            raise DianLoginRejected(f"Redirección inesperada. URL final: {page.url}")

        # PARCHE LOCAL: networkidle (no domcontentloaded). El submit del form
        # de login dispara una cadena de redirects (catalogo → certificate →
        # certificate/User/Authenticated → ...) que con domcontentloaded se
        # interrumpe a medio camino, dejando la sesión en estado inconsistente.
        # networkidle espera ~500ms sin requests, capturando todos los redirects.
        # Le damos un pequeño buffer extra para cookies HttpOnly que se setean
        # tras el último redirect.
        await page.goto(f"{base_cert}/", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(500)
        if "/User/Login" in page.url or "/User/CertificateLogin" in page.url:
            raise DianLoginRejected(
                f"Sesión no quedó establecida. Tras visitar dashboard redirigió a login: {page.url}"
            )

        # Doble check: verificar que la cookie de auth (.AspNet.ApplicationCookie)
        # haya sido emitida. Hay casos donde DIAN responde 200 en base_cert/ sin
        # haber establecido sesión real (anti-bot silencioso, edge case en handoff).
        # Sin este check, devolveríamos cookies incompletas y los consumidores
        # quedarían en loop: validan cache (existe), llaman al endpoint, reciben
        # 302 a /User/Login, no se auto-recuperan.
        cookies = await context.cookies()
        if not any(c.get("name") == ".AspNet.ApplicationCookie" for c in cookies):
            raise DianLoginRejected(
                "Login completó (URL OK) pero DIAN no emitió .AspNet.ApplicationCookie. "
                "Posible rate-limit, anti-bot o sesión rechazada silenciosamente."
            )
        return cookies
    finally:
        await context.close()


# ---------- API pública ----------

async def login(
    *,
    env: Optional[str] = None,
    cert_path: Optional[str] = None,
    cert_pwd: Optional[str] = None,
    user_code: Optional[str] = None,
    comp_code: Optional[str] = None,
    id_type: Optional[str] = None,
    capsolver_key: Optional[str] = None,
    headless: Optional[bool] = None,
    force: bool = False,
    cookies_path: Optional[Path] = None,
    user_data_dir: Optional[str] = None,
) -> dict:
    """Ejecuta el flujo de login DIAN. Retorna dict con cookies + metadatos.

    Si ya hay cookies guardadas y siguen válidas (validación con browser),
    las reutiliza salvo que force=True.
    """
    env           = env           or os.getenv("DIAN_ENV", "hab")
    cert_path     = cert_path     or os.environ["DIAN_CERT_PATH"]
    cert_pwd      = cert_pwd      or os.environ["DIAN_CERT_PASSWORD"]
    user_code     = user_code     or os.environ["DIAN_USER_CODE"]
    comp_code     = comp_code     or os.environ["DIAN_COMPANY_CODE"]
    id_type       = id_type       or os.getenv("DIAN_ID_TYPE", "10910094")
    capsolver_key = capsolver_key or os.environ["CAPSOLVER_API_KEY"]
    if headless is None:
        headless = os.getenv("HEADLESS", "true").lower() == "true"
    cookies_path  = cookies_path  or Path(os.getenv("COOKIES_PATH", str(DEFAULT_COOKIES)))
    user_data_dir = user_data_dir or os.getenv("BROWSER_PROFILE_DIR", DEFAULT_PROFILE)

    if env not in URLS:
        raise RuntimeError(f"DIAN_ENV inválido: {env!r}. Usa 'hab' o 'prod'.")

    base_catalogo = URLS[env]["catalogo"]
    base_cert     = URLS[env]["cert"]

    async with async_playwright() as p:
        cookies: Optional[list[dict]] = None
        reused = False

        if not force:
            saved = load_cookies(cookies_path)
            if saved:
                refreshed = await _validate_saved_cookies_browser(
                    p, base_cert, cert_path, cert_pwd, saved, user_data_dir
                )
                if refreshed:
                    cookies = refreshed
                    reused = True

        if cookies is None:
            cookies = await _login_with_capsolver(
                p, env, base_catalogo, base_cert,
                cert_path, cert_pwd,
                user_code, comp_code, id_type,
                capsolver_key, headless, user_data_dir,
            )

        save_cookies(cookies, cookies_path)

        return {
            "cookies": cookies,
            "env": env,
            "reused": reused,
            "cookies_path": str(cookies_path),
        }


# ---------- modo CLI (compatibilidad) ----------

if __name__ == "__main__":
    try:
        result = asyncio.run(login(force=os.getenv("FORCE_LOGIN", "false").lower() == "true"))
    except KeyError as e:
        print(f"Variable de entorno requerida no definida: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Login OK. reused={result['reused']}. {len(result['cookies'])} cookies.")
    for c in result["cookies"]:
        print(f"  - {c.get('name', ''):40} domain={c.get('domain', ''):50} httpOnly={c.get('httpOnly', False)} secure={c.get('secure', False)}")
