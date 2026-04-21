# Usamos la misma imagen base
FROM python:3.10-slim

# Establecemos el directorio de trabajo
WORKDIR /app

# Copiamos el archivo de requerimientos PRIMERO (esto optimiza la caché de Docker)
COPY requirements.txt .

# Instalamos las librerías automáticamente al construir la imagen
RUN pip install --no-cache-dir -r requirements.txt

# Mantenemos el contenedor vivo (igual que hacíamos en el compose)
CMD ["tail", "-f", "/dev/null"]