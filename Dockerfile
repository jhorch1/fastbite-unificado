FROM python:3.12-slim
# Evita archivos .pyc y permite ver los mensajes de consola sin demora.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
# Instala antes de copiar el código para reutilizar esta capa al reconstruir.
RUN pip install --no-cache-dir -r requirements.txt
COPY users-service /app/users-service
COPY menu-service /app/menu-service
COPY orders-service /app/orders-service
COPY notifier-service /app/notifier-service
COPY gateway-service /app/gateway-service
COPY tests /app/tests
# El usuario de la aplicación necesita escribir únicamente sus datos en /data.
RUN useradd --uid 10001 --create-home fastbite && mkdir -p /data && chown fastbite:fastbite /data
USER fastbite
WORKDIR /app/gateway-service
# Compose cambia la carpeta y el comando para ejecutar cada servicio con esta imagen.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
