FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8087

# Create working directory and data directories
WORKDIR /workspace

# Install system dependencies (curl & openssl for SSL certificate generation)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    openssl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY app/ ./app/

# Expose port for Web UI & OAuth Callback
EXPOSE 8087

# Define persistent data volume
VOLUME ["/data"]

# Run dreeve-zwift-connector application
CMD ["python", "-m", "app.main"]
