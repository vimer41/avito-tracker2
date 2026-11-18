FROM python:3.11-slim

# Установка зависимостей ОС для curl_cffi, ONNX/OpenCV и сборки
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    libgomp1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Установка python-зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование исходного кода
COPY . .

# Создание рабочего пользователя и каталогов с ограниченными правами
RUN useradd -m -u 10001 -s /bin/bash appuser \
    && mkdir -p /app/data /app/logs /app/backups \
    && chown -R appuser:appuser /app

USER appuser

# Запуск бота
CMD ["python", "bot.py"]
