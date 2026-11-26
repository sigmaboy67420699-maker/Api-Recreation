import os

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "OGYeeps")
SERVER_PORT = int(os.getenv("PORT", "8000"))
SEED_DB = 0
