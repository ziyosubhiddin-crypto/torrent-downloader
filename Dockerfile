FROM python:3.9-slim

# Install system dependencies (aria2 is required to download torrents)
RUN apt-get update && apt-get install -y --no-install-recommends \
    aria2 \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY config.py downloader.py telegram_uploader.py main.py ./
COPY static/ ./static/

# Expose FastAPI port
EXPOSE 8000

# Start FastAPI server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
