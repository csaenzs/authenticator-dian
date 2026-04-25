# Convierte el .p12 legacy de la DIAN (RC2/3DES+SHA1) a un .p12 moderno (AES-256)
# que OpenSSL 3 / Playwright pueden cargar sin problemas.
#
# Uso:
#   . .\set-env.ps1                                                 # carga DIAN_CERT_PASSWORD
#   .\convert-cert.ps1 "C:\ruta\al\cert-original.p12"               # convierte
#
# Resultado: genera secrets\cert-modern.p12 con la MISMA contraseña original.

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Source
)

$ErrorActionPreference = "Stop"

$openssl = "C:\Program Files\Git\mingw64\bin\openssl.exe"
$src     = $Source
$pwd     = $env:DIAN_CERT_PASSWORD

if (-not (Test-Path $src)) {
    Write-Error "El .p12 origen no existe: $src"
    exit 1
}
if (-not $pwd) {
    Write-Error "DIAN_CERT_PASSWORD no está definida. Ejecuta primero: . .\set-env.ps1"
    exit 1
}

$dst = "C:\laragon\www\tokendian\secrets\cert-modern.p12"
$tmp = "C:\laragon\www\tokendian\secrets\_tmp_extract.pem"

Write-Host "Extrayendo PEM (modo legacy)..." -ForegroundColor Cyan
& $openssl pkcs12 -in $src -nodes -legacy -passin "pass:$pwd" -out $tmp
if ($LASTEXITCODE -ne 0) {
    if (Test-Path $tmp) { Remove-Item $tmp -Force }
    Write-Error "Falló la extracción del .p12 original."
    exit 1
}

Write-Host "Re-empaquetando como .p12 moderno (AES-256)..." -ForegroundColor Cyan
& $openssl pkcs12 -export -in $tmp -out $dst -passin "pass:$pwd" -passout "pass:$pwd"
$ok = $LASTEXITCODE
Remove-Item $tmp -Force   # Borra siempre el PEM intermedio (contiene la private key sin cifrar)

if ($ok -ne 0) {
    Write-Error "Falló el re-empaquetado del .p12."
    exit 1
}

Write-Host "OK -> $dst" -ForegroundColor Green
Write-Host "Ahora actualiza DIAN_CERT_PATH para apuntar a este archivo, o usa el set-env.ps1 ya actualizado." -ForegroundColor Yellow
