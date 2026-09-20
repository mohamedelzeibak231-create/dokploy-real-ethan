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
ENV 
ENV DISCORD_TOKEN=MTUzMjU0NjcwNjUwMjE4OTA5Nw.GIJ7QL.Gy7sTOZMU0R4uWXKwDooa0VIw5L5-2tXaHP754
