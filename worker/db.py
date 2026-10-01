import os
from contextlib import contextmanager

import psycopg2


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("POSTGRES_HOST", "db"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
    )


@contextmanager
def db_cursor(commit: bool = False):
    """Short-lived connection + cursor for a single unit of work. Personal/
    lab scale — a fresh connection per request is simpler and safer than
    hand-rolling a pool here. Swap in psycopg2.pool or SQLAlchemy if this
    ever needs to handle real concurrent load."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        yield cur
        if commit:
            conn.commit()
    finally:
        conn.close()
