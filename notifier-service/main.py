# Listener AMQP ejecutado en un hilo de Pedidos, sin API ni puerto propio.
# Guarda avisos en SQLite y los muestra en consola; no envía correos ni SMS.

import hashlib
import json
import logging
import os
import threading
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pika

logger = logging.getLogger("notifier")

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://fastbite:fastbite@localhost:5672/%2F")
EXCHANGE = os.getenv("RABBITMQ_EXCHANGE", "fastbite")
QUEUE = os.getenv("RABBITMQ_QUEUE", "notificador.pedidos")

DB_PATH = Path(os.getenv(
    "NOTIFIER_DB_PATH", str(Path(__file__).parent / "data" / "notificaciones.sqlite3")
))


def _db():
    # El evento_id único permite reconocer mensajes que RabbitMQ vuelva a entregar.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=5)
    db.execute("""CREATE TABLE IF NOT EXISTS notificaciones (
        secuencia INTEGER PRIMARY KEY AUTOINCREMENT,
        evento_id TEXT UNIQUE NOT NULL, usuario_id INTEGER NOT NULL, cuerpo TEXT NOT NULL
    )""")
    return db


def validar_evento(evento):
    # Valida los datos mínimos antes de generar o guardar un aviso.
    if not isinstance(evento, dict) or evento.get("tipo") not in (
        "pedido.creado", "pedido.actualizado"
    ):
        raise ValueError("Tipo de evento no soportado")
    pedido = evento.get("pedido")
    if not isinstance(pedido, dict):
        raise ValueError("Falta el pedido")
    for campo in ("id", "usuario_id"):
        if type(pedido.get(campo)) is not int or pedido[campo] <= 0:
            raise ValueError(f"pedido.{campo} debe ser un entero positivo")
    if not isinstance(pedido.get("estado"), str) or "total" not in pedido:
        raise ValueError("Faltan estado/total del pedido")
    if evento.get("evento_id") is not None and (
        not isinstance(evento["evento_id"], str) or not evento["evento_id"]
    ):
        raise ValueError("evento_id invalido")


def construir_mensaje(evento: dict) -> tuple[int | None, str]:
    """Arma el texto con el que se 'informa al usuario'. Solo usa el evento."""
    pedido = evento.get("pedido", {}) if isinstance(evento, dict) else {}
    usuario = evento.get("usuario") if isinstance(evento, dict) else None
    pedido_id = pedido.get("id")
    usuario_id = pedido.get("usuario_id")
    estado = pedido.get("estado")
    total = pedido.get("total")
    pago = pedido.get("pago", {}) if isinstance(pedido.get("pago"), dict) else {}
    tipo = evento.get("tipo", "?") if isinstance(evento, dict) else "?"

    if isinstance(usuario, dict):
        nombre = usuario.get("nombre") or f"usuario {usuario_id}"
    else:
        nombre = f"usuario {usuario_id}"

    if tipo == "pedido.creado":
        texto = (
            f"Hola {nombre}: tu pedido #{pedido_id} por ${total} fue creado. "
            f"Estado del pedido: {estado}. "
            f"Pago con {pago.get('metodo')}: {pago.get('estado')}."
        )
    elif tipo == "pedido.actualizado":
        anterior = evento.get("estado_anterior")
        texto = (
            f"Hola {nombre}: tu pedido #{pedido_id} paso de '{anterior}' "
            f"a '{estado}'. Total ${total}."
        )
    else:  # evento desconocido: igual se deja registro
        texto = f"Hola {nombre}: evento '{tipo}' del pedido #{pedido_id} (estado: {estado})."
    return usuario_id, texto


