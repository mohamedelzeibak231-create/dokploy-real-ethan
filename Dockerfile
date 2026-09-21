FROM python:3.11-slim

# Prevents Python from buffering stdout/stderr (logs show up immediately)
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Persist the sqlite db outside the image layer if you mount a volume here
VOLUME ["/app/data"]
ENV DB_PATH=/app/data/skullix.db

CMD ["python", "main.py"]
ENV DISCORD_TOKEN=DISCORD_TOKEN=MTUzMjU0NjcwNjUwMjE4OTA5Nw.GMaxoH.-T9Wx64HfwlqMJr7gJW6NNjMYz96QRq6KjNXKk
