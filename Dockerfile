FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Downloaded files are written here temporarily then deleted after sending
RUN mkdir -p /app/downloads

CMD ["python", "bot.py"]
