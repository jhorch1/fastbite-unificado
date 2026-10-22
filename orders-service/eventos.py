"""Eventos RabbitMQ/Pika con una bandeja local y publicacion en segundo plano.

Un unico hilo es dueno de la conexion AMQP. Solo elimina eventos pendientes
cuando RabbitMQ confirma su publicacion; los demas sobreviven reinicios.
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pika

logger = logging.getLogger("orders.eventos")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://fastbite:fastbite@localhost:5672/%2F")
EXCHANGE = os.getenv("RABBITMQ_EXCHANGE", "fastbite")
QUEUE = os.getenv("RABBITMQ_QUEUE", "notificador.pedidos")
OUTBOX_PATH = Path(os.getenv(
    "ORDERS_OUTBOX_PATH", str(Path(__file__).parent / "data" / "eventos.sqlite3")
))


class Publicador:
    def __init__(self, ruta=OUTBOX_PATH, url=RABBITMQ_URL, reintento=5):
        self.ruta = Path(ruta)
        self.url = url
        self.reintento = reintento
        # Una señal solicita el cierre; la otra despierta al hilo cuando hay trabajo.
        self.detener = threading.Event()
        self.despertar = threading.Event()
        self.hilo = None
        self._lock = threading.Lock()

    def _db(self):
        # La outbox conserva mensajes pendientes aunque el proceso o el broker se reinicien.
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.ruta, timeout=5)
        db.execute("""CREATE TABLE IF NOT EXISTS pendientes (
            secuencia INTEGER PRIMARY KEY AUTOINCREMENT,
            evento_id TEXT UNIQUE NOT NULL,
            routing_key TEXT NOT NULL,
            cuerpo TEXT NOT NULL
        )""")
        return db

    def encolar(self, tipo, datos):
        # Utilidad heredada para encolar solo mensajes; Pedidos usa su transacción del repositorio.
        evento_id = str(uuid4())
        evento = {
            **datos, "tipo": tipo, "evento_id": evento_id,
            "ocurrido_en": datetime.now(timezone.utc).isoformat(), "version": 1,
        }
        # Serializar aqui conserva una copia anterior a futuros PUT.
        cuerpo = json.dumps(evento, ensure_ascii=False)
        db = self._db()
        try:
            with db:
                db.execute(
                    "INSERT INTO pendientes (evento_id, routing_key, cuerpo) VALUES (?, ?, ?)",
                    (evento_id, tipo, cuerpo),
                )
        finally:
            db.close()
        self.despertar.set()
        return True  # Guardado para publicar, aun no entregado al usuario.

    def _siguiente(self):
        # Publica primero el pendiente más antiguo para conservar el orden de los cambios.
        db = self._db()
        try:
            return db.execute(
                "SELECT evento_id, routing_key, cuerpo FROM pendientes ORDER BY secuencia LIMIT 1"
            ).fetchone()
        finally:
            db.close()

    def _confirmar(self, evento_id):
        # Quita el pendiente, no el evento del historial de negocio.
        db = self._db()
        try:
            with db:
                db.execute("DELETE FROM pendientes WHERE evento_id = ?", (evento_id,))
        finally:
            db.close()

    def iniciar(self):
        with self._lock:
            # Evita dos publicadores compitiendo por los mismos pendientes en este proceso.
            if self.hilo and self.hilo.is_alive():
                return
            self.detener.clear()
            self.hilo = threading.Thread(target=self._ejecutar, name="orders-rabbitmq", daemon=True)
            self.hilo.start()

    def cerrar(self):
        self.detener.set()
        self.despertar.set()  # Interrumpe la espera para que el hilo pueda terminar.
        if self.hilo:
            self.hilo.join(timeout=10)

    def _conectar(self):
        params = pika.URLParameters(self.url)
        params.socket_timeout = 2
        params.stack_timeout = 5
        params.blocked_connection_timeout = 3
        params.connection_attempts = 1
        params.heartbeat = 30
        conexion = pika.BlockingConnection(params)
        try:
            canal = conexion.channel()
            canal.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)
            # Crea la cola antes de publicar, aunque el listener todavía no haya arrancado.
            canal.queue_declare(queue=QUEUE, durable=True)
            canal.queue_bind(queue=QUEUE, exchange=EXCHANGE, routing_key="pedido.*")
            canal.confirm_delivery()  # El broker debe confirmar que recibió el mensaje.
            return conexion, canal
        except Exception:
            conexion.close()
            raise

    def _ejecutar(self):
        conexion = None
        try:
            while not self.detener.is_set():
                try:
                    self.despertar.clear()
                    pendiente = self._siguiente()
                    if pendiente is None:
                        # Atiende la conexión y espera sin consultar la base continuamente.
                        if conexion is not None:
                            conexion.process_data_events(time_limit=0.2)
                        self.despertar.wait(timeout=0.5)
                        continue
                    if conexion is None or conexion.is_closed:
                        conexion, canal = self._conectar()
                    evento_id, tipo, cuerpo = pendiente
                    canal.basic_publish(
                        exchange=EXCHANGE, routing_key=tipo,
                        body=cuerpo.encode("utf-8"), mandatory=True,  # Falla si no hay cola de destino.
                        properties=pika.BasicProperties(
                            content_type="application/json", delivery_mode=2,  # Mensaje persistente.
                            message_id=evento_id, type=tipo,
                        ),
                    )
                    # Un fallo antes de este punto deja el mensaje listo para reintentar.
                    self._confirmar(evento_id)
                except Exception as exc:
                    logger.warning("Evento pendiente; reintento RabbitMQ en %s s: %s", self.reintento, exc)
                    if conexion is not None and conexion.is_open:
                        try:
                            conexion.close()
                        except Exception:
                            logger.debug("Conexion ya cerrada", exc_info=True)
                    conexion = None
                    self.detener.wait(self.reintento)
        finally:
            if conexion is not None and conexion.is_open:
                conexion.close()


publicador = Publicador()


# Envoltorios heredados; el flujo actual de Pedidos registra su outbox desde CQRS.
def publicar_pedido_creado(pedido: dict, usuario: dict | None = None) -> bool:
    return publicador.encolar("pedido.creado", {"pedido": pedido, "usuario": usuario})


def publicar_pedido_actualizado(
    pedido: dict, estado_anterior: str | None = None, cambios: dict | None = None
) -> bool:
    return publicador.encolar("pedido.actualizado", {
        "pedido": pedido, "estado_anterior": estado_anterior, "cambios": cambios or {},
    })
