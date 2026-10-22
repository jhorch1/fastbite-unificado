"""Carga el manejador de su carpeta y lo ejecuta dentro de Pedidos.

No levanta una API ni un proceso adicional. Su hilo consume eventos AMQP.
"""
import importlib.util
import os
import threading
from pathlib import Path

# Carga el listener desde su carpeta sin iniciar otro servidor HTTP.
_ruta = Path(__file__).resolve().parent.parent / "notifier-service" / "main.py"
_spec = importlib.util.spec_from_file_location("fastbite_notificador", _ruta)
manejador = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(manejador)


class Consumidor:
    def __init__(self):
        # Estas señales comunican el cierre y la conexión con el hilo consumidor.
        self.detener = threading.Event()
        self.conectado = threading.Event()
        self.hilo = None

    def iniciar(self):
        # Las pruebas pueden omitir RabbitMQ; en la ejecución normal el hilo sí arranca.
        if os.getenv("NOTIFIER_DISABLE_CONSUMER") == "1":
            return
        if self.hilo and self.hilo.is_alive():
            return
        self.detener.clear()
        self.hilo = threading.Thread(
            target=manejador.consumir, args=(self.detener, self.conectado),
            name="orders-notificaciones", daemon=True,
        )
        self.hilo.start()

    def cerrar(self):
        # Solicita el cierre y permite que el listener libere su conexión.
        self.detener.set()
        if self.hilo:
            self.hilo.join(timeout=10)


consumidor = Consumidor()
