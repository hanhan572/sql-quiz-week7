"""Retrieval (ChromaDB + Gemini embeddings), generation (Gemini), execution, and validation."""

import itertools
import re
import time
from decimal import Decimal

import chromadb
import psycopg
import requests

from . import knowledge

API = "https://generativelanguage.googleapis.com/v1beta"
# Tried in order; the next one is used when a model is overloaded, slow, or unavailable.
DEFAULT_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-3.8-flash"]
DEFAULT_EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768
TOP_K = {"schema": 3, "semantics": 3, "example": 3}


# ---------------------------------------------------------------- Gemini API
def _post(cfg, path, payload, timeout=25, tries=3):
    last = None
    for attempt in range(tries):
        try:
            resp = requests.post(
                f"{API}/{path}",
                headers={"x-goog-api-key": cfg["GEMINI_API_KEY"], "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last = RuntimeError(f"Gemini request failed: {type(exc).__name__}")
        else:
            if resp.ok:
                return resp.json()
            last = RuntimeError(f"Gemini API error {resp.status_code}: {resp.text[:300]}")
            if resp.status_code not in (429, 500, 503):
                raise last
        if attempt < tries - 1:
            time.sleep(1 + attempt)
    raise last


def embed(cfg, texts, task):
    """Embed texts; task is RETRIEVAL_DOCUMENT for stored knowledge, RETRIEVAL_QUERY for questions."""
    model = cfg.get("GEMINI_EMBED_MODEL", DEFAULT_EMBED_MODEL)
    vectors = []
    for i in range(0, len(texts), 50):
        batch = texts[i:i + 50]
        data = _post(cfg, f"models/{model}:batchEmbedContents", {"requests": [
            {"model": f"models/{model}", "content": {"parts": [{"text": t}]},
             "taskType": task, "outputDimensionality": EMBED_DIM}
            for t in batch
        ]})
        vectors += [e["values"] for e in data["embeddings"]]
    return vectors


# ------------------------------------------------------------ knowledge base
def introspect_schema(db):
    """Read columns and foreign keys of the practice tables from PostgreSQL."""
    columns, fks = {}, {}
    with psycopg.connect(**db) as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = ANY(%s) ORDER BY table_name, ordinal_position",
            (list(knowledge.TABLES),),
        )
        for table, column, dtype in cur.fetchall():
            columns.setdefault(table, []).append(f"{column} {dtype}")
        cur.execute(
            "SELECT conrelid::regclass::text, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE contype = 'f' AND connamespace = 'public'::regnamespace"
        )
        for table, definition in cur.fetchall():
            if table in knowledge.TABLES:
                fks.setdefault(table, []).append(definition.replace("FOREIGN KEY ", "").replace("REFERENCES ", "-> "))
        conn.rollback()
    if not columns:
        raise RuntimeError("No practice tables found in information_schema.")
    return columns, fks


def schema_documents(db):
    try:
        columns, fks = introspect_schema(db)
        source = "live"
    except Exception:
        columns, fks, source = knowledge.FALLBACK_SCHEMA, knowledge.FALLBACK_FKS, "fallback"
    docs = []
    for table, cols in columns.items():
        text = f"Table public.{table} has columns: " + ", ".join(cols) + "."
        if fks.get(table):
            text += " Foreign keys: " + "; ".join(fks[table]) + "."
        docs.append((f"schema-{table}", text, {"type": "schema", "table": table}))
    return docs, source


def build_index(cfg, db):
    """Create an in-memory ChromaDB collection holding schema, semantics and few-shot examples."""
    schema_docs, schema_source = schema_documents(db)
    docs = list(schema_docs)
    for i, (topic, text) in enumerate(knowledge.SEMANTICS):
        docs.append((f"semantics-{i}", f"{topic}: {text}", {"type": "semantics", "topic": topic}))
    for i, (question, sql) in enumerate(knowledge.EXAMPLES):
        # The question is embedded (that is what users search with); the SQL travels as metadata.
        docs.append((f"example-{i}", question, {"type": "example", "sql": sql}))

    client = chromadb.EphemeralClient()
    collection = client.get_or_create_collection("text_to_sql_knowledge", metadata={"hnsw:space": "cosine"})
    collection.upsert(
        ids=[d[0] for d in docs],
        documents=[d[1] for d in docs],
        metadatas=[d[2] for d in docs],
        embeddings=embed(cfg, [d[1] for d in docs], "RETRIEVAL_DOCUMENT"),
    )
    return collection, {"documents": len(docs), "schema_source": schema_source}


def retrieve(cfg, collection, question):
    """Vector search per knowledge type; returns {type: [(text, metadata, distance)]}."""
    query_vector = embed(cfg, [question], "RETRIEVAL_QUERY")[0]
    results = {}
    for kind, k in TOP_K.items():
        found = collection.query(query_embeddings=[query_vector], n_results=k, where={"type": kind})
        results[kind] = list(zip(found["documents"][0], found["metadatas"][0], found["distances"][0]))
    return results


# ---------------------------------------------------------------- generation
SYSTEM_PROMPT = (
    "You are an expert PostgreSQL analyst. Convert the user's question into ONE read-only PostgreSQL SELECT "
    "query. Use only the tables and columns in the provided context. Follow the business rules exactly. "
    "Return only the SQL, with no explanation and no markdown."
)


def build_prompt(question, retrieved):
    parts = ["## Retrieved database schema"]
    parts += [f"- {text}" for text, _, _ in retrieved["schema"]]
    parts += ["", "## Retrieved business rules"]
    parts += [f"- {text}" for text, _, _ in retrieved["semantics"]]
    parts += ["", "## Retrieved similar solved examples"]
    for text, meta, _ in retrieved["example"]:
        parts += [f"Q: {text}", f"SQL: {meta['sql']}", ""]
    parts += [f"## Question\n{question}", "", "SQL:"]
    return "\n".join(parts)


def extract_sql(text):
    match = re.search(r"```(?:sql)?\s*(.*?)```", text, re.S | re.I)
    sql = (match.group(1) if match else text).strip()
    return sql.rstrip(";").strip() + ";"


def generate_sql(cfg, question, retrieved):
    prompt = build_prompt(question, retrieved)
    models = [cfg["GEMINI_MODEL"]] if cfg.get("GEMINI_MODEL") else DEFAULT_MODELS
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 4096},
    }
    errors = []
    for model in models:
        try:
            data = _post(cfg, f"models/{model}:generateContent", payload, tries=2)
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
            if text.strip():
                return extract_sql(text), prompt, model
            errors.append(f"{model}: empty response")
        except (RuntimeError, KeyError, IndexError) as exc:
            errors.append(f"{model}: {exc}")
    raise RuntimeError("All Gemini models failed. " + " | ".join(errors))


# ---------------------------------------------------------------- validation
def _norm(value):
    if isinstance(value, (Decimal, float, int)) and not isinstance(value, bool):
        return round(float(value), 2)
    return value if value is None or isinstance(value, (bool, str)) else str(value)


def _rows(frame):
    return [tuple(_norm(v) for v in row) for row in frame.itertuples(index=False, name=None)]


def compare_results(generated, expected):
    """Ignore row order, column names and (leniently) extra columns. Returns (ok, message)."""
    exp_rows = sorted(map(repr, _rows(expected)))
    if len(generated) != len(expected):
        return False, f"Row count differs: got {len(generated)}, expected {len(expected)}."
    gen_cols = list(range(generated.shape[1]))
    width = expected.shape[1]
    if len(gen_cols) < width:
        return False, f"Column count differs: got {len(gen_cols)}, expected {width}."
    for combo in itertools.permutations(gen_cols, width):
        picked = generated.iloc[:, list(combo)]
        if sorted(map(repr, _rows(picked))) == exp_rows:
            note = "" if len(gen_cols) == width else " (extra columns ignored)"
            return True, "Result matches the reference answer" + note + "."
    return False, "Values differ from the reference answer."
