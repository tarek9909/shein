from sqlalchemy import create_engine, text
import os

db_url = os.getenv("DB_URL")
engine = create_engine(db_url, pool_pre_ping=True)

with engine.connect() as conn:
    print(conn.execute(text("SELECT 1")).scalar())

print("DB OK")
