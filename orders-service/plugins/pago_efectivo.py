# Plugin registrado en PLUGINS; comparte la misma entrada y salida con los demás.

def procesar(monto: float) -> dict:
    # Efectivo: aprueba todo monto positivo; rechaza montos invalidos.
    if monto <= 0:
        return {"metodo": "efectivo", "monto": monto, "estado": "rechazado"}
    return {"metodo": "efectivo", "monto": monto, "estado": "aprobado"}
