# Use Microsoft's official pre-configured Playwright Python image (v1.44.0)
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

# Set working directory inside container
WORKDIR /app

# Force Python to instantly output log streams without buffering
ENV PYTHONUNBUFFERED=1

# Copy requirements file first
COPY requirements.txt .

# Install Python application dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Ensure Playwright browsers & dependencies are installed
RUN playwright install --with-deps

# Download patchright's stealth Chromium browser binary
RUN python3 -m patchright install chromium

# Copy rest of application code
COPY . .

# Expose FastAPI port
EXPOSE 8000

# Start Uvicorn app server with unbuffered log output
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
