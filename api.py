from fastapi import FastAPI, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel
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

# --- Η ΒΑΣΗ ΣΤΟ CLOUD (Supabase) & STORAGE (Cloudflare R2) ---
DB_URL = "postgresql://postgres.kfaodzjcdtmwwxwyzasq:SupaSofos02%23@aws-1-eu-central-1.pooler.supabase.com:6543/postgres"
R2_STORAGE_URL = "https://pub-fba8694ecb8b4b62b4b01308c630548c.r2.dev"


class LinkRequest(BaseModel):
    book_id: int
    file_keys: list[str]


class NewBook(BaseModel):
    title: str
    author_publisher: str = ""
    publication_year: str = ""


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
def search_books(q: str = "", free_only: bool = False):
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        query = """
            SELECT * FROM books 
            WHERE (title ILIKE %s OR author_publisher ILIKE %s)
            AND file_key IS NOT NULL
        """

        if free_only:
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
