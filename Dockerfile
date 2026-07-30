# Use Microsoft's official pre-configured Playwright Python image (v1.44.0)
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# Copy requirements file first
COPY requirements.txt .

# Install Python dependencies (includes patchright from GitHub)
RUN pip install --no-cache-dir -r requirements.txt

# Ensure Playwright browsers & dependencies are installed
RUN playwright install --with-deps chromium

# Install stealth Chromium via patchright
RUN python3 -m patchright

# Copy rest of application code
COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]
