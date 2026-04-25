"""Servicio de autenticación con cache + validación inteligente.

Estrategia para minimizar llamadas a CapSolver:

1. Cookies en memoria (proceso) + persistidas en disco.
2. Validación HTTP barata (httpx, no Playwright) cada vez que se piden cookies,
   pero solo si pasaron > VALIDATION_TTL desde la última validación exitosa.
3. Si la validación HTTP da 'expired' → intentar validación con browser
   (a veces httpx falla por Cloudflare aunque la sesión esté viva).
4. Solo si TODO falla → login completo con CapSolver.
5. Lock asíncrono: si N peticiones llegan a la vez y la sesión expiró,
   solo UN login se ejecuta (las demás esperan ese resultado).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional

from patchright.async_api import async_playwright

from dian_login import (
    DEFAULT_COOKIES,
    DEFAULT_PROFILE,
    URLS,
    _login_with_capsolver,
    _validate_saved_cookies_browser,
    load_cookies,
    save_cookies,
    validate_cookies_http,
)

log = logging.getLogger("tokendian.auth")


class AuthService:
    """Mantiene una sesión DIAN viva con mínimas llamadas a CapSolver."""

    def __init__(
        self,
        env: str,
        cert_path: str,
        cert_pwd: str,
        user_code: str,
        comp_code: str,
        id_type: str,
        capsolver_key: str,
        headless: bool = True,
        cookies_path: Optional[Path] = None,
        user_data_dir: Optional[str] = None,
        validation_ttl_seconds: int = 300,
    ) -> None:
        if env not in URLS:
            raise ValueError(f"env inválido: {env!r}")
        self.env = env
        self.cert_path = cert_path
        self.cert_pwd = cert_pwd
        self.user_code = user_code
        self.comp_code = comp_code
        self.id_type = id_type
        self.capsolver_key = capsolver_key
        self.headless = headless
        self.cookies_path = cookies_path or Path(os.getenv("COOKIES_PATH", str(DEFAULT_COOKIES)))
        self.user_data_dir = user_data_dir or os.getenv("BROWSER_PROFILE_DIR", DEFAULT_PROFILE)
        self.validation_ttl_seconds = validation_ttl_seconds

        self._base_catalogo = URLS[env]["catalogo"]
        self._base_cert     = URLS[env]["cert"]

        self._cookies: Optional[list[dict]] = load_cookies(self.cookies_path)
        self._last_validated_at: float = 0.0
        self._last_login_at: float = 0.0
        self._login_count: int = 0
        self._lock = asyncio.Lock()

    @property
    def has_cookies(self) -> bool:
        return self._cookies is not None and len(self._cookies) > 0

    def status(self) -> dict:
        now = time.time()
        return {
            "env": self.env,
            "has_cookies": self.has_cookies,
            "cookie_count": len(self._cookies) if self._cookies else 0,
            "last_validated_at": self._last_validated_at or None,
            "last_validated_seconds_ago": int(now - self._last_validated_at) if self._last_validated_at else None,
            "last_login_at": self._last_login_at or None,
            "login_count_since_start": self._login_count,
            "validation_ttl_seconds": self.validation_ttl_seconds,
            "cookies_path": str(self.cookies_path),
        }

    async def get_cookies(self, *, validate: bool = True) -> dict:
        """Devuelve cookies vigentes. Login automático si caducaron.

        - Si las cookies se validaron hace < TTL, se asume válidas (ahorra red).
        - Si TTL expiró, valida con httpx (barato).
        - Si httpx dice 'expired', intenta browser (a veces Cloudflare miente).
        - Solo hace login si todo lo anterior falla.
        """
        async with self._lock:
            now = time.time()

            if not self.has_cookies:
                log.info("No hay cookies en cache, ejecutando login inicial")
                await self._do_full_login()
                return self._build_response(reason="initial_login")

            if not validate:
                return self._build_response(reason="cache_no_validate")

            seconds_since_validation = now - self._last_validated_at
            if seconds_since_validation < self.validation_ttl_seconds:
                return self._build_response(reason=f"cache_recent_{int(seconds_since_validation)}s")

            # TTL expirado: validar con httpx (rápido, sin browser)
            check = validate_cookies_http(self._cookies, self.env)
            if check["valid"]:
                self._last_validated_at = now
                return self._build_response(reason="validated_http")

            log.info("Validación HTTP indica %s. Probando con browser...", check.get("status"))

            # httpx falló: puede ser Cloudflare o sesión real expirada
            async with async_playwright() as p:
                refreshed = await _validate_saved_cookies_browser(
                    p, self._base_cert, self.cert_path, self.cert_pwd,
                    self._cookies, self.user_data_dir,
                )

            if refreshed:
                self._cookies = refreshed
                save_cookies(self._cookies, self.cookies_path)
                self._last_validated_at = now
                return self._build_response(reason="validated_browser")

            log.info("Sesión expirada según browser, ejecutando login completo")
            await self._do_full_login()
            return self._build_response(reason="full_login")

    async def force_refresh(self) -> dict:
        """Fuerza un login fresco (ignora cache)."""
        async with self._lock:
            log.info("force_refresh solicitado")
            await self._do_full_login()
            return self._build_response(reason="force_refresh")

    async def _do_full_login(self) -> None:
        async with async_playwright() as p:
            cookies = await _login_with_capsolver(
                p, self.env,
                self._base_catalogo, self._base_cert,
                self.cert_path, self.cert_pwd,
                self.user_code, self.comp_code, self.id_type,
                self.capsolver_key, self.headless, self.user_data_dir,
            )
        self._cookies = cookies
        save_cookies(self._cookies, self.cookies_path)
        now = time.time()
        self._last_validated_at = now
        self._last_login_at = now
        self._login_count += 1
        log.info("Login completo exitoso (count=%d)", self._login_count)

    def _build_response(self, *, reason: str) -> dict:
        return {
            "cookies": self._cookies,
            "env": self.env,
            "validated_at": self._last_validated_at,
            "reason": reason,
        }
