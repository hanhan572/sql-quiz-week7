"""Verify GitHub Actions can read hosted Supabase PostgreSQL."""
import os
import psycopg

password = os.environ.get('DB_PASSWORD')
if not password:
    raise RuntimeError('DB_PASSWORD GitHub Secret is not configured')

with psycopg.connect(
    host='aws-1-us-west-2.pooler.supabase.com',
    port=5432,
    dbname='postgres',
    user='quiz_reader.qmstlzlxllfijcwytgte',
    password=password,
    sslmode='require',
    connect_timeout=15,
    options='-c statement_timeout=5000',
) as conn:
    with conn.cursor() as cur:
        cur.execute('SET TRANSACTION READ ONLY')
        cur.execute('SELECT COUNT(*) FROM public.customers')
        count = cur.fetchone()[0]
        assert count == 8, f'Expected 8 customers, got {count}'
        print(f'PASS: Supabase PostgreSQL reachable; customers={count}')
    conn.rollback()
