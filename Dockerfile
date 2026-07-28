# Use official Python lightweight base image
FROM python:3.10-slim

# Set working directory inside container
WORKDIR /app

# Prevent Python from writing pyc files and buffer outputs
ENV PYTHONUNBUFFERED=1

# Copy requirements file first to utilize Docker layer caching
COPY requirements.txt .

# Install Python packages and Playwright Chromium with browser dependencies
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install chromium && \
    playwright install-deps chromium

# Copy rest of application code
COPY . .

# Expose FastAPI default port
EXPOSE 8000

# Start Uvicorn app server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
