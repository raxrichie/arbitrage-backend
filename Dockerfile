# Use Microsoft's official pre-configured Playwright Python image
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

# Set working directory inside container
WORKDIR /app

# Prevent Python from writing pyc files and buffer outputs
ENV PYTHONUNBUFFERED=1

# Copy requirements file first to leverage Docker caching
COPY requirements.txt .

# Install Python application dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy rest of application code
COPY . .

# Expose FastAPI port
EXPOSE 8000

# Start Uvicorn app server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
