# plugins/pago_nequi.py
# Simula el resultado de Nequi; no se conecta a la plataforma ni mueve dinero.


def procesar(monto: float) -> dict:
    if monto <= 0:
        return {"metodo": "nequi", "monto": monto, "estado": "rechazado"}
    return {"metodo": "nequi", "monto": monto, "estado": "aprobado"}