def procesar_evento(evento: dict) -> dict:
    """Bandeja de avisos persistente; reentregas no crean duplicados."""
    validar_evento(evento)
    usuario_id, texto = construir_mensaje(evento)
    # Compatibilidad con los eventos antiguos que no tenian UUID.
    evento_id = evento.get("evento_id") or hashlib.sha256(
        json.dumps(evento, sort_keys=True).encode("utf-8")
    ).hexdigest()
    notificacion = {
        "evento_id": evento_id,
        "recibido_en": datetime.now(timezone.utc).isoformat(),
        "tipo": evento.get("tipo"),
        "pedido_id": (evento.get("pedido") or {}).get("id"),
        "usuario_id": usuario_id,
        "mensaje": texto,
        "evento": evento,
    }
    db = _db()
    try:
        with db:
            resultado = db.execute(
                # Una reentrega conserva el primer aviso en lugar de insertar otro.
                "INSERT OR IGNORE INTO notificaciones (evento_id, usuario_id, cuerpo) VALUES (?, ?, ?)",
                (evento_id, usuario_id, json.dumps(notificacion, ensure_ascii=False)),
            )
            if resultado.rowcount == 0:
                # Devuelve el aviso existente para que el consumidor pueda confirmar igual.
                return json.loads(db.execute(
                    "SELECT cuerpo FROM notificaciones WHERE evento_id = ?", (evento_id,)
                ).fetchone()[0])
    finally:
        db.close()
    logger.info("Aviso a usuario %s: %s", usuario_id, texto)
    print(f"[notificador] usuario {usuario_id}: {texto}", flush=True)
    return notificacion


def recibir_evento(canal, metodo, propiedades, cuerpo):
    try:
        evento = json.loads(cuerpo.decode("utf-8"))
        validar_evento(evento)
    except (UnicodeDecodeError, ValueError):
        logger.exception("Evento malformado; se descarta sin reencolar")
        # Reencolar un mensaje inválido repetiría el mismo error indefinidamente.
        canal.basic_nack(delivery_tag=metodo.delivery_tag, requeue=False)
        return
    # Un fallo de almacenamiento sale del callback: al cerrar la conexion
    # RabbitMQ reencola el mensaje sin ACK para reintentarlo mas tarde.
    procesar_evento(evento)
    canal.basic_ack(delivery_tag=metodo.delivery_tag)  # Se confirma después de guardar el aviso.


def consumir(detener: threading.Event, conectado: threading.Event | None = None):
    """Consumidor AMQP independiente con reconexion y cierre controlado."""
    conectado = conectado if conectado is not None else threading.Event()
    while not detener.is_set():
        conexion = None
        try:
            params = pika.URLParameters(RABBITMQ_URL)
            params.socket_timeout = 2
            params.stack_timeout = 5
            params.blocked_connection_timeout = 3
            params.connection_attempts = 1
            params.heartbeat = 30
            conexion = pika.BlockingConnection(params)
            canal = conexion.channel()
            canal.exchange_declare(exchange=EXCHANGE, exchange_type="topic", durable=True)
            canal.queue_declare(queue=QUEUE, durable=True)
            canal.queue_bind(queue=QUEUE, exchange=EXCHANGE, routing_key="pedido.*")
            # Procesa un mensaje sin confirmar a la vez y controla manualmente su ACK.
            canal.basic_qos(prefetch_count=1)
            canal.basic_consume(queue=QUEUE, on_message_callback=recibir_evento, auto_ack=False)
            conectado.set()
            logger.info("Notificador suscrito a '%s' (cola %s)", EXCHANGE, QUEUE)
            while not detener.is_set():
                # El límite permite revisar periódicamente si se solicitó detener el hilo.
                conexion.process_data_events(time_limit=1)
        except Exception as exc:
            logger.warning("RabbitMQ no disponible (%s). Reintento en 5 s...", exc)
        finally:
            conectado.clear()
            if conexion is not None and conexion.is_open:
                try:
                    conexion.close()
                except Exception:
                    logger.debug("Conexion ya cerrada", exc_info=True)
        detener.wait(5)  # Pausa antes de reconectar; el cierre puede interrumpirla.


def salud(conectado=False):
    # La conexión al broker y el número de avisos son indicadores distintos.
    db = _db()
    try:
        cantidad = db.execute("SELECT COUNT(*) FROM notificaciones").fetchone()[0]
    finally:
        db.close()
    return {"componente": "notificador", "rabbitmq_conectado": conectado, "notificaciones": cantidad}


def listar_notificaciones():
    """Historial de avisos enviados a usuarios (lo genera el consumidor RabbitMQ)."""
    db = _db()
    try:
        return [json.loads(fila[0]) for fila in db.execute(
            "SELECT cuerpo FROM notificaciones ORDER BY secuencia"
        )]
    finally:
        db.close()


def notificaciones_de_usuario(usuario_id: int):
    db = _db()
    try:
        return [json.loads(fila[0]) for fila in db.execute(
            "SELECT cuerpo FROM notificaciones WHERE usuario_id = ? ORDER BY secuencia", (usuario_id,)
        )]
    finally:
        db.close()

