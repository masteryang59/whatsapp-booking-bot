FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data/business.json ./data/business.json

RUN useradd --create-home bot && mkdir /app/var && chown bot /app/var
USER bot

ENV DB_PATH=/app/var/bookings.db
EXPOSE 8000
# One worker: reminders and per-customer locks live in this process.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
ENV TRANSFORM_DATA=true
ENV CLASSIFY_INTENT=true