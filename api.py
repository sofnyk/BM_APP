from fastapi import FastAPI, Response, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional
import psycopg2
from psycopg2.extras import RealDictCursor
import urllib.parse
import httpx
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ΒΑΣΗ (Supabase), AUTH & STORAGE (Cloudflare R2) ---
DB_URL = "postgresql://postgres.kfaodzjcdtmwwxwyzasq:SupaSofos02%23@aws-1-eu-central-1.pooler.supabase.com:6543/postgres"
R2_STORAGE_URL = "https://pub-fba8694ecb8b4b62b4b01308c630548c.r2.dev"
SUPABASE_URL = "https://kfaodzjcdtmwwxwyzasq.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImtmYW9kempjZHRtd3d4d3l6YXNxIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzczNTUyOTQsImV4cCI6MjA5MjkzMTI5NH0.YhmIdxKx51KgCiYRaSlSlgVhoM8EAM3qdKBHlBnx1s8"
INITIAL_ADMIN_EMAIL = "sofoklisnyk@gmail.com"


class LinkRequest(BaseModel):
    book_id: int
    file_keys: list[str]


class NewBook(BaseModel):
    title: str
    author_publisher: str = ""
    publication_year: str = ""


class UpdateBookRequest(BaseModel):
    title: str
    author_publisher: Optional[str] = ""
    publication_year: Optional[str] = None
    is_free: bool = True


class AdminEmailRequest(BaseModel):
    email: str


def init_admins_table():
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS public.admins (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        cursor.execute(
            "INSERT INTO public.admins (email) VALUES (%s) ON CONFLICT (email) DO NOTHING;",
            (INITIAL_ADMIN_EMAIL.lower(),),
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print("Admin table init warning:", e)


@app.on_event("startup")
def on_startup():
    init_admins_table()


async def get_authenticated_user(authorization: Optional[str]) -> Optional[dict]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{SUPABASE_URL}/auth/v1/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": SUPABASE_ANON_KEY,
                },
            )
            if resp.status_code == 200:
                return resp.json()
            return None
    except Exception:
        return None


def is_email_admin(email: str) -> bool:
    if not email:
        return False
    conn = psycopg2.connect(DB_URL)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM public.admins WHERE LOWER(email) = LOWER(%s);", (email.strip(),))
    found = cursor.fetchone() is not None
    cursor.close()
    conn.close()
    return found


async def require_admin(authorization: Optional[str]) -> dict:
    user = await get_authenticated_user(authorization)
    if not user or not user.get("email"):
        raise HTTPException(status_code=401, detail="Απαιτείται σύνδεση.")
    if not is_email_admin(user["email"]):
        raise HTTPException(status_code=403, detail="Απαιτούνται δικαιώματα διαχειριστή.")
    return user


@app.get("/")
def serve_home():
    return FileResponse("index.html")


@app.get("/robots.txt")
def serve_robots():
    robot_content = (
        "User-agent: *\n"
        "Allow: /\n"
        "Sitemap: https://psifiobook.onrender.com/sitemap.xml\n"
    )
    return PlainTextResponse(robot_content)


@app.get("/sitemap.xml")
def serve_sitemap():
    xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://psifiobook.onrender.com/</loc>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>"""
    return Response(content=xml_content, media_type="application/xml")


@app.get("/pdf/{file_path:path}")
async def proxy_pdf(file_path: str, download: bool = False):
    r2_url = f"{R2_STORAGE_URL}/{urllib.parse.quote(file_path, safe='/')}"
    client = httpx.AsyncClient(timeout=60.0)
    req = client.build_request("GET", r2_url)
    r = await client.send(req, stream=True)

    if r.status_code != 200:
        await r.aclose()
        await client.aclose()
        raise HTTPException(status_code=r.status_code, detail="Το αρχείο PDF δεν βρέθηκε.")

    async def iterfile():
        try:
            async for chunk in r.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await r.aclose()
            await client.aclose()

    filename = os.path.basename(file_path)
    encoded_filename = urllib.parse.quote(filename)
    disposition = "attachment" if download else "inline"

    headers = {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{encoded_filename}",
        "Cache-Control": "public, max-age=86400",
    }
    if "content-length" in r.headers:
        headers["Content-Length"] = r.headers["content-length"]

    return StreamingResponse(
        iterfile(),
        media_type="application/pdf",
        headers=headers,
    )


@app.get("/search")
async def search_books(
    q: str = "",
    free_only: bool = False,
    authorization: Optional[str] = Header(default=None),
):
    try:
        user = await get_authenticated_user(authorization)
        is_authenticated = user is not None

        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        query = """
            SELECT * FROM books 
            WHERE (title ILIKE %s OR author_publisher ILIKE %s)
            AND file_key IS NOT NULL
        """

        if not is_authenticated or free_only:
            query += " AND is_free = TRUE"

        query += " ORDER BY title ASC;"

        search_val = f"%{q}%"
        cursor.execute(query, (search_val, search_val))
        books = cursor.fetchall()

        for book in books:
            files = book['file_key'].split(', ')
            clean_urls = []
            for f in files:
                f = f.strip()
                if f.startswith('http'):
                    clean_urls.append(f)
                else:
                    clean_urls.append(f"/pdf/{urllib.parse.quote(f, safe='/')}")

            book['pdf_urls'] = clean_urls

        cursor.close()
        conn.close()
        return {"status": "success", "data": books}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ======== ADMIN ENDPOINTS (ΠΡΟΣΤΑΤΕΥΜΕΝΑ ΜΕ JWT + ΠΙΝΑΚΑ ADMINS) ========

