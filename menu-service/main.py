# API del menú; sus precios son la referencia para calcular cada pedido.
from fastapi import FastAPI, HTTPException

app = FastAPI(title="Menu Service")

# Catálogo fijo de la demostración, con precios expresados en pesos colombianos.
menu = [
    {
        "id": 1,
        "nombre": "Hamburguesa Clásica",
        "descripcion": "Carne de res, queso, lechuga, tomate y salsa de la casa.",
        "precio": 18000,
    },
    {
        "id": 2,
        "nombre": "Hamburguesa BBQ",
        "descripcion": "Carne de res, queso cheddar, tocineta y salsa BBQ.",
        "precio": 22000,
    },
    {
        "id": 3,
        "nombre": "Papas Fritas",
        "descripcion": "Papas doradas sazonadas con sal y especias.",
        "precio": 8000,
    },
    {
        "id": 4,
        "nombre": "Perro Caliente",
        "descripcion": "Salchicha, papas ripio, queso y salsas.",
        "precio": 15000,
    },
    {
        "id": 5,
        "nombre": "Gaseosa",
        "descripcion": "Bebida gaseosa fría en sabores disponibles.",
        "precio": 5000,
    },
]


@app.get("/menu/{producto_id}")
def obtener_producto(producto_id: int):
    # El 404 evita que Pedidos trate un producto inexistente como una compra válida.
    for producto in menu:
        if producto["id"] == producto_id:
            return producto
    raise HTTPException(status_code=404, detail="Producto no encontrado")


@app.get("/menu")
def listar_menu():
    return menu


@app.get("/health")
def health():
    # Docker consulta esta ruta antes de permitir que Pedidos utilice el menú.
    return {"servicio": "menu", "estado": "ok"}
