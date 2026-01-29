import os
import re
import uuid
from datetime import datetime
from typing import Optional, List, Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from docx import Document
from docx.shared import Pt

# ======================================================
# CONFIGURACIÓN
# ======================================================
API_BEARER_TOKEN = os.getenv("DOCGEN_API_TOKEN", "sk-docgen-change-me")
# Usamos /tmp porque es el único sitio seguro para escribir en Render
OUTPUT_DIR = "/tmp"
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = FastAPI(title="Document Generator API (Binary)", version="2.0.0")

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
    body_markdown: str = Field(..., description="Contenido principal")
    sections: Optional[List[Section]] = None

class CreateDocumentRequest(BaseModel):
    document_type: DocumentType
    format: FormatType
    language: str
    title: str
    candidate: Optional[Candidate] = None
    content: Content

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

def build_docx(req: CreateDocumentRequest) -> str:
    doc = Document()
    doc.add_heading(req.title, level=1)
    
    # Metadata
    meta = []
    if req.candidate and req.candidate.full_name:
        meta.append(req.candidate.full_name)
    meta.append(datetime.now().strftime("%Y-%m-%d"))
    p = doc.add_paragraph(" | ".join(meta))
    p.italic = True
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

    # Guardado temporal antes de enviar
    safe_title = sanitize_filename(req.title)
    file_name = f"{safe_title}.docx"
    file_path = os.path.join(OUTPUT_DIR, file_name)
    doc.save(file_path)
    
    return file_path, file_name

# ======================================================
# ENDPOINT PRINCIPAL (Direct Response)
# ======================================================
@app.get("/health")
def health():
    return {"status": "ok", "mode": "binary_response"}

@app.post("/v1/documents")
def create_document(req: CreateDocumentRequest, authorization: Optional[str] = Header(None)):
    verify_bearer_token(authorization)
    
    # Generamos el archivo en /tmp
    file_path, file_name = build_docx(req)
    
    # Devolvemos el archivo directamente (Binary Stream)
    return FileResponse(
        path=file_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=file_name
    )
