param(
    [ValidateSet('up','down','reload','status','logs','test','setup')]
    [string]$Accion = 'up',
    [ValidateSet('all','users','menu','orders','gateway','rabbitmq','notifier')]
    [string]$Servicio = 'all'
)
$ErrorActionPreference = 'Stop'
# Los nombres cortos apuntan a servicios de Compose; el listener se administra con Pedidos.
$mapa = @{ users='users-service'; menu='menu-service'; orders='orders-service'; gateway='gateway-service'; rabbitmq='rabbitmq'; notifier='orders-service' }
# Permite ejecutar el script desde cualquier carpeta sin perder la ubicación anterior.
Push-Location -LiteralPath $PSScriptRoot
try {
    $seleccion = @()
    if ($Servicio -ne 'all') { $seleccion = @($mapa[$Servicio]) }
    switch ($Accion) {
        'setup' {
            # Este entorno local sirve para estudiar y probar; Docker instala el suyo aparte.
            if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
                python -m venv .venv
                if ($LASTEXITCODE -ne 0) { throw 'No se pudo crear el entorno Python' }
            }
            & '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
        }
        'up' { docker compose up -d --build --wait --wait-timeout 180 @seleccion }
        'down' {
            # Detiene un servicio o retira el conjunto, conservando sus volúmenes.
            if ($seleccion.Count) { docker compose stop @seleccion }
            else { docker compose down }
        }
        'reload' { docker compose restart @seleccion }
        'status' { docker compose ps @seleccion }
        'logs' { docker compose logs -f --tail 100 @seleccion }
        'test' { docker compose exec -T gateway-service python -m unittest discover -s /app/tests -v }
    }
    # Los programas externos informan errores con su código de salida.
    if ($LASTEXITCODE -ne 0) { throw "La accion '$Accion' fallo (codigo $LASTEXITCODE)" }
}
finally { Pop-Location }
