# Diagnóstico y reporte de unificación de FastBite

Fecha: 22 de octubre de 2026, America/Bogota.

## Resultado

Se entrega `Unificar Fastbite/Fastbite Unificado` como proyecto independiente,
con entorno virtual nuevo, dependencias instaladas y fijadas, y cinco contenedores
Docker en estado `healthy`: Gateway, Usuarios, Menú, Pedidos y RabbitMQ.

Las 25 pruebas automatizadas pasan tanto en Windows/Python 3.12 como en el
contenedor Linux. La prueba real del sistema completa 33 comprobaciones,
incluyendo entrega por AMQP, caída del broker y recreación del contenedor de Pedidos.
Esto valida la integración académica y sus escenarios comprobados; no certifica
preparación para producción.

## Qué había realmente en las dos versiones

La v2 contiene la aplicación base: tres APIs FastAPI, plugins de pago y un
notificador dentro del proceso de Pedidos. Su Compose solo levanta RabbitMQ;
los scripts originales ejecutan las APIs en Windows. Usuarios y productos son
catálogos estáticos. Los pedidos se guardan en memoria, mientras avisos y mensajes
pendientes ya tienen almacenamiento SQLite.

La v4 es una extensión parcial del proyecto anterior, no una segunda aplicación
completa y autónoma. Incluye una plantilla de Gateway, una propuesta de Compose,
Dockerfiles y una extensión CQRS. El módulo CQRS importable de v4, su copia con
guiones y el módulo de v2 tienen el mismo SHA-256:
`880f32d7765e87fedb59b6c2bfb6eaacc58933a97e052e2309427ab31f89bc95`.
Por tanto, no había tres implementaciones distintas que conservar de ese módulo.

Se inspeccionaron las carpetas extraídas actuales y el inventario de Python de
los ZIP. Se registraron hashes de 24 archivos fuente/configuración de v2 y 9 de v4
en `origenes.json`. Las carpetas y ZIP originales se conservaron; el nuevo proyecto
no necesita sus rutas para construir ni para ejecutarse.

## Hallazgos y correcciones

| Hallazgo original | Consecuencia | Solución integrada |
| --- | --- | --- |
| Gateway de v4 termina en `pass` | No reenvía solicitudes | Proxy implementado con método, cuerpo, query, headers y estado HTTP |
| Sin Dockerfile de Gateway y con rutas de COPY/contexto incompatibles con las carpetas extraídas | El Compose original no construye el sistema entregado | Dockerfile compartido y Compose autocontenido en la raíz unificada |
| `localhost` fijo dentro de Pedidos y Gateway | Los servicios no se encuentran dentro de contenedores separados | Variables de entorno y DNS de servicios Docker |
| Dos listas independientes: pedidos y eventos CQRS | El resumen puede asignar otro ID y omitir estados reales | Un almacén persistente de eventos y consultas por replay |
| Cinco pedidos precargados sin sus eventos CQRS | El resumen inicialmente puede estar vacío y el siguiente pedido divergir en ID | Carga explícita de ejemplos, con eventos y IDs reales |
| Cancelaciones por PUT sin integrar el comando CQRS de cancelación | Pedido cancelado y resumen activo | Toda actualización válida registra su snapshot, incluida cancelación |
| Pedidos y eventos CQRS en memoria | Se pierden al reiniciar | SQLite en volumen `fastbite-unificado_orders-data` |
| Escritura del pedido y mensaje sin transacción común | Ventana de inconsistencia ante un fallo entre operaciones | Evento y outbox se insertan en una misma transacción |
| Validación incompleta de respuestas HTTP de catálogos | Un fallo remoto podía acabar en precio cero o respuesta inesperada | Validación de estado HTTP, JSON y precio positivo; errores 502/503/504 |
| Dependencias sin versiones y entorno virtual antiguo incorporado en v2 | Entorno dependiente de otra ubicación/instalación | `.venv` nuevo y `requirements.txt` con versiones comprobadas |
| Arranque sin comprobar disponibilidad de APIs | Pedidos/Gateway podían arrancar antes de sus dependencias | Healthchecks y dependencias `service_healthy` |

