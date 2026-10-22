# plugins/pago_tarjeta.py
# Simula un límite para demostrar pagos aprobados y rechazados, sin cobros reales.

LIMITE_TARJETA = 50000


def procesar(monto: float) -> dict:
    if monto <= 0:
        return {"metodo": "tarjeta", "monto": monto, "estado": "rechazado"}
    if monto > LIMITE_TARJETA:
        return {"metodo": "tarjeta", "monto": monto, "estado": "rechazado"}
    return {"metodo": "tarjeta", "monto": monto, "estado": "aprobado"}