@app.get("/admin/me")
async def check_admin_status(authorization: Optional[str] = Header(default=None)):
    user = await get_authenticated_user(authorization)
    if not user or not user.get("email"):
        return {"is_admin": False}
    try:
        return {"is_admin": is_email_admin(user["email"]), "email": user["email"]}
    except Exception:
        return {"is_admin": False}


@app.get("/admin/books")
async def admin_list_books(authorization: Optional[str] = Header(default=None)):
    await require_admin(authorization)
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("""
            SELECT id, title, author_publisher, publication_year, is_free, file_key
            FROM books
            ORDER BY title ASC;
        """)
        books = cursor.fetchall()
        cursor.close()
        conn.close()
        return {"status": "success", "data": books}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.put("/admin/books/{book_id}")
async def admin_update_book(
    book_id: int,
    req: UpdateBookRequest,
    authorization: Optional[str] = Header(default=None),
):
    await require_admin(authorization)
    if not req.title.strip():
        raise HTTPException(status_code=400, detail="Ο τίτλος είναι υποχρεωτικός.")
    try:
        year_val = req.publication_year.strip() if req.publication_year else None
        if year_val == "":
            year_val = None

        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE books
            SET title = %s,
                author_publisher = %s,
                publication_year = %s,
                is_free = %s
            WHERE id = %s
            """,
            (req.title.strip(), (req.author_publisher or "").strip(), year_val, req.is_free, book_id),
        )
        conn.commit()
        cursor.close()
        conn.close()
        return {"status": "success", "message": "Το βιβλίο ενημερώθηκε."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.delete("/admin/books/{book_id}")
async def admin_delete_book(book_id: int, authorization: Optional[str] = Header(default=None)):
    await require_admin(authorization)
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM books WHERE id = %s;", (book_id,))
        conn.commit()
        cursor.close()
        conn.close()
        return {"status": "success", "message": "Το βιβλίο διαγράφηκε."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/admin/admins")
async def admin_list_admins(authorization: Optional[str] = Header(default=None)):
    await require_admin(authorization)
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT id, email, created_at FROM public.admins ORDER BY id ASC;")
        admins = cursor.fetchall()
        cursor.close()
        conn.close()
        return {"status": "success", "data": admins}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/admin/admins")
async def admin_add_admin(req: AdminEmailRequest, authorization: Optional[str] = Header(default=None)):
    await require_admin(authorization)
    email = req.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Μη έγκυρο email.")
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO public.admins (email) VALUES (%s) ON CONFLICT (email) DO NOTHING;",
            (email,),
        )
        conn.commit()
        cursor.close()
        conn.close()
        return {"status": "success", "message": f"Ο χρήστης {email} προστέθηκε στους διαχειριστές."}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.delete("/admin/admins/{admin_id}")
async def admin_remove_admin(admin_id: int, authorization: Optional[str] = Header(default=None)):
    current_user = await require_admin(authorization)
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT email FROM public.admins WHERE id = %s;", (admin_id,))
        row = cursor.fetchone()
        if not row:
            cursor.close()
            conn.close()
            raise HTTPException(status_code=404, detail="Ο διαχειριστής δεν βρέθηκε.")
        if row["email"].lower() == current_user["email"].lower():
            cursor.close()
            conn.close()
            raise HTTPException(status_code=400, detail="Δεν μπορείτε να αφαιρέσετε τον εαυτό σας.")

        cursor.execute("DELETE FROM public.admins WHERE id = %s;", (admin_id,))
        conn.commit()
        cursor.close()
        conn.close()
        return {"status": "success", "message": "Ο διαχειριστής αφαιρέθηκε."}
    except HTTPException:
        raise
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ======== ΤΟΠΙΚΑ ENDPOINTS (admin.html) ========

@app.get("/books")
def get_all_books():
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM books WHERE file_key IS NULL OR file_key = '' ORDER BY id ASC;")
        books = cursor.fetchall()
        cursor.close()
        conn.close()
        return {"status": "success", "total_books": len(books), "data": books}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/books")
def create_book(book: NewBook):
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO books (title, author_publisher, publication_year) VALUES (%s, %s, %s)",
            (book.title, book.author_publisher, book.publication_year)
        )
        conn.commit()
        cursor.close()
        conn.close()
        return {"status": "success", "message": "Προστέθηκε επιτυχώς"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/files")
def get_local_files():
    try:
        folder = 'db_pdf'
        if not os.path.exists(folder):
            return {"status": "error", "message": "Ο φάκελος db_pdf δεν βρέθηκε."}

        all_physical_files = [f for f in os.listdir(folder) if f.lower().endswith('.pdf')]
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("SELECT file_key FROM books WHERE file_key IS NOT NULL AND file_key != '';")
        rows = cursor.fetchall()

        used_files = set()
        for row in rows:
            if row[0]:
                names = [name.strip() for name in row[0].split(', ')]
                used_files.update(names)

        cursor.close()
        conn.close()

        available_files = [f for f in all_physical_files if f not in used_files]
        return {"status": "success", "total_files": len(available_files), "data": sorted(available_files)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/link")
def link_file_to_book(req: LinkRequest):
    try:
        joined_files = ", ".join(req.file_keys)
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()
        cursor.execute("UPDATE books SET file_key = %s WHERE id = %s", (joined_files, req.book_id))
        conn.commit()
        cursor.close()
        conn.close()
        return {"status": "success", "message": "Συνδέθηκε!"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