El Compose usa un volumen RabbitMQ nuevo, separado del original. Se instala
`rabbitmq:4.1-management`; no se intentó actualizar ni reutilizar datos antiguos de RabbitMQ 3.

## Funciones conservadas y procedencia

| Función | Procedencia | Estado unificado |
| --- | --- | --- |
| Listar y consultar cinco usuarios | v2 | Conservada; error 404 más descriptivo y healthcheck |
| Listar y consultar cinco productos/precios | v2 | Conservada, con healthcheck |
| Crear, listar, consultar y actualizar pedidos | v2 | Conservada, con persistencia y validaciones |
| Efectivo, tarjeta y Nequi | v2 | Se conservan los tres plugins; tarjeta rechaza montos superiores a 50.000 |
| Ciclo creado → enviado → en camino → entregado | v2 | Conservado; cancelación solo desde creado/enviado y estados finales protegidos |
| Eventos, outbox, confirmación de publicación | v2 | Reutilizados; outbox integrada con la transacción de eventos del pedido |
| Notificador y deduplicación SQLite | v2 | Conservados dentro de Pedidos; ACK tras guardar |
| API Gateway | Plantilla v4 | Completada; incluye rutas de notificaciones y compatibilidad con rutas originales de v4 |
| CQRS/Event Sourcing | Extensión compartida por v2/v4 | Integrada con snapshots persistentes e IDs comunes; el resumen muestra estados logísticos reales |
| Despliegue Docker completo | Propuesta v4 | Reparado y comprobado con construcción y arranque reales |

La respuesta de creación conserva `{"pedido": ..., "cqrs": {"status": "evento registrado"}}`.
Se mantienen los endpoints directos y se agregan `/health` y `/pedidos/eventos`.
La entrada inválida que incumple los modelos Pydantic responde 422; las reglas
de negocio responden 400 o 409. Esto reemplaza algunos 400 manuales de v2.
Los ejemplos ya no se recargan automáticamente en cada reinicio.

CQRS separa comandos y consultas a nivel de código. No se utilizan bases separadas
ni una proyección de lectura asíncrona. Las consultas hacen replay del historial
de snapshots. Event Sourcing no se limita a una segunda lista decorativa en memoria.

## Entorno instalado

| Elemento | Versión/estado comprobado |
| --- | --- |
| Python local | 3.12.10; `.venv` creado dentro de esta carpeta |
| Imagen de aplicación | `python:3.12-slim`, usuario UID 10001 |
| FastAPI | 0.160.0 |
| Uvicorn | 0.54.0 |
| HTTPX | 0.28.1 |
| Pika | 1.4.4 |
| Pydantic | 2.14.1 |
| Starlette | 1.9.0 |
| Docker Engine | 29.8.2, contexto `desktop-linux` |
| Dependencias | `pip check`: sin incompatibilidades detectadas |

Las dependencias transitivas también están fijadas en `requirements.txt`.
Las imágenes base usan etiquetas de versión; no se ha fijado su digest en el Compose/Dockerfile.
Node.js no es necesario para este proyecto Python.

## Validación realizada

1. `docker compose config --quiet`: configuración aceptada.
2. Construcción real de la imagen y arranque con `docker compose up -d --build --wait`.
3. Suite local: **25/25 pruebas**, código de salida 0.
4. Misma suite en Linux/Docker: **25/25 pruebas**, código de salida 0.
5. Prueba real `tests/validar_docker.py`: **33 comprobaciones**, resultado OK.
6. Swagger de los cuatro servicios: HTTP 200.
7. Revisión final: los cinco contenedores saludables y notificador conectado.

La suite cubre catálogos, rutas de Gateway, propagación de conflictos y errores,
los tres pagos, rechazo de tarjeta, validación de entrada, estados finales,
cancelaciones en CQRS, rollback ante fallo, IDs únicos con 20 creaciones concurrentes,
reapertura del almacén, carga idempotente de ejemplos y deduplicación de avisos.

