# Plantilla de variables de entorno para dian_login.py
# 1. Copia este archivo como set-env.ps1
# 2. Rellena los valores reales
# 3. En PowerShell: . .\set-env.ps1   (el punto y espacio al inicio "dot-source" el script)
# 4. python dian_login.py

$env:DIAN_ENV          = "hab"                              # hab | prod
$env:DIAN_CERT_PATH    = "C:\laragon\www\tokendian\secrets\cert.p12"
$env:DIAN_CERT_PASSWORD = "PON_AQUI_LA_PASSWORD_DEL_P12"
$env:DIAN_USER_CODE    = "1117488256"                       # cédula del representante legal
$env:DIAN_COMPANY_CODE = "9015591465"                       # NIT empresa, sin DV
$env:DIAN_ID_TYPE      = "10910094"                         # 10910094 = Cédula de Ciudadanía
$env:HEADLESS          = "false"                            # false en dev, true en prod
$env:CAPSOLVER_API_KEY = "CSAPI-..."                        # API key de https://dashboard.capsolver.com

Write-Host "Variables DIAN_* cargadas en la sesión actual." -ForegroundColor Green
