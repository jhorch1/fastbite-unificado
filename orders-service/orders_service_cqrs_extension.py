"""CQRS + Event Sourcing de v4 integrado con la outbox de v2.

Un evento contiene el snapshot completo del agregado. Las consultas reconstruyen
el estado con replay; evento y mensaje pendiente se confirman en una transaccion.
"""
import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

import eventos


class RepositorioPedidos:
    def __init__(self, publicador):
        self.publicador = publicador

    def _db(self):
        # Comparte la base de la outbox para poder confirmar ambas escrituras juntas.
        db = self.publicador._db()
        db.execute("""CREATE TABLE IF NOT EXISTS eventos_pedidos (
            secuencia INTEGER PRIMARY KEY AUTOINCREMENT,
            evento_id TEXT UNIQUE NOT NULL,
            pedido_id INTEGER NOT NULL,
            tipo TEXT NOT NULL,
            cuerpo TEXT NOT NULL
        )""")
        return db

    @staticmethod
    def _replay(db):
        pedidos = {}
        # Cada evento contiene una copia completa; el último de cada ID fija su estado.
        for (cuerpo,) in db.execute("SELECT cuerpo FROM eventos_pedidos ORDER BY secuencia"):
            evento = json.loads(cuerpo)
            pedidos[evento["pedido"]["id"]] = evento["pedido"]
        return pedidos

    def _registrar(self, db, tipo, pedido, **datos):
        # El UUID identifica este cambio, incluso si RabbitMQ lo entrega más de una vez.
        evento = {**datos, "tipo": tipo, "pedido": pedido,
                  "evento_id": str(uuid4()), "version": 1,
                  "ocurrido_en": datetime.now(timezone.utc).isoformat()}
        cuerpo = json.dumps(evento, ensure_ascii=False)
        db.execute("INSERT INTO eventos_pedidos (evento_id,pedido_id,tipo,cuerpo) VALUES (?,?,?,?)",
                   (evento["evento_id"], pedido["id"], tipo, cuerpo))
        # El historial permanece; el pendiente se borra al confirmar la publicación.
        db.execute("INSERT INTO pendientes (evento_id,routing_key,cuerpo) VALUES (?,?,?)",
                   (evento["evento_id"], tipo, cuerpo))

    def comando_crear_pedido(self, datos, usuario=None):
        db = self._db()
        try:
            with db:
                # Reserva la escritura antes de calcular el ID para evitar colisiones.
                db.execute("BEGIN IMMEDIATE")
                pedido = {**datos, "id": max(self._replay(db), default=0) + 1}
                self._registrar(db, "pedido.creado", pedido, usuario=usuario)
        finally:
            db.close()
        # Solo despierta al publicador cuando la transacción ya quedó confirmada.
        self.publicador.despertar.set()
        return pedido

    def comando_actualizar_pedido(self, pedido_id, transformar, cambios):
        db = self._db()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                pedido = self._replay(db).get(pedido_id)
                if pedido is None:
                    raise HTTPException(404, f"El pedido {pedido_id} no existe.")
                anterior = pedido["estado"]
                # Las reglas se ejecutan dentro de la misma transacción que guarda el evento.
                pedido = transformar(pedido)
                self._registrar(db, "pedido.actualizado", pedido,
                                estado_anterior=anterior, cambios=cambios)
        finally:
            db.close()
        self.publicador.despertar.set()
        return pedido

    def listar(self):
        db = self._db()
        try:
            return list(self._replay(db).values())
        finally:
            db.close()

    def consulta_resumen_pedidos(self):
        # La vista de lectura conserva el mismo ID y estado del pedido reconstruido.
        return [{"id": p["id"], "usuario": str(p["usuario_id"]), "items": p["items"],
                 "estado": p["estado"], "total": p["total"]} for p in self.listar()]

    def historial(self):
        # Devuelve los cambios en orden, sin reemplazarlos por el estado actual.
        db = self._db()
        try:
            return [json.loads(f[0]) for f in db.execute(
                "SELECT cuerpo FROM eventos_pedidos ORDER BY secuencia")]
        finally:
            db.close()

    def cargar_ejemplos(self, ejemplos):
        db = self._db()
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                existentes = self._replay(db)
                # La carga y esta comprobación son atómicas, también con peticiones simultáneas.
                if existentes:
                    return {"detalle": "Ya hay pedidos, no se cargaron ejemplos.", "total": len(existentes)}
                for pedido in ejemplos:
                    self._registrar(db, "pedido.creado", pedido)
        finally:
            db.close()
        self.publicador.despertar.set()
        return ejemplos


repositorio = RepositorioPedidos(eventos.publicador)
