"""Gateway de v4 completado: conserva ruta, query, cuerpo y estado HTTP."""
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

# Solo estos prefijos pueden elegir un servicio de destino.
RUTAS = {
    "usuarios": os.getenv("USERS_SERVICE_URL", "http://localhost:8001"),
    "menu": os.getenv("MENU_SERVICE_URL", "http://localhost:8002"),
    "pedidos": os.getenv("ORDERS_SERVICE_URL", "http://localhost:8003"),
    "notificaciones": os.getenv("ORDERS_SERVICE_URL", "http://localhost:8003"),
}
# Estas cabeceras describen cada conexión y no deben copiarse al siguiente salto.
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}


@asynccontextmanager
async def lifespan(app):
    # Reutiliza conexiones HTTP mientras el Gateway esté encendido.
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        app.state.client = client
        yield


app = FastAPI(title="FastBite Unificado - API Gateway", lifespan=lifespan)


@app.get("/health")
def health():
    return {"servicio": "gateway", "estado": "ok"}


@app.get("/")
def inicio():
    return {"proyecto": "FastBite Unificado", "documentacion": "/docs",
            "rutas": [f"/api/{nombre}" for nombre in RUTAS]}


async def reenviar(servicio: str, resto: str, request: Request):
    if servicio not in RUTAS:
        raise HTTPException(404, "Servicio no encontrado")
    # /api/usuarios/1 -> /usuarios/1. También acepta la forma de la plantilla
    # v4: /api/usuarios/usuarios/1 -> /usuarios/1.
    ruta = resto if resto == servicio or resto.startswith(servicio + "/") else (
        servicio + ("/" + resto if resto else "")
    )
    url = f"{RUTAS[servicio]}/{ruta}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
    try:
        # Reenvía el cuerpo y conserva parámetros repetidos, como ?id=1&id=2.
        respuesta = await request.app.state.client.request(
            request.method, url, params=request.query_params.multi_items(),
            content=await request.body(), headers=headers,
        )
    except httpx.TimeoutException:
        raise HTTPException(504, "El servicio excedio el tiempo de respuesta")
    except httpx.RequestError:
        raise HTTPException(502, "No se pudo conectar con el servicio")
    # HTTPX ya descomprimió el cuerpo; no se anuncia de nuevo como comprimido.
    excluir = HOP_HEADERS | {"content-encoding"}
    # Conserva el estado del servicio, incluidos sus errores de validación o negocio.
    return Response(content=respuesta.content, status_code=respuesta.status_code,
                    headers={k: v for k, v in respuesta.headers.items() if k.lower() not in excluir})


@app.api_route("/api/{servicio}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def gateway_raiz(servicio: str, request: Request):
    return await reenviar(servicio, "", request)


@app.api_route("/api/{servicio}/{ruta_resto:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def gateway(servicio: str, ruta_resto: str, request: Request):
    return await reenviar(servicio, ruta_resto, request)
