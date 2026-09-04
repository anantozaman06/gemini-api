# Use official lightweight Python image
FROM python:3.11-slim

# Set environment variables for Python and Hugging Face Spaces
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860 \
    HOME=/home/user

# Install system utilities needed for operations
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create user with UID 1000 (Required for Hugging Face Spaces non-root execution)
RUN useradd -m -u 1000 user

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY --chown=user:user . /app

# Ensure output and log directories exist and have proper permissions
RUN mkdir -p /app/logs /app/outputs && chown -R user:user /app

# Switch to non-root user
USER user

# Hugging Face default internal HTTP port
EXPOSE 7860

# Run the API server
CMD ["python", "server.py"]
