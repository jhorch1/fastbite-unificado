"""Prueba real del Compose. Crea pedidos demo y recrea SOLO Pedidos de este proyecto.

Interrumpe temporalmente su RabbitMQ para comprobar outbox y recuperacion.
No elimina volumenes. Ejecutar con el Compose encendido, desde cualquier carpeta.
"""
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parents[1]
RESULTADOS = []
CLIENTE = httpx.Client(base_url="http://127.0.0.1:8000", timeout=15, trust_env=False)


def compose(*args):
    # Usa el Compose de esta carpeta, independientemente del directorio de ejecución.
    resultado = subprocess.run(["docker", "compose", *args], cwd=ROOT,
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
    if resultado.returncode:
        raise RuntimeError(resultado.stdout + resultado.stderr)
    return resultado.stdout.strip()


def comprobar(nombre, condicion):
    if not condicion:
        raise AssertionError(nombre)
    RESULTADOS.append(nombre)
    print(f"OK: {nombre}", flush=True)


def esperar(funcion, segundos=90):
    # La entrega de avisos es asíncrona; se espera una condición en lugar de asumirla inmediata.
    limite = time.monotonic() + segundos
    while time.monotonic() < limite:
        try:
            if funcion():
                return True
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        time.sleep(1)
    raise TimeoutError("No se cumplio la condicion de recuperacion")


def avisos(pedido_id):
    return [n for n in CLIENTE.get("/api/notificaciones").json() if n["pedido_id"] == pedido_id]


def pendientes():
    # Consulta la outbox real dentro del contenedor, sin modificar sus datos.
    return int(compose("exec", "-T", "orders-service", "python", "-c",
                       "import sqlite3; c=sqlite3.connect('/data/pedidos.sqlite3'); "
                       "print(c.execute('SELECT COUNT(*) FROM pendientes').fetchone()[0]); c.close()"))


def nuevo(metodo="efectivo", cantidad=1):
    respuesta = CLIENTE.post("/api/pedidos", json={"usuario_id": 1,
                             "items": [{"producto_id": 3, "cantidad": cantidad}],
                             "metodo_pago": metodo})
    comprobar(f"crear pedido {metodo} x{cantidad}: HTTP 201", respuesta.status_code == 201)
    return respuesta.json()["pedido"]


def main():
    for puerto in (8000, 8001, 8002, 8003):
        comprobar(f"health HTTP {puerto}", httpx.get(f"http://127.0.0.1:{puerto}/health").status_code == 200)
    comprobar("catalogo usuarios", len(CLIENTE.get("/api/usuarios").json()) == 5)
    comprobar("catalogo menu", len(CLIENTE.get("/api/menu").json()) == 5)
    comprobar("compatibilidad ruta v4", CLIENTE.get("/api/usuarios/usuarios/1").json()["id"] == 1)
    comprobar("Gateway conserva 404", CLIENTE.get("/api/usuarios/99999").status_code == 404)
    comprobar("Gateway rechaza servicio desconocido", CLIENTE.get("/api/desconocido").status_code == 404)
    pedido = nuevo()
    pid = pedido["id"]
    comprobar("total calculado desde menu", pedido["total"] == 8000)
    for estado in ("enviado", "en camino", "entregado"):
        respuesta = CLIENTE.put(f"/api/pedidos/{pid}", json={"estado": estado})
        comprobar(f"transicion {estado}", respuesta.status_code == 200 and respuesta.json()["estado"] == estado)
    comprobar("bloqueo de estado final", CLIENTE.put(f"/api/pedidos/{pid}", json={"estado": "cancelado"}).status_code == 409)
    esperar(lambda: len(avisos(pid)) == 4)
    comprobar("4 avisos reales via RabbitMQ", len(avisos(pid)) == 4)
    nequi = nuevo("nequi")
    comprobar("plugin nequi", nequi["pago"]["estado"] == "aprobado")
    tarjeta = nuevo("tarjeta", 7)
    comprobar("tarjeta rechazada y pedido cancelado", tarjeta["estado"] == "cancelado" and tarjeta["pago"]["estado"] == "rechazado")
    respuesta = CLIENTE.post("/api/pedidos", json={"usuario_id": 1, "items": [], "metodo_pago": "efectivo"})
    comprobar("validacion de entrada HTTP 422", respuesta.status_code == 422)
    esperar(lambda: pendientes() == 0)
    # Interrumpe el broker para comprobar que Pedidos siga guardando eventos pendientes.
    compose("stop", "rabbitmq")
    try:
        diferido = nuevo()
        did = diferido["id"]
        comprobar("actualizacion sin broker", CLIENTE.put(f"/api/pedidos/{did}", json={"estado": "cancelado"}).status_code == 200)
        comprobar("2 eventos pendientes durante caida", pendientes() == 2)
        datos_antes = CLIENTE.get("/api/pedidos").json()
        eventos_antes = CLIENTE.get("/api/pedidos/eventos").json()
        avisos_antes = CLIENTE.get("/api/notificaciones").json()
        # Un contenedor nuevo debe recuperar la misma información desde su volumen.
        compose("up", "-d", "--no-deps", "--force-recreate", "orders-service")
        esperar(lambda: CLIENTE.get("/api/pedidos").status_code == 200)
        comprobar("pedidos sobreviven recreacion contenedor", CLIENTE.get("/api/pedidos").json() == datos_antes)
        comprobar("eventos sobreviven recreacion contenedor", CLIENTE.get("/api/pedidos/eventos").json() == eventos_antes)
        comprobar("avisos sobreviven recreacion contenedor", CLIENTE.get("/api/notificaciones").json() == avisos_antes)
        comprobar("outbox sobrevive recreacion contenedor", pendientes() == 2)
    finally:
        # Restaura el broker incluso si falla una comprobación durante la interrupción.
        compose("start", "rabbitmq")
    esperar(lambda: CLIENTE.get("/api/notificaciones/salud").json()["rabbitmq_conectado"])
    esperar(lambda: len(avisos(did)) == 2)
    esperar(lambda: pendientes() == 0)
    comprobar("reconexion automatica al broker", CLIENTE.get("/api/notificaciones/salud").json()["rabbitmq_conectado"])
    comprobar("entrega diferida sin duplicados", len(avisos(did)) == 2)
    comprobar("outbox drenada", pendientes() == 0)
    resumen = CLIENTE.get("/api/pedidos/resumen").json()
    comprobar("CQRS cancelacion e identidad coherentes", next(p for p in resumen if p["id"] == did)["estado"] == "cancelado")
    comprobar("historial inmutable con snapshots", any(e["pedido"]["id"] == pid and e["pedido"]["estado"] == "creado"
                                                     for e in CLIENTE.get("/api/pedidos/eventos").json()))
    # Dar tiempo a los healthchecks posteriores a la recreacion.
    compose("up", "-d", "--wait", "--wait-timeout", "120")
    # Deja evidencia reproducible de la validación y de los pedidos creados por ella.
    reporte = {"fecha": datetime.now().astimezone().isoformat(), "comprobaciones": len(RESULTADOS),
               "resultado": "OK", "detalles": RESULTADOS,
               "pedidos_demostracion": [pid, nequi["id"], tarjeta["id"], did],
               "compose": compose("ps", "--format", "json")}
    (ROOT / "docs" / "validacion-docker.json").write_text(json.dumps(reporte, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"TOTAL: {len(RESULTADOS)} comprobaciones correctas", flush=True)


if __name__ == "__main__":
    try:
        main()
    finally:
        CLIENTE.close()
