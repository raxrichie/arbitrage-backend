FROM python:3.11-slim

# 1. Install Linux system utilities required for git dependencies & Chromium OS libraries
RUN apt-get update && apt-get install -y \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2. Copy and install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Downloads Patchright Chromium stealth binaries + Linux OS dependencies
RUN python -m patchright install chromium --with-deps

# 4. Copy remaining application files
COPY . .

EXPOSE 8000

# 5. Start the FastAPI server using Uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
