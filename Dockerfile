FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY telegram_forwarder.py bot_menu.py db.py ./

VOLUME /data

CMD ["python", "telegram_forwarder.py"]
