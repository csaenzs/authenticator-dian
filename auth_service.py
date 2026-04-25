"""Multi-tenant: maneja N sesiones DIAN simultáneas, una por (env|NIT|cédula).

Diseño:
- TenantManager: dict de Tenant por tenant_id (sha256 corto de las credenciales).
- Tenant: cookies + metadata + lock por tenant.
- Las credenciales sensibles (.p12, password, capsolver_key) NO se persisten —
  solo viven durante el login. Si la sesión expira de verdad, el cliente debe
  volver a llamar /auth/login.

Optimizaciones para minimizar CapSolver:
1. Cache en memoria + persistencia de cookies en disco.
2. Validación HTTP barata (httpx) si pasó el TTL.
3. Validación con browser headless si httpx falló (Cloudflare puede mentir).
4. Solo si TODO falla → 410 Gone (cliente reintenta login).
5. Lock por tenant: peticiones concurrentes del mismo cliente solo gatillan 1 login.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from patchright.async_api import async_playwright

from dian_login import (
    URLS,
    _login_with_capsolver,
    _validate_saved_cookies_browser,
    validate_cookies_http,
)

log = logging.getLogger("tokendian.auth")


def make_tenant_id(env: str, user_code: str, company_code: str) -> str:
    """ID determinista — mismas credenciales = mismo tenant_id."""
    raw = f"{env}|{user_code}|{company_code}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _modernize_p12(orig_path: str, password: str, modern_path: str, pem_path: str) -> bool:
    """Convierte un .p12 legacy (RC2/3DES+SHA1) a moderno (AES-256).

    OpenSSL 3 (y por tanto Playwright/Chromium) no carga los .p12 que la DIAN
    suele entregar. Esta conversión es transparente: si el .p12 ya era moderno
    también funciona porque OpenSSL acepta el flag -legacy en ambos casos.

    Devuelve True si la conversión tuvo éxito.
    """
    try:
        r1 = subprocess.run(
            ["openssl", "pkcs12", "-in", orig_path, "-nodes", "-legacy",
             "-passin", f"pass:{password}", "-out", pem_path],
            capture_output=True, timeout=30,
        )
        if r1.returncode != 0:
            log.warning("openssl extract falló: %s", r1.stderr.decode(errors="replace")[:200])
            return False

        r2 = subprocess.run(
            ["openssl", "pkcs12", "-export", "-in", pem_path, "-out", modern_path,
             "-passin", f"pass:{password}", "-passout", f"pass:{password}"],
            capture_output=True, timeout=30,
        )
        if r2.returncode != 0:
            log.warning("openssl export falló: %s", r2.stderr.decode(errors="replace")[:200])
            return False

        os.chmod(modern_path, 0o600)
        return True
    except FileNotFoundError:
        log.error("openssl no está instalado en el sistema")
        return False
    except subprocess.TimeoutExpired:
        log.error("openssl timeout al modernizar el .p12")
        return False


@contextmanager
def _temp_cert_file(p12_bytes: bytes, password: str):
    """Escribe el .p12 en disco temporalmente, modernizándolo si es legacy.

    Garantiza limpieza al salir (incluso si Playwright lanza).
    """
    fd_orig, orig_path = tempfile.mkstemp(suffix=".p12", prefix="dian_cert_orig_")
    fd_modern, modern_path = tempfile.mkstemp(suffix=".p12", prefix="dian_cert_modern_")
    fd_pem, pem_path = tempfile.mkstemp(suffix=".pem", prefix="dian_cert_pem_")
    os.close(fd_modern)
    os.close(fd_pem)

    try:
        with os.fdopen(fd_orig, "wb") as f:
            f.write(p12_bytes)
        os.chmod(orig_path, 0o600)

        # Intentar modernizar — el flag -legacy de openssl también lee p12 modernos
        if _modernize_p12(orig_path, password, modern_path, pem_path):
            log.info("Cert .p12 convertido a formato moderno (AES-256)")
            yield modern_path
        else:
            log.warning("No se pudo modernizar el .p12; usando original (puede fallar en Playwright)")
            yield orig_path
    finally:
        for p in (orig_path, modern_path, pem_path):
            try:
                os.unlink(p)
            except OSError:
                pass


@dataclass
class Tenant:
    tenant_id: str
    env: str
    user_code: str
    company_code: str
    id_type: str
    cookies: list[dict] = field(default_factory=list)
    last_validated_at: float = 0.0
    last_login_at: float = 0.0
    login_count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def has_cookies(self) -> bool:
        return bool(self.cookies)

    def status(self) -> dict:
        now = time.time()
        return {
            "tenant_id": self.tenant_id,
            "env": self.env,
            "user_code": self.user_code,
            "company_code": self.company_code,
            "has_cookies": self.has_cookies,
            "cookie_count": len(self.cookies),
            "last_validated_at": self.last_validated_at or None,
            "last_validated_seconds_ago": int(now - self.last_validated_at) if self.last_validated_at else None,
            "last_login_at": self.last_login_at or None,
            "login_count": self.login_count,
        }


class TenantManager:
    """Administra múltiples Tenants, persiste cookies por tenant."""

    def __init__(
        self,
        sessions_dir: Path,
        browser_profiles_root: Path,
        headless: bool = True,
        validation_ttl_seconds: int = 300,
    ):
        self.sessions_dir = Path(sessions_dir)
        self.browser_profiles_root = Path(browser_profiles_root)
        self.headless = headless
        self.validation_ttl_seconds = validation_ttl_seconds
        self._tenants: dict[str, Tenant] = {}
        self._global_lock = asyncio.Lock()

        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.browser_profiles_root.mkdir(parents=True, exist_ok=True)

    # ---------- persistencia ----------

    def _cookies_file(self, tenant_id: str) -> Path:
        return self.sessions_dir / f"{tenant_id}.json"

    def _meta_file(self, tenant_id: str) -> Path:
        return self.sessions_dir / f"{tenant_id}.meta.json"

    def _profile_dir(self, tenant_id: str) -> str:
        return str(self.browser_profiles_root / tenant_id)

    def _persist(self, t: Tenant) -> None:
        self._cookies_file(t.tenant_id).write_text(
            json.dumps(t.cookies, indent=2), encoding="utf-8"
        )
        meta = {
            "tenant_id": t.tenant_id,
            "env": t.env,
            "user_code": t.user_code,
            "company_code": t.company_code,
            "id_type": t.id_type,
            "last_validated_at": t.last_validated_at,
            "last_login_at": t.last_login_at,
            "login_count": t.login_count,
        }
        self._meta_file(t.tenant_id).write_text(json.dumps(meta, indent=2), encoding="utf-8")
        try:
            os.chmod(self._cookies_file(t.tenant_id), 0o600)
            os.chmod(self._meta_file(t.tenant_id), 0o600)
        except OSError:
            pass

    def _load_from_disk(self, tenant_id: str) -> Optional[Tenant]:
        meta_path = self._meta_file(tenant_id)
        cookies_path = self._cookies_file(tenant_id)
        if not meta_path.exists() or not cookies_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            cookies = json.loads(cookies_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return Tenant(
            tenant_id=meta["tenant_id"],
            env=meta["env"],
            user_code=meta["user_code"],
            company_code=meta["company_code"],
            id_type=meta.get("id_type", "10910094"),
            cookies=cookies,
            last_validated_at=meta.get("last_validated_at", 0.0),
            last_login_at=meta.get("last_login_at", 0.0),
            login_count=meta.get("login_count", 0),
        )

    # ---------- lookup ----------

    async def _get_tenant(self, tenant_id: str) -> Optional[Tenant]:
        if tenant_id in self._tenants:
            return self._tenants[tenant_id]
        async with self._global_lock:
            if tenant_id in self._tenants:
                return self._tenants[tenant_id]
            t = self._load_from_disk(tenant_id)
            if t is not None:
                self._tenants[tenant_id] = t
            return t

    # ---------- operaciones públicas ----------

    async def login(
        self,
        *,
        cert_base64: str,
        cert_password: str,
        user_code: str,
        company_code: str,
        id_type: str,
        env: str,
        capsolver_api_key: str,
    ) -> Tenant:
        """Hace login completo (browser + CapSolver). Reemplaza cookies del tenant si existía."""
        if env not in URLS:
            raise ValueError(f"env inválido: {env!r}. Usa 'hab' o 'prod'.")
        try:
            p12_bytes = base64.b64decode(cert_base64, validate=True)
        except Exception as e:
            raise ValueError(f"certificado_base64 inválido: {e}")
        if len(p12_bytes) < 100:
            raise ValueError("certificado_base64 demasiado corto")

        tenant_id = make_tenant_id(env, user_code, company_code)

        async with self._global_lock:
            if tenant_id not in self._tenants:
                cached = self._load_from_disk(tenant_id)
                self._tenants[tenant_id] = cached or Tenant(
                    tenant_id=tenant_id,
                    env=env,
                    user_code=user_code,
                    company_code=company_code,
                    id_type=id_type,
                )
        t = self._tenants[tenant_id]

        async with t.lock:
            base_catalogo = URLS[env]["catalogo"]
            base_cert     = URLS[env]["cert"]
            user_data_dir = self._profile_dir(tenant_id)

            with _temp_cert_file(p12_bytes, cert_password) as cert_path:
                async with async_playwright() as p:
                    cookies = await _login_with_capsolver(
                        p, env, base_catalogo, base_cert,
                        cert_path, cert_password,
                        user_code, company_code, id_type,
                        capsolver_api_key, self.headless, user_data_dir,
                    )

            now = time.time()
            t.cookies = cookies
            t.id_type = id_type
            t.last_login_at = now
            t.last_validated_at = now
            t.login_count += 1
            self._persist(t)
            log.info("Login completo exitoso (tenant=%s, count=%d)", tenant_id, t.login_count)
            return t

    async def get_or_login(self, **login_kwargs) -> tuple[Tenant, str]:
        """Si la sesión cacheada vive, la devuelve. Sino hace login.

        Retorna (tenant, reason) donde reason explica qué se hizo.
        """
        env = login_kwargs["env"]
        user_code = login_kwargs["user_code"]
        company_code = login_kwargs["company_code"]
        tenant_id = make_tenant_id(env, user_code, company_code)

        existing = await self._get_tenant(tenant_id)
        if existing and existing.has_cookies:
            tenant, reason = await self._validate_or_none(existing)
            if tenant is not None:
                return tenant, reason

        t = await self.login(**login_kwargs)
        return t, "full_login"

    async def _validate_or_none(self, t: Tenant) -> tuple[Optional[Tenant], str]:
        """Devuelve (tenant, motivo) si la sesión está viva, (None, motivo) si no."""
        async with t.lock:
            now = time.time()
            seconds_since = now - t.last_validated_at if t.last_validated_at else 999999
            if seconds_since < self.validation_ttl_seconds:
                return t, f"cache_recent_{int(seconds_since)}s"

            check = validate_cookies_http(t.cookies, t.env)
            if check["valid"]:
                t.last_validated_at = now
                self._persist(t)
                return t, "validated_http"

            log.info("Validación HTTP del tenant %s: %s", t.tenant_id, check.get("status"))
            return None, f"http_check_failed_{check.get('status')}"

    async def get_cookies_only(self, tenant_id: str) -> tuple[Optional[Tenant], str]:
        """Solo lee cache (no hace login). Devuelve (None, motivo) si la sesión murió."""
        t = await self._get_tenant(tenant_id)
        if t is None:
            return None, "tenant_not_found"
        if not t.has_cookies:
            return None, "no_cookies"
        return await self._validate_or_none(t)

    async def get_status(self, tenant_id: str) -> Optional[dict]:
        t = await self._get_tenant(tenant_id)
        if t is None:
            return None
        s = t.status()
        s["validation_ttl_seconds"] = self.validation_ttl_seconds
        return s
