FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY soxl_bot.py .
RUN mkdir -p /app/data

CMD ["python", "soxl_bot.py"]
