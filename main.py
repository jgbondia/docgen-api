import base64
import os
import re
import uuid
from datetime import datetime
from typing import Optional, List, Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from docx import Document
from docx.shared import Pt


# ======================================================
# CONFIGURACIÓN
# ======================================================

# Debe coincidir con la variable de entorno en Render: DOCGEN_API_TOKEN
API_BEARER_TOKEN = os.getenv("DOCGEN_API_TOKEN", "sk-docgen-change-me")

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# >>> IMPORTANTE: esta variable DEBE llamarse "app" <<<
app = FastAPI(
    title="Document Generator API",
    version="1.0.0",
    description="Generates DOCX documents for CVs and cover letters"
)


# ======================================================
# MODELOS (Payload)
# ======================================================

DocumentType = Literal["cover_letter", "cv", "recruiter_scorecard"]
FormatType = Literal["docx"]  # PDF se puede añadir después


class Candidate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None


class Section(BaseModel):
    heading: Optional[str] = None
    text: str


class Content(BaseModel):
    body_markdown: str = Field(..., description="Contenido principal en texto o markdown simple")
    sections: Optional[List[Section]] = None


class CreateDocumentRequest(BaseModel):
    document_type: DocumentType
    format: FormatType
    language: str
    title: str
    candidate: Optional[Candidate] = None
    content: Content


class CreateDocumentResponse(BaseModel):
    file_name: str
    content_type: str
    file_base64: str


# ======================================================
# AUTENTICACIÓN
# ======================================================

def verify_bearer_token(authorization: Optional[str]) -> None:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    expected = f"Bearer {API_BEARER_TOKEN}"
    if authorization.strip() != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ======================================================
# UTILIDADES DOCX
# ======================================================

def sanitize_filename(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[^\w\-\. ]+", "", name, flags=re.UNICODE)
    name = re.sub(r"\s+", "_", name)
    return name[:120]


def add_text_with_basic_markdown(doc: Document, text: str) -> None:
    """
    Soporta markdown básico:
    - '# '  → Heading 1
    - '## ' → Heading 2
    - '- '  → Bullet list
    """
    lines = text.replace("\r\n", "\n").split("\n")
    bullet_buffer: List[str] = []

    def flush_bullets():
        nonlocal bullet_buffer
        for bullet in bullet_buffer:
            p = doc.add_paragraph(bullet, style="List Bullet")
            for run in p.runs:
                run.font.size = Pt(11)
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

        if line.startswith("- "):
            bullet_buffer.append(line[2:])
            continue

        flush_bullets()
        p = doc.add_paragraph(line)
        for run in p.runs:
            run.font.size = Pt(11)

    flush_bullets()


def build_docx(req: CreateDocumentRequest) -> str:
    doc = Document()

    # Título
    doc.add_heading(req.title, level=1)

    # Meta info
    meta = []
    if req.candidate and req.candidate.full_name:
        meta.append(req.candidate.full_name)
    meta.append(datetime.now().strftime("%Y-%m-%d"))
    meta.append(f"Language: {req.language}")

    p = doc.add_paragraph(" | ".join(meta))
    for run in p.runs:
        run.font.size = Pt(9)

    doc.add_paragraph("")

    # Contenido principal
    add_text_with_basic_markdown(doc, req.content.body_markdown)

    # Secciones adicionales
    if req.content.sections:
        doc.add_page_break()
        doc.add_heading("Additional Sections", level=2)
        for section in req.content.sections:
            if section.heading:
                doc.add_heading(section.heading, level=3)
            add_text_with_basic_markdown(doc, section.text)

    # Guardar archivo
    safe_title = sanitize_filename(req.title)
    file_id = uuid.uuid4().hex[:8]
    file_name = f"{safe_title}_{file_id}.docx"
    file_path = os.path.join(OUTPUT_DIR, file_name)

    doc.save(file_path)
    return file_path


# ======================================================
# ENDPOINT PRINCIPAL
# ======================================================

@app.post("/v1/documents", response_model=CreateDocumentResponse)
def create_document(
    req: CreateDocumentRequest,
    authorization: Optional[str] = Header(None)
):
    verify_bearer_token(authorization)

    file_path = build_docx(req)

    with open(file_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")

    return CreateDocumentResponse(
        file_name=os.path.basename(file_path),
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_base64=encoded
    )


# ======================================================
# HEALTH CHECK
# ======================================================

@app.get("/health")
def health():
    return {"status": "ok"}


