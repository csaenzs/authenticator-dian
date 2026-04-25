"""Servicio HTTP que mantiene una sesión DIAN viva.

Endpoints:
  GET  /health              - ping
  GET  /auth/status         - estado de la sesión sin tocar DIAN
  GET  /auth/cookies        - devuelve cookies vigentes (login automático si caducó)
  POST /auth/refresh        - fuerza login fresco
  GET  /auth/cookies/netscape - cookies en formato Netscape (listas para cURL)

Autenticación: header X-API-Key con valor SERVICE_API_KEY.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

from auth_service import AuthService
from dian_login import CapSolverError, DianLoginRejected, TurnstileChallengeError

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("tokendian.server")


# ---------- bootstrap del servicio ----------

def _build_service() -> AuthService:
    required = ["DIAN_CERT_PATH", "DIAN_CERT_PASSWORD", "DIAN_USER_CODE",
                "DIAN_COMPANY_CODE", "CAPSOLVER_API_KEY"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"Faltan variables de entorno: {', '.join(missing)}")

    return AuthService(
        env=os.getenv("DIAN_ENV", "hab"),
        cert_path=os.environ["DIAN_CERT_PATH"],
        cert_pwd=os.environ["DIAN_CERT_PASSWORD"],
        user_code=os.environ["DIAN_USER_CODE"],
        comp_code=os.environ["DIAN_COMPANY_CODE"],
        id_type=os.getenv("DIAN_ID_TYPE", "10910094"),
        capsolver_key=os.environ["CAPSOLVER_API_KEY"],
        headless=os.getenv("HEADLESS", "true").lower() == "true",
        validation_ttl_seconds=int(os.getenv("VALIDATION_TTL_SECONDS", "300")),
    )


_service: Optional[AuthService] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _service
    _service = _build_service()
    log.info("AuthService iniciado (env=%s, ttl=%ds)", _service.env, _service.validation_ttl_seconds)
    yield
    log.info("Apagando servicio")


app = FastAPI(
    title="DIAN Auth Service",
    description="Mantiene una sesión DIAN viva minimizando llamadas a CapSolver",
    version="1.0.0",
    lifespan=lifespan,
)


def get_service() -> AuthService:
    if _service is None:
        raise HTTPException(503, "Servicio aún no inicializado")
    return _service


# ---------- auth ----------

API_KEY_HEADER = "X-API-Key"


def require_api_key(x_api_key: Optional[str] = Header(None, alias=API_KEY_HEADER)) -> None:
    expected = os.environ.get("SERVICE_API_KEY")
    if not expected:
        raise HTTPException(500, "SERVICE_API_KEY no configurada en el servidor")
    if x_api_key != expected:
        raise HTTPException(401, "API key inválida o faltante")


# ---------- response models ----------

class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "tokendian"


class StatusResponse(BaseModel):
    env: str
    has_cookies: bool
    cookie_count: int
    last_validated_at: Optional[float]
    last_validated_seconds_ago: Optional[int]
    last_login_at: Optional[float]
    login_count_since_start: int
    validation_ttl_seconds: int
    cookies_path: str


class CookiesResponse(BaseModel):
    env: str
    reason: str
    validated_at: Optional[float]
    cookies: list[dict]


# ---------- endpoints ----------

@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse()


@app.get("/auth/status", response_model=StatusResponse, dependencies=[Depends(require_api_key)])
def auth_status(svc: AuthService = Depends(get_service)):
    return svc.status()


@app.get("/auth/cookies", response_model=CookiesResponse, dependencies=[Depends(require_api_key)])
async def auth_cookies(
    validate: bool = True,
    svc: AuthService = Depends(get_service),
):
    """Devuelve cookies vigentes (formato Playwright JSON).

    - validate=true (default): valida sesión si pasó el TTL
    - validate=false: devuelve cache sin tocar DIAN (úsalo en peticiones rápidas
      cuando ya sabes que validaste hace poco)
    """
    try:
        return await svc.get_cookies(validate=validate)
    except (DianLoginRejected, CapSolverError, TurnstileChallengeError) as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.post("/auth/refresh", response_model=CookiesResponse, dependencies=[Depends(require_api_key)])
async def auth_refresh(svc: AuthService = Depends(get_service)):
    try:
        return await svc.force_refresh()
    except (DianLoginRejected, CapSolverError, TurnstileChallengeError) as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.get("/auth/cookies/netscape", dependencies=[Depends(require_api_key)])
async def auth_cookies_netscape(
    validate: bool = True,
    svc: AuthService = Depends(get_service),
):
    """Cookies en formato Netscape (listo para CURLOPT_COOKIEFILE de cURL/PHP)."""
    data = await svc.get_cookies(validate=validate)
    netscape = _to_netscape(data["cookies"])
    return Response(content=netscape, media_type="text/plain")


# ---------- conversor a formato Netscape ----------

def _to_netscape(cookies: list[dict]) -> str:
    lines = [
        "# Netscape HTTP Cookie File",
        "# Generated by tokendian service",
        "",
    ]
    for c in cookies:
        domain = c.get("domain") or ""
        domain_specified = "TRUE" if domain.startswith(".") else "FALSE"
        if not domain.startswith("."):
            domain = "." + domain.lstrip(".") if domain else ""
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = int(c.get("expires") or 0)
        if expires < 0:
            expires = 0
        name = c.get("name") or ""
        value = c.get("value") or ""
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        lines.append(f"{prefix}{domain}\t{domain_specified}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
    return "\n".join(lines) + "\n"
