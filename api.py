from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import psycopg2
from psycopg2.extras import RealDictCursor
import urllib.parse
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ΤΟ URL ΤΟΥ CLOUDFLARE ΣΟΥ ---
PUBLIC_BASE_URL = "https://pub-fba8694ecb8b4b62b4b01308c630548c.r2.dev"

# --- Η ΝΕΑ ΣΟΥ ΒΑΣΗ ΣΤΟ CLOUD (Supabase) ---
# ΚΑΝΕ ΕΠΙΚΟΛΛΗΣΗ ΤΟ URI LINK ΣΟΥ ΑΝΑΜΕΣΑ ΣΤΑ ΑΥΤΑΚΙΑ:
DB_URL = "postgresql://postgres.kfaodzjcdtmwwxwyzasq:SupaSofos02%23@aws-1-eu-central-1.pooler.supabase.com:6543/postgres"

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

@app.get("/search")
def search_books(q: str = "", free_only: bool = False):
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Το βασικό μας ερώτημα
        query = """
            SELECT * FROM books 
            WHERE (title ILIKE %s OR author_publisher ILIKE %s)
            AND file_key IS NOT NULL
        """
        
        # Αν κάποιος δεν είναι συνδεδεμένος, του δείχνουμε ΜΟΝΟ τα δωρεάν!
        if free_only:
            query += " AND is_free = TRUE"
            
        # Βάζουμε την ταξινόμηση στο τέλος
        query += " ORDER BY title ASC;"
        
        search_val = f"%{q}%"
        cursor.execute(query, (search_val, search_val))
        books = cursor.fetchall()
        
        for book in books:
            files = book['file_key'].split(', ')
            book['pdf_urls'] = [f"{PUBLIC_BASE_URL}/{urllib.parse.quote(f)}" for f in files]
            
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
