"""Pedidos de v2 con plugins, coreografia y CQRS persistente integrado de v4."""
import asyncio
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import eventos
import notificaciones
from orders_service_cqrs_extension import repositorio
from plugins import pago_efectivo, pago_nequi, pago_tarjeta

# En Docker las URLs apuntan al nombre de cada servicio; en local, a su puerto.
USERS_SERVICE_URL = os.getenv("USERS_SERVICE_URL", "http://localhost:8001")
MENU_SERVICE_URL = os.getenv("MENU_SERVICE_URL", "http://localhost:8002")
# El núcleo elige una función del registro sin conocer sus reglas de pago.
PLUGINS = {"efectivo": pago_efectivo.procesar, "tarjeta": pago_tarjeta.procesar,
           "nequi": pago_nequi.procesar}
# Cada estado indica sus siguientes pasos; una lista vacía marca un estado final.
TRANSICIONES = {"creado": ["enviado", "cancelado"], "enviado": ["en camino", "cancelado"],
               "en camino": ["entregado"], "entregado": [], "cancelado": []}


@asynccontextmanager
async def lifespan(app):
    repositorio.listar()  # Crear/verificar el almacen antes de declarar disponibilidad.
    # Estos hilos publican y consumen mensajes sin bloquear la petición HTTP.
    if os.getenv("ORDERS_DISABLE_PUBLISHER") != "1":
        eventos.publicador.iniciar()
    notificaciones.consumidor.iniciar()
    try:
        yield
    finally:
        # El cierre de los hilos espera fuera del bucle asíncrono de FastAPI.
        await asyncio.gather(asyncio.to_thread(eventos.publicador.cerrar),
                             asyncio.to_thread(notificaciones.consumidor.cerrar))


app = FastAPI(title="FastBite Unificado - Pedidos", lifespan=lifespan)


# Pydantic rechaza cantidades inválidas e IDs que no sean enteros positivos.
class ItemPedido(BaseModel):
    producto_id: int = Field(gt=0, strict=True)
    cantidad: int = Field(gt=0, strict=True)


class NuevoPedido(BaseModel):
    usuario_id: int = Field(gt=0, strict=True)
    items: list[ItemPedido] = Field(min_length=1)
    metodo_pago: str


class ActualizarPedido(BaseModel):
    # El cliente solo puede solicitar cambios de estado o de método de pago.
    model_config = ConfigDict(extra="forbid")
    estado: str | None = None
    metodo_pago: str | None = None


def _consultar(url, nombre):
    # Un fallo del catálogo debe detener la creación, no producir un total incorrecto.
    try:
        respuesta = httpx.get(url, timeout=5, trust_env=False)
    except httpx.TimeoutException:
        raise HTTPException(504, f"{nombre} excedio el tiempo de respuesta")
    except httpx.RequestError:
        raise HTTPException(503, f"No se pudo conectar con {nombre}")
    if respuesta.status_code == 404:
        raise HTTPException(404, f"{nombre}: recurso no encontrado")
    if respuesta.status_code != 200:
        raise HTTPException(502, f"Respuesta invalida de {nombre}")
    try:
        datos = respuesta.json()
    except ValueError:
        raise HTTPException(502, f"JSON invalido de {nombre}")
    if not isinstance(datos, dict) or type(datos.get("id")) is not int:
        raise HTTPException(502, f"Datos invalidos de {nombre}")
    return datos


def _normalizar_estado(estado):
    # Conserva la compatibilidad con los nombres usados en las versiones anteriores.
    estado = " ".join(estado.strip().lower().replace("_", " ").replace("-", " ").split())
    return "enviado" if estado == "aprobado" else estado


def _procesar_pago(metodo, total):
    if metodo not in PLUGINS:
        raise HTTPException(400, "Metodo de pago no soportado")
    return PLUGINS[metodo](total)


@app.get("/health")
def health():
    # Comprueba el acceso a los eventos; la conexión AMQP se consulta por separado.
    repositorio.listar()
    return {"servicio": "pedidos", "estado": "ok"}