En la prueba real se creó el pedido #1 y se recorrieron sus estados hasta entregado;
RabbitMQ generó sus cuatro avisos. Los pedidos #2 y #3 verificaron Nequi y tarjeta
rechazada. Con el broker detenido se creó y canceló el pedido #4: quedaron dos
mensajes pendientes. Se recreó el contenedor de Pedidos y se verificó que pedidos,
eventos, avisos y pendientes permanecían. Al arrancar RabbitMQ se entregaron
los dos avisos diferidos, sin duplicados, y la outbox quedó vacía.

Los cuatro pedidos de validación se dejan como datos de demostración. No se
importaron historiales operativos, bases SQLite ni colas antiguas de las versiones
originales. Sus archivos se conservan en las carpetas de origen.

En los logs aparecen desconexiones AMQP y un 502 del Gateway durante la interrupción
intencional de Pedidos/RabbitMQ. Corresponden a la prueba de recuperación; no indican
que el estado final siga caído.

## Evidencias entregadas

| Archivo | Contenido |
| --- | --- |
| `pruebas-locales.txt` | Salida completa de las 25 pruebas en Windows |
| `pruebas-docker.txt` | Salida completa de las 25 pruebas en Linux |
| `prueba-sistema.txt` | Las 33 comprobaciones sobre los servicios reales |
| `validacion-docker.json` | Resultado estructurado, fecha, pedidos usados y estado Compose |
| `estado-contenedores.json` | Captura de estado de los cinco contenedores |
| `logs-integracion.txt` | Logs de Gateway/Pedidos durante el ejercicio |
| `origenes.json` | Rutas y hashes de los archivos fuente originales inspeccionados |

## Límites y trabajo que no forma parte de esta unificación

- **Sin autenticación ni autorización:** cualquier cliente con acceso local puede
  consultar y cambiar pedidos o leer avisos de otros usuarios. Los puertos solo
  están publicados en loopback. No se debe publicar directamente este Compose en Internet.
- **Pagos simulados:** no hay proveedor de pagos, conciliación, reembolso ni cobro real.
- **Notificaciones locales:** se guardan y consultan avisos; no hay correo, SMS o push externos.
- **Catálogos estáticos:** no hay CRUD de usuarios/productos, inventario ni reservas de stock.
- **Sin frontend:** Swagger permite explorar la API, pero no sustituye una interfaz para clientes.
- **Escala de demo:** replay completo en lecturas, SQLite y publicador pensado para una sola
  réplica de Pedidos. No se validó escalado horizontal, carga sostenida ni almacenamiento distribuido.
- **Semántica de reintento:** la deduplicación aplica a eventos recibidos. Dos POST idénticos
  del cliente pueden crear dos pedidos; no hay clave de idempotencia de solicitudes.
- **Operación:** no se implementaron TLS, backups automáticos, recuperación ante pérdida
  de disco, migraciones versionadas del esquema ni monitoreo centralizado.
- **Cola de errores:** mensajes malformados se descartan con NACK sin reencolar; no se
  agregó una dead-letter queue para inspección posterior.
- **Aviso del entorno de pruebas:** Starlette 1.9.0 informa que el uso de HTTPX en
  TestClient está deprecado y sugiere HTTPX2. Las suites pasan; se conserva como
  pendiente de actualización del tooling, sin ocultar la advertencia.

## Uso inmediato

El proyecto queda encendido. Entrada: http://localhost:8000.
Swagger de Pedidos: http://localhost:8003/docs.
RabbitMQ: http://localhost:15672, usuario `fastbite`, contraseña `fastbite_local`.

Para próximos arranques: `.\iniciar-fastbite.ps1`.
Para detener conservando datos: `.\fastbite.ps1 down`.
El README incluye la demostración paso a paso y los comandos de recreación del entorno.
