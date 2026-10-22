import os

POSTGRES_URL = os.getenv("POSTGRES_URL", "postgresql://postgres:password@localhost:5432/arbradar")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