@app.post("/pedidos", status_code=201)
def crear_pedido(pedido: NuevoPedido):
    if pedido.metodo_pago not in PLUGINS:
        raise HTTPException(400, "Metodo de pago no soportado")
    usuario = _consultar(f"{USERS_SERVICE_URL}/usuarios/{pedido.usuario_id}", "usuarios")
    # El precio lo decide Menú; nunca se toma un importe enviado por el cliente.
    total = 0
    for item in pedido.items:
        producto = _consultar(f"{MENU_SERVICE_URL}/menu/{item.producto_id}", "menu")
        precio = producto.get("precio")
        if type(precio) is not int or precio <= 0:
            raise HTTPException(502, "Precio invalido del menu")
        total += precio * item.cantidad
    pago = _procesar_pago(pedido.metodo_pago, total)
    # El repositorio guarda el evento y su mensaje pendiente en una transacción.
    nuevo = repositorio.comando_crear_pedido({
        "usuario_id": pedido.usuario_id, "items": [i.model_dump() for i in pedido.items],
        "metodo_pago": pedido.metodo_pago, "total": total, "pago": pago,
        "estado": "creado" if pago["estado"] == "aprobado" else "cancelado",
    }, usuario)
    return {"pedido": nuevo, "cqrs": {"status": "evento registrado"}}


@app.get("/pedidos")
def listar_pedidos():
    # Las consultas reconstruyen el estado desde los eventos, sin otra lista en memoria.
    return repositorio.listar()


@app.get("/pedidos/resumen")
def resumen_pedidos():
    return repositorio.consulta_resumen_pedidos()


# Las rutas fijas van antes de /pedidos/{pedido_id} para no interpretarlas como IDs.
@app.get("/pedidos/eventos")
def historial_eventos():
    return repositorio.historial()


@app.get("/pedidos/{pedido_id}")
def obtener_pedido(pedido_id: int):
    for pedido in repositorio.listar():
        if pedido["id"] == pedido_id:
            return pedido
    raise HTTPException(404, f"El pedido {pedido_id} no existe.")


@app.put("/pedidos/{pedido_id}")
def actualizar_pedido(pedido_id: int, cambios: ActualizarPedido):
    if cambios.estado is None and cambios.metodo_pago is None:
        raise HTTPException(400, "Nada que actualizar")

    def transformar(pedido):
        # Si alguna validación falla, el repositorio revierte toda la actualización.
        if cambios.metodo_pago is not None:
            if pedido["estado"] not in ("creado", "enviado"):
                raise HTTPException(409, "No se puede cambiar el pago en este estado")
            pedido["pago"] = _procesar_pago(cambios.metodo_pago, pedido["total"])
            pedido["metodo_pago"] = cambios.metodo_pago
            if pedido["pago"]["estado"] != "aprobado":
                # El pago y la logística tienen estados distintos, pero reglas comunes.
                pedido["estado"] = "cancelado"
        if cambios.estado is not None:
            nuevo = _normalizar_estado(cambios.estado)
            if nuevo not in TRANSICIONES:
                raise HTTPException(400, "Estado de pedido no valido")
            if nuevo not in TRANSICIONES.get(pedido["estado"], []):
                raise HTTPException(409, f"No se puede pasar de '{pedido['estado']}' a '{nuevo}'")
            pedido["estado"] = nuevo
        return pedido

    return repositorio.comando_actualizar_pedido(pedido_id, transformar,
                                                 cambios.model_dump(exclude_none=True))


@app.post("/pedidos/ejemplos", status_code=201)
def cargar_ejemplos():
    """Los cinco escenarios de v2 se cargan explicitamente y una sola vez."""
    escenarios = [(1, 3, 1, 8000, "efectivo", "creado"),
                  (2, 1, 2, 36000, "tarjeta", "enviado"),
                  (3, 2, 1, 32000, "efectivo", "en camino"),
                  (4, 4, 1, 15000, "tarjeta", "entregado"),
                  (5, 2, 3, 66000, "tarjeta", "cancelado")]
    ejemplos = []
    for numero, (usuario, producto, cantidad, total, metodo, estado) in enumerate(escenarios, 1):
        ejemplos.append({"id": numero, "usuario_id": usuario,
                         "items": [{"producto_id": producto, "cantidad": cantidad}],
                         "total": total, "metodo_pago": metodo, "estado": estado,
                         "pago": _procesar_pago(metodo, total)})
    # El tercer escenario incluye dos gaseosas para completar su total de 32.000.
    ejemplos[2]["items"].append({"producto_id": 5, "cantidad": 2})
    return repositorio.cargar_ejemplos(ejemplos)


# Pedidos expone los avisos guardados; el listener no tiene una API propia.
@app.get("/notificaciones/salud", tags=["Notificaciones"])
def salud_notificaciones():
    return notificaciones.manejador.salud(notificaciones.consumidor.conectado.is_set())


@app.get("/notificaciones", tags=["Notificaciones"])
def listar_notificaciones():
    return notificaciones.manejador.listar_notificaciones()


@app.get("/notificaciones/usuario/{usuario_id}", tags=["Notificaciones"])
def notificaciones_de_usuario(usuario_id: int):
    return notificaciones.manejador.notificaciones_de_usuario(usuario_id)
