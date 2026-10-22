# API de consulta de usuarios; Pedidos la utiliza para validar quién compra.
from fastapi import FastAPI, HTTPException

app = FastAPI(title="Users Service")

# Catálogo fijo de la demostración; no hay registro ni autenticación de usuarios.
usuarios = [
    {"id": 1, "nombre": "Juan Perez", "correo": "juan.perez@fastbite.com"},
    {"id": 2, "nombre": "Maria Lopez", "correo": "maria.lopez@fastbite.com"},
    {"id": 3, "nombre": "Carlos Garcia", "correo": "carlos.garcia@fastbite.com"},
    {"id": 4, "nombre": "Ana Rodriguez", "correo": "ana.rodriguez@fastbite.com"},
    {"id": 5, "nombre": "Luis Martinez", "correo": "luis.martinez@fastbite.com"},
]

@app.get("/usuarios/{usuario_id}")
def obtener_usuario(usuario_id: int):
    # Un ID inexistente debe devolver 404 para que Pedidos pueda rechazar la compra.
    for usuario in usuarios:
        if usuario["id"] == usuario_id:
            return usuario
    raise HTTPException(status_code=404, detail="Usuario no encontrado")

@app.get("/usuarios")
def listar_usuarios():
    return usuarios


@app.get("/health")
def health():
    # Docker consulta esta ruta antes de arrancar los servicios dependientes.
    return {"servicio": "usuarios", "estado": "ok"}
