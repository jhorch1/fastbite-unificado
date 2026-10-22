"""Regresiones de integracion y persistencia, aisladas de Docker y datos reales."""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orders-service"))


def cargar(nombre, ruta):
    # Los servicios tienen archivos main.py distintos; cada prueba les da un nombre propio.
    spec = importlib.util.spec_from_file_location(nombre, ROOT / ruta)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = modulo
    spec.loader.exec_module(modulo)
    return modulo


orders = cargar("orders_test", "orders-service/main.py")
gateway = cargar("gateway_test", "gateway-service/main.py")
users = cargar("users_test", "users-service/main.py")
menu = cargar("menu_test", "menu-service/main.py")
from orders_service_cqrs_extension import RepositorioPedidos


class PedidosTest(unittest.TestCase):
    def setUp(self):
        # Cada prueba usa bases temporales y respuestas HTTP simuladas, sin tocar la demo.
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pub = orders.eventos.Publicador(Path(self.tmp.name) / "orders.sqlite3")
        self.repo = RepositorioPedidos(self.pub)
        self.notifier = orders.notificaciones.manejador
        for parche in [patch.object(orders, "repositorio", self.repo),
                       patch.object(orders.eventos, "publicador", self.pub),
                       patch.object(self.notifier, "DB_PATH", Path(self.tmp.name) / "avisos.sqlite3"),
                       patch.dict(os.environ, ORDERS_DISABLE_PUBLISHER="1", NOTIFIER_DISABLE_CONSUMER="1")]:
            parche.start()
            self.addCleanup(parche.stop)
        self.client = self.enterContext(TestClient(orders.app))
        # TestClient ejecuta el arranque y cierre; el mock evita llamadas a otros servicios.
        self.mock_http = self.enterContext(patch.object(orders.httpx, "get", side_effect=self.respuesta))
        self.payload = {"usuario_id": 1, "items": [{"producto_id": 3, "cantidad": 1}],
                        "metodo_pago": "efectivo"}

    @staticmethod
    def respuesta(url, **kwargs):
        return httpx.Response(200, json={"id": 1, "nombre": "Prueba"} if "/usuarios/" in url
                              else {"id": 3, "precio": 8000})

    def crear(self, **cambios):
        return self.client.post("/pedidos", json={**self.payload, **cambios})

    def test_creacion_total_y_contrato_v2(self):
        respuesta = self.crear()
        self.assertEqual(respuesta.status_code, 201)
        self.assertEqual(respuesta.json()["pedido"]["total"], 8000)
        self.assertEqual(respuesta.json()["cqrs"], {"status": "evento registrado"})

    def test_tres_plugins(self):
        for metodo in ("efectivo", "tarjeta", "nequi"):
            self.assertEqual(self.crear(metodo_pago=metodo).json()["pedido"]["pago"]["estado"], "aprobado")

    def test_tarjeta_rechazada_cancela(self):
        pedido = self.crear(metodo_pago="tarjeta", items=[{"producto_id": 3, "cantidad": 7}]).json()["pedido"]
        self.assertEqual(pedido["estado"], "cancelado")
        self.assertEqual(self.client.put(f"/pedidos/{pedido['id']}", json={"estado": "enviado"}).status_code, 409)

    def test_validacion_items_ids_cantidades(self):
        for cambios in ({"items": []}, {"usuario_id": 0}, {"usuario_id": True},
                        {"items": [{"producto_id": 0, "cantidad": 1}]},
                        {"items": [{"producto_id": 3, "cantidad": -1}]},
                        {"items": [{"producto_id": 3, "cantidad": 1.5}]}):
            with self.subTest(cambios=cambios):
                self.assertEqual(self.crear(**cambios).status_code, 422)
        self.assertEqual(self.repo.historial(), [])

    def test_metodo_invalido_no_consulta_ni_persiste(self):
        self.assertEqual(self.crear(metodo_pago="bitcoin").status_code, 400)
        self.mock_http.assert_not_called()
        self.assertIsNone(self.pub._siguiente())

    def test_404_dependencia(self):
        self.mock_http.side_effect = None
        self.mock_http.return_value = httpx.Response(404, json={})
        self.assertEqual(self.crear().status_code, 404)
        self.assertEqual(self.repo.listar(), [])

    def test_dependencia_caida_o_timeout(self):
        for error, esperado in [(httpx.ConnectError("caido"), 503), (httpx.ReadTimeout("lento"), 504)]:
            self.mock_http.side_effect = error
            self.assertEqual(self.crear().status_code, esperado)
        self.assertIsNone(self.pub._siguiente())

    def test_respuestas_invalidas_no_generan_pedido_gratuito(self):
        for respuesta in (httpx.Response(500, json={"id": 1}), httpx.Response(200, text="html"),
                          httpx.Response(200, json=[]), httpx.Response(200, json={"id": 1}),
                          httpx.Response(200, json={"id": 1, "precio": -1})):
            self.mock_http.side_effect = [self.respuesta("/usuarios/1"), respuesta]
            self.assertEqual(self.crear().status_code, 502)
        self.assertEqual(self.repo.listar(), [])

    def test_ciclo_completo_alias_y_final(self):
        self.crear()
        for estado, esperado in [("aprobado", "enviado"), ("en_camino", "en camino"), ("entregado", "entregado")]:
            r = self.client.put("/pedidos/1", json={"estado": estado})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["estado"], esperado)
        self.assertEqual(self.client.put("/pedidos/1", json={"estado": "cancelado"}).status_code, 409)

    def test_cancelacion_reflejada_en_cqrs_mismo_id(self):
        for _ in range(3):
            self.crear()
        self.client.put("/pedidos/3", json={"estado": "cancelado"})
        resumen = self.client.get("/pedidos/resumen").json()
        self.assertEqual(resumen[2]["id"], 3)
        self.assertEqual(resumen[2]["estado"], "cancelado")
        self.assertEqual(resumen[0]["estado"], "creado")

    def test_put_rechazado_no_altera_estado_eventos_outbox(self):
        self.crear()
        antes = self.repo.historial()
        self.assertEqual(self.client.put("/pedidos/1", json={"metodo_pago": "nequi", "estado": "entregado"}).status_code, 409)
        self.assertEqual(self.repo.historial(), antes)
        self.assertEqual(self.repo.listar()[0]["metodo_pago"], "efectivo")
        with closing(sqlite3.connect(self.pub.ruta)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pendientes").fetchone()[0], 1)

    def test_actualizaciones_vacias_invalidas_y_no_existente(self):
        self.crear()
        for datos, codigo in [({}, 400), ({"estado": "desconocido"}, 400), ({"total": 1}, 422)]:
            self.assertEqual(self.client.put("/pedidos/1", json=datos).status_code, codigo)
        self.assertEqual(self.client.put("/pedidos/99", json={"estado": "cancelado"}).status_code, 404)
        self.assertEqual(self.client.get("/pedidos/99").status_code, 404)

    def test_persistencia_replay_y_ids_tras_reabrir(self):
        self.crear()
        self.client.put("/pedidos/1", json={"estado": "cancelado"})
        nuevo = RepositorioPedidos(orders.eventos.Publicador(self.pub.ruta))
        self.assertEqual(nuevo.listar(), self.repo.listar())
        self.assertEqual(nuevo.consulta_resumen_pedidos()[0]["estado"], "cancelado")
        self.assertEqual(nuevo.comando_crear_pedido(self.repo.listar()[0])["id"], 2)

    def test_evento_y_outbox_rollback_atomico(self):
        # Provoca un fallo antes del commit y comprueba que no quede ninguna escritura parcial.
        with patch.object(self.repo, "_registrar", wraps=self.repo._registrar) as guardar:
            def fallar(*args, **kwargs):
                # Insertar ambos y fallar antes del commit.
                type(self.repo)._registrar(self.repo, *args, **kwargs)
                raise OSError("disco")
            guardar.side_effect = fallar
            with self.assertRaises(OSError):
                self.crear()
        self.assertEqual(self.repo.historial(), [])
        self.assertIsNone(self.pub._siguiente())

    def test_creaciones_concurrentes_ids_unicos(self):
        # Solicitudes simultáneas al repositorio deben recibir IDs distintos.
        datos = {"usuario_id": 1, "items": [], "estado": "creado", "total": 1}
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(lambda _: self.repo.comando_crear_pedido(datos)["id"], range(20)))
        self.assertEqual(sorted(ids), list(range(1, 21)))

    def test_ejemplos_idempotentes_y_coherentes(self):
        self.assertEqual(len(self.client.post("/pedidos/ejemplos").json()), 5)
        self.client.post("/pedidos/ejemplos")
        self.assertEqual(len(self.repo.historial()), 5)
        self.assertEqual(len(self.client.get("/pedidos/resumen").json()), 5)
        self.assertEqual(self.crear().json()["pedido"]["id"], 6)

    def test_notificador_deduplica_y_ack_despues_guardado(self):
        self.crear()
        evento = json.loads(self.pub._siguiente()[2])
        canal = MagicMock()
        self.notifier.recibir_evento(canal, SimpleNamespace(delivery_tag=4), None, json.dumps(evento).encode())
        canal.basic_ack.assert_called_once_with(delivery_tag=4)
        self.notifier.procesar_evento(evento)  # Simula una reentrega del mismo mensaje.
        self.assertEqual(len(self.client.get("/notificaciones/usuario/1").json()), 1)

    def test_notificador_fallo_db_no_ack(self):
        self.crear()
        canal = MagicMock()
        with patch.object(self.notifier, "procesar_evento", side_effect=OSError("disco")):
            with self.assertRaises(OSError):
                self.notifier.recibir_evento(canal, SimpleNamespace(delivery_tag=4), None,
                                            self.pub._siguiente()[2].encode())
        canal.basic_ack.assert_not_called()
        canal.basic_nack.assert_not_called()

    def test_notificador_malformados_se_descartan(self):
        for cuerpo in (b"no json", b"[]", b'{"tipo":"otro"}'):
            canal = MagicMock()
            with self.assertLogs("notifier", level="ERROR"):
                self.notifier.recibir_evento(canal, SimpleNamespace(delivery_tag=4), None, cuerpo)
            canal.basic_nack.assert_called_once_with(delivery_tag=4, requeue=False)

    def test_publicador_conserva_snapshot_y_pendiente_al_reabrir(self):
        datos = {"pedido": {"id": 1}}
        with patch("pika.BlockingConnection") as broker:
            self.pub.encolar("pedido.creado", datos)
            broker.assert_not_called()
        datos["pedido"]["id"] = 99
        nuevo = orders.eventos.Publicador(self.pub.ruta)
        self.assertEqual(json.loads(nuevo._siguiente()[2])["pedido"]["id"], 1)
        nuevo._confirmar(nuevo._siguiente()[0])
        self.assertIsNone(nuevo._siguiente())


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.client = self.enterContext(TestClient(gateway.app))
        self.vistas = []
        def backend(request):
            self.vistas.append(request)
            return httpx.Response(409, json={"detail": "conflicto"}, headers={"x-prueba": "ok"})
        # Sustituye el destino HTTP por una respuesta controlada para probar solo el proxy.
        self.client.app.state.client._transport = httpx.MockTransport(backend)

    def test_proxy_metodo_cuerpo_query_estado_y_headers(self):
        r = self.client.put("/api/pedidos/3?x=1&x=2", json={"estado": "cancelado"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json(), {"detail": "conflicto"})
        self.assertEqual(r.headers["x-prueba"], "ok")
        solicitud = self.vistas[0]
        self.assertEqual(solicitud.url.path, "/pedidos/3")
        self.assertEqual(solicitud.url.query, b"x=1&x=2")
        self.assertEqual(json.loads(solicitud.content), {"estado": "cancelado"})

    def test_rutas_raiz_y_compatibilidad_v4(self):
        for ruta in ("/api/usuarios", "/api/usuarios/usuarios", "/api/usuarios/1", "/api/usuarios/usuarios/1",
                     "/api/notificaciones/usuario/1"):
            self.client.get(ruta)
        self.assertEqual([r.url.path for r in self.vistas],
                         ["/usuarios", "/usuarios", "/usuarios/1", "/usuarios/1", "/notificaciones/usuario/1"])

    def test_servicio_desconocido(self):
        self.assertEqual(self.client.get("/api/desconocido").status_code, 404)
        self.assertEqual(self.vistas, [])

    def test_errores_gateway(self):
        for error, codigo in [(httpx.ConnectError("caido"), 502), (httpx.ReadTimeout("lento"), 504)]:
            async def fallar(request):
                raise error
            self.client.app.state.client._transport = httpx.MockTransport(fallar)
            self.assertEqual(self.client.get("/api/pedidos").status_code, codigo)


class CatalogosTest(unittest.TestCase):
    def test_usuarios_y_menu_v2(self):
        for app, ruta in [(users.app, "usuarios"), (menu.app, "menu")]:
            with TestClient(app) as client:
                self.assertEqual(len(client.get(f"/{ruta}").json()), 5)
                self.assertEqual(client.get(f"/{ruta}/1").status_code, 200)
                self.assertEqual(client.get(f"/{ruta}/99").status_code, 404)
                self.assertEqual(client.get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
