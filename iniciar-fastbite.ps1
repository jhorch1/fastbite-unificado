$ErrorActionPreference = 'Stop'
# Delega el arranque completo en el administrador del proyecto.
& (Join-Path $PSScriptRoot 'fastbite.ps1') up
Write-Host 'FastBite: http://localhost:8000 | Pedidos Swagger: http://localhost:8003/docs'
