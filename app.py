"""Week 7 SQL Quiz — Streamlit + Supabase Auth + read-only PostgreSQL."""

from datetime import datetime, timezone

import pandas as pd
import psycopg
import sqlparse
import streamlit as st
from supabase import create_client

st.set_page_config(page_title="SQL Quiz | Online Store", page_icon="🗃️", layout="wide")
st.title("🗃️ Online Store SQL Quiz")
st.caption("Five questions · 4 points each · 20 points total · Live Supabase PostgreSQL")

REQUIRED = ("SUPABASE_URL", "SUPABASE_KEY", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
if any(key not in st.secrets for key in REQUIRED):
    st.error("The app is missing required Secrets. Configure them in Streamlit Cloud app settings.")
    st.stop()


def supabase_client():
    """A client per Streamlit execution; never share sessions across users."""
    return create_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_KEY"])


def checked_user(client):
    """Verify the access token with Supabase Auth on every app rerun."""
    access_token = st.session_state.get("access_token")
    if not access_token:
        return None
    try:
        response = client.auth.get_user(access_token)
        return response.user
    except Exception:
        st.session_state.pop("access_token", None)
        st.session_state.pop("refresh_token", None)
        return None


def authorized_client(client):
    """Attach the verified student's access token for RLS-protected requests."""
    client.postgrest.auth(st.session_state["access_token"])
    return client


def readonly_query(query):
    """Run a single SELECT query under the least-privileged database login."""
    statements = sqlparse.parse(query.strip())
    if len(statements) != 1 or statements[0].get_type() != "SELECT":
        raise ValueError("Only one SELECT query is allowed (WITH ... SELECT is supported).")
    # PostgreSQL read-only transactions and quiz_reader grants are the real security boundary.
    # A 5-second statement timeout and fetch limit reduce resource abuse.
    params = dict(
        host=st.secrets["DB_HOST"],
        port=int(st.secrets["DB_PORT"]),
        dbname=st.secrets["DB_NAME"],
        user=st.secrets["DB_USER"],
        password=st.secrets["DB_PASSWORD"],
        sslmode=st.secrets.get("DB_SSLMODE", "require"),
        connect_timeout=8,
        options="-c statement_timeout=5000",
    )
    with psycopg.connect(**params) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = '5s'")
            cur.execute(query)
            if cur.description is None:
                raise ValueError("The query did not produce a result table.")
            columns = [col.name for col in cur.description]
            rows = cur.fetchmany(101)
            conn.rollback()
    return pd.DataFrame(rows[:100], columns=columns), len(rows) > 100


client = supabase_client()
user = checked_user(client)

if user is None:
    st.info("Sign in to open questions, run SQL, and save your answers.")
    login_tab, register_tab = st.tabs(["Sign in", "Sign up"])
    with login_tab:
        with st.form("login"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            login = st.form_submit_button("Sign in", use_container_width=True)
        if login:
            try:
                response = client.auth.sign_in_with_password({"email": email, "password": password})
                if response.session is None:
                    st.error("No session returned. Please verify your email.")
                else:
                    st.session_state["access_token"] = response.session.access_token
                    st.session_state["refresh_token"] = response.session.refresh_token
                    st.rerun()
            except Exception as exc:
                st.error(f"Sign-in failed: {exc}")
    with register_tab:
        with st.form("register"):
            signup_email = st.text_input("Email address")
            signup_password = st.text_input("Choose a password", type="password")
            register = st.form_submit_button("Create account", use_container_width=True)
        if register:
            try:
                response = client.auth.sign_up({"email": signup_email, "password": signup_password})
                if response.session:
                    st.session_state["access_token"] = response.session.access_token
                    st.session_state["refresh_token"] = response.session.refresh_token
                    st.rerun()
                st.success("Account requested. Check your email to confirm it, then sign in.")
            except Exception as exc:
                st.error(f"Sign-up failed: {exc}")
    st.stop()  # Crucial: no DB execution or saving available to signed-out users.

with st.sidebar:
    st.success(f"Signed in: {user.email}")
    if st.button("Sign out"):
        try:
            client.auth.sign_out()
        except Exception:
            pass
        for key in ("access_token", "refresh_token"):
            st.session_state.pop(key, None)
        st.rerun()
    st.markdown("**Allowed SQL:** one read-only `SELECT` query (or `WITH ... SELECT`).")
    st.markdown("**Available practice tables:** `customers`, `products`, `orders`, `order_items`.")
    st.caption("Revenue and spending include all orders, including Pending orders.")

# Even though Supabase RLS enforces access, verify an Auth user before every app operation.
client = authorized_client(client)
try:
    questions = (
        client.table("quiz_questions")
        .select("question_id,question_text,points,hint_sql,expected_result")
        .order("question_id")
        .execute()
        .data
    )
except Exception as exc:
    st.error(f"Could not load quiz questions. Check table permissions and Secrets: {exc}")
    st.stop()

if not questions:
    st.warning("No quiz questions are available.")
    st.stop()

st.write(f"**Total:** {sum(int(q['points']) for q in questions)} points")
choice = st.selectbox(
    "Choose a question",
    range(len(questions)),
    format_func=lambda i: f"Question {questions[i]['question_id']} — {questions[i]['points']} pts",
)
question = questions[choice]
question_id = int(question["question_id"])
st.subheader(f"Question {question_id} — {question['points']} points")
st.write(question["question_text"])

with st.expander("Hint / Answer key (for checking your work)"):
    st.code(question["hint_sql"], language="sql")
    st.markdown(f"**Expected result:** {question['expected_result']}")

try:
    saved = (
        client.table("quiz_answers")
        .select("submitted_sql,updated_at")
        .eq("user_id", str(user.id))
        .eq("question_id", question_id)
        .limit(1)
        .execute()
        .data
    )
except Exception as exc:
    st.error(f"Could not load saved answer: {exc}")
    st.stop()

editor_key = f"sql_{user.id}_{question_id}"
if editor_key not in st.session_state:
    st.session_state[editor_key] = saved[0]["submitted_sql"] if saved else "SELECT * FROM public.customers LIMIT 5;"

query = st.text_area("Write your SQL", key=editor_key, height=180)
col_run, col_save = st.columns(2)
with col_run:
    run = st.button("▶ Run SQL", type="primary", use_container_width=True)
with col_save:
    save = st.button("💾 Save answer", use_container_width=True)

if run:
    # User token was verified at the start of this execution. Database role is read-only.
    try:
        frame, truncated = readonly_query(query)
        st.success(f"Query completed. Showing {len(frame)} row(s).")
        st.dataframe(frame, use_container_width=True)
        if truncated:
            st.warning("Only the first 100 rows are shown.")
    except ValueError as exc:
        st.warning(str(exc))
    except Exception as exc:
        st.error(f"Query could not run: {exc}")

if save:
    try:
        client.table("quiz_answers").upsert(
            {
                "user_id": str(user.id),
                "question_id": question_id,
                "submitted_sql": query,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="user_id,question_id",
        ).execute()
        st.success("Your answer was saved!")
    except Exception as exc:
        st.error(f"Could not save your answer: {exc}")
