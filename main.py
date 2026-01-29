import base64
import os
import re
import uuid
import time
from datetime import datetime
from glob import glob
from typing import Optional, List, Literal, Dict, Tuple

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from docx import Document
from docx.shared import Pt

# ======================================================
# CONFIGURACIÓN
# ======================================================
API_BEARER_TOKEN = os.getenv("DOCGEN_API_TOKEN", "sk-docgen-change-me")
# Usamos /tmp porque en Render Free es el único sitio escribible garantizado
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/tmp/docgen_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
DOWNLOAD_TTL_SECONDS = int(os.getenv("DOWNLOAD_TTL_SECONDS", "86400"))

app = FastAPI(
    title="Document Generator API",
    version="1.3.0",
    description="Generates DOCX documents with robust persistence fix"
)

# ======================================================
# MODELOS
# ======================================================
DocumentType = Literal["cover_letter", "cv", "recruiter_scorecard"]
FormatType = Literal["docx"]

class Candidate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None

class Section(BaseModel):
    heading: Optional[str] = None
    text: str

class Content(BaseModel):
    body_markdown: str = Field(..., description="Main content")
    sections: Optional[List[Section]] = None

class CreateDocumentRequest(BaseModel):
    document_type: DocumentType
    format: FormatType
    language: str
    title: str
    candidate: Optional[Candidate] = None
    content: Content

class CreateDocumentResponse(BaseModel):
    file_id: str
    file_name: str
    content_type: str
    download_url: str
    download_markdown: str
    file_base64: Optional[str] = None

# ======================================================
# UTILIDADES
# ======================================================
def verify_bearer_token(authorization: Optional[str]) -> None:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    expected = f"Bearer {API_BEARER_TOKEN}"
    if authorization.strip() != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

def sanitize_filename(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[^\w\-\. ]+", "", name, flags=re.UNICODE)
    name = re.sub(r"\s+", "_", name)
    return name[:50]  # Limitamos longitud para evitar problemas de filesystem

def add_text_with_basic_markdown(doc: Document, text: str) -> None:
    lines = text.replace("\r\n", "\n").split("\n")
    bullet_buffer = []

    def flush_bullets():
        nonlocal bullet_buffer
        for bullet in bullet_buffer:
            p = doc.add_paragraph(bullet, style="List Bullet")
            for run in p.runs: run.font.size = Pt(11)
        bullet_buffer = []

    for line in lines:
        line = line.rstrip()
        if not line.strip():
            flush_bullets()
            doc.add_paragraph("")
            continue
        if line.startswith("## "):
            flush_bullets()
            doc.add_heading(line[3:], level=2)
            continue
        if line.startswith("# "):
            flush_bullets()
            doc.add_heading(line[2:], level=1)
            continue
        if line.startswith("- ") or line.startswith("* "):
            bullet_buffer.append(line[2:])
            continue
        
        flush_bullets()
        p = doc.add_paragraph(line)
        for run in p.runs: run.font.size = Pt(11)
    flush_bullets()

def cleanup_expired_files() -> None:
    """Borra ficheros antiguos del disco"""
    now = time.time()
    # Busca todos los .docx en la carpeta
    for p in glob(os.path.join(OUTPUT_DIR, "*.docx")):
        try:
            if now - os.path.getmtime(p) > DOWNLOAD_TTL_SECONDS:
                os.remove(p)
        except Exception:
            pass

def build_docx(req: CreateDocumentRequest) -> Tuple[str, str, str]:
    doc = Document()
    doc.add_heading(req.title, level=1)
    
    # Metadata
    meta = []
    if req.candidate and req.candidate.full_name:
        meta.append(req.candidate.full_name)
    meta.append(datetime.now().strftime("%Y-%m-%d"))
    p = doc.add_paragraph(" | ".join(meta))
    for run in p.runs: run.font.size = Pt(9)
    doc.add_paragraph("")

    # Body
    add_text_with_basic_markdown(doc, req.content.body_markdown)

    # Sections
    if req.content.sections:
        doc.add_page_break()
        for section in req.content.sections:
            if section.heading:
                doc.add_heading(section.heading, level=2)
            add_text_with_basic_markdown(doc, section.text)

    # --- CORRECCIÓN CLAVE ---
    file_id = uuid.uuid4().hex
    safe_title = sanitize_filename(req.title)
    
    # El nombre en disco AHORA incluye el título y el ID, coincidiendo con el patrón de búsqueda
    file_name_on_disk = f"{safe_title}_{file_id}.docx"
    file_path = os.path.join(OUTPUT_DIR, file_name_on_disk)
    
    doc.save(file_path)
    return file_id, file_name_on_disk, file_path

# ======================================================
# ENDPOINTS
# ======================================================
@app.get("/health")
def health():
    return {"status": "ok", "version": "1.3.0", "storage": OUTPUT_DIR}

@app.post("/v1/documents", response_model=CreateDocumentResponse)
def create_document(req: CreateDocumentRequest, authorization: Optional[str] = Header(None)):
    verify_bearer_token(authorization)
    cleanup_expired_files()
    
    file_id, file_name, file_path = build_docx(req)
    
    # Construcción URL
    base_url = PUBLIC_BASE_URL if PUBLIC_BASE_URL else ""
    download_url = f"{base_url}/v1/download/{file_id}"
    download_markdown = f"[Download DOCX]({download_url})"

    # Base64 opcional por si acaso
    with open(file_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")

    return CreateDocumentResponse(
        file_id=file_id,
        file_name=file_name,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        download_url=download_url,
        download_markdown=download_markdown,
        file_base64=encoded
    )

@app.get("/v1/download/{file_id}")
def download_document(file_id: str):
    cleanup_expired_files()
    
    # --- BÚSQUEDA ROBUSTA ---
    # Buscamos cualquier archivo que termine en _{file_id}.docx
    # Esto encuentra el archivo aunque el servidor se haya reiniciado
    pattern = os.path.join(OUTPUT_DIR, f"*_{file_id}.docx")
    matches = glob(pattern)
    
    if not matches:
        # Fallback: intentar buscar solo por ID si el patrón complejo falla
        direct_path = os.path.join(OUTPUT_DIR, f"{file_id}.docx")
        if os.path.exists(direct_path):
            matches = [direct_path]
        else:
            raise HTTPException(status_code=404, detail="File not found (expired or unknown ID). Please regenerate.")
    
    file_path = matches
    
    return FileResponse(
        path=file_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=os.path.basename(file_path)
    )
