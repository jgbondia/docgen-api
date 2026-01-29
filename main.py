import base64
import os
import re
import uuid
import time
from datetime import datetime
from glob import glob
from typing import Optional, List, Literal, Tuple

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from docx import Document
from docx.shared import Pt

# ======================================================
# CONFIGURACIÓN ROBUSTA (Hardcoded para evitar errores de ENV)
# ======================================================
API_BEARER_TOKEN = os.getenv("DOCGEN_API_TOKEN", "sk-docgen-change-me")

# EN RENDER SIEMPRE USAR /tmp
OUTPUT_DIR = "/tmp/docgen_output"
# Aseguramos que existe al arrancar
try:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
except Exception as e:
    print(f"Error creating dir: {e}")

# URL pública para construir links absolutos
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

app = FastAPI(title="Document Generator API - Debug Version")

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
    name = str(name).strip()
    name = re.sub(r"[^\w\-\. ]+", "", name, flags=re.UNICODE)
    name = re.sub(r"\s+", "_", name)
    return name[:50]

def add_text_with_basic_markdown(doc: Document, text: str) -> None:
    if not text: return
    lines = text.replace("\r\n", "\n").split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            doc.add_paragraph("")
            continue
        if line.startswith("# "):
            doc.add_heading(line[2:], level=1)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=2)
        elif line.startswith("- ") or line.startswith("* "):
            p = doc.add_paragraph(line[2:], style="List Bullet")
        else:
            doc.add_paragraph(line)

def build_docx(req: CreateDocumentRequest) -> Tuple[str, str, str]:
    doc = Document()
    doc.add_heading(req.title, level=1)
    
    # Metadata
    if req.candidate:
        meta = f"Candidate: {req.candidate.full_name or 'N/A'} | Date: {datetime.now().strftime('%Y-%m-%d')}"
        p = doc.add_paragraph(meta)
        p.italic = True
    
    doc.add_paragraph("") # Spacer

    # Body
    add_text_with_basic_markdown(doc, req.content.body_markdown)

    # Sections
    if req.content.sections:
        doc.add_page_break()
        for section in req.content.sections:
            if section.heading:
                doc.add_heading(section.heading, level=2)
            add_text_with_basic_markdown(doc, section.text)

    # Guardado seguro
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
    except:
        pass

    file_id = uuid.uuid4().hex
    safe_title = sanitize_filename(req.title) or "document"
    file_name_on_disk = f"{safe_title}_{file_id}.docx"
    file_path = os.path.join(OUTPUT_DIR, file_name_on_disk)
    
    doc.save(file_path)
    return file_id, file_name_on_disk, file_path

# ======================================================
# ENDPOINTS
# ======================================================

@app.get("/health")
def health():
    # Comprobación de escritura en disco para debug
    try:
        test_file = os.path.join(OUTPUT_DIR, "write_test.txt")
        with open(test_file, "w") as f:
            f.write("ok")
        write_status = "writable"
    except Exception as e:
        write_status = f"error: {str(e)}"
        
    return {
        "status": "ok", 
        "version": "1.4.0-debug", 
        "output_dir": OUTPUT_DIR,
        "disk_status": write_status
    }

@app.post("/v1/documents", response_model=CreateDocumentResponse)
def create_document(req: CreateDocumentRequest, authorization: Optional[str] = Header(None)):
    verify_bearer_token(authorization)
    
    try:
        file_id, file_name, file_path = build_docx(req)
        
        base_url = PUBLIC_BASE_URL if PUBLIC_BASE_URL else "https://docgen-api-o3tq.onrender.com"
        download_url = f"{base_url}/v1/download/{file_id}"
        download_markdown = f"[Descargar DOCX]({download_url})"

        return CreateDocumentResponse(
            file_id=file_id,
            file_name=file_name,
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            download_url=download_url,
            download_markdown=download_markdown
        )
    except Exception as e:
        # Devolver error visible en lugar de 500 generico
        raise HTTPException(status_code=500, detail=f"Error generating doc: {str(e)}")

@app.get("/v1/download/{file_id}")
def download_document(file_id: str):
    try:
        # Búsqueda segura
        pattern = os.path.join(OUTPUT_DIR, f"*_{file_id}.docx")
        matches = glob(pattern)
        
        # Fallback: buscar solo por ID si el patrón complejo falla
        if not matches:
             matches = glob(os.path.join(OUTPUT_DIR, f"{file_id}.docx"))

        if not matches:
            # Listar archivos disponibles para debug (solo veras esto si falla)
            available = os.listdir(OUTPUT_DIR)
            raise HTTPException(status_code=404, detail=f"File not found. ID: {file_id}. Available in {OUTPUT_DIR}: {len(available)} files.")
        
        file_path = matches
        return FileResponse(
            path=file_path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=os.path.basename(file_path)
        )
    except HTTPException as he:
        raise he
    except Exception as e:
        # Capturamos el error 500 y te decimos qué es
        return JSONResponse(
            status_code=500, 
            content={"error": "Internal Server Error", "details": str(e), "type": type(e).__name__}
        )
