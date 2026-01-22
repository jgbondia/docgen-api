import base64
import os
import re
import uuid
import time
from datetime import datetime
from typing import Optional, List, Literal, Dict, Tuple

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from docx import Document
from docx.shared import Pt


# ======================================================
# CONFIG
# ======================================================

API_BEARER_TOKEN = os.getenv("DOCGEN_API_TOKEN", "sk-docgen-change-me")

# IMPORTANTE: usa /tmp en Render (siempre writable)
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/tmp/docgen_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
DOWNLOAD_TTL_SECONDS = int(os.getenv("DOWNLOAD_TTL_SECONDS", "86400"))

# registry en memoria (best-effort)
FILE_REGISTRY: Dict[str, Dict[str, object]] = {}

app = FastAPI(
    title="Document Generator API",
    version="1.2.0",
    description="Generates DOCX documents for CVs, cover letters and recruiter scorecards"
)

# ======================================================
# MODELS
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
    body_markdown: str = Field(..., description="Main content in text/markdown")
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
# AUTH
# ======================================================

def verify_bearer_token(authorization: Optional[str]) -> None:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    expected = f"Bearer {API_BEARER_TOKEN}"
    if authorization.strip() != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ======================================================
# UTILS
# ======================================================

def sanitize_filename(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[^\w\-\. ]+", "", name, flags=re.UNICODE)
    name = re.sub(r"\s+", "_", name)
    return name[:120]


def add_text_with_basic_markdown(doc: Document, text: str) -> None:
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


def cleanup_expired_files() -> None:
    """Borra ficheros DOCX expirados en OUTPUT_DIR para no acumular basura."""
    now = time.time()
    for p in glob(os.path.join(OUTPUT_DIR, "*.docx")):
        try:
            age = now - os.path.getmtime(p)
            if age > DOWNLOAD_TTL_SECONDS:
                os.remove(p)
        except Exception:
            pass


    # 2) limpieza best-effort del disco por mtime (si registry se perdió)
    try:
        for fn in os.listdir(OUTPUT_DIR):
            if not fn.endswith(".docx"):
                continue
            full = os.path.join(OUTPUT_DIR, fn)
            try:
                mtime = os.path.getmtime(full)
                if now - mtime > DOWNLOAD_TTL_SECONDS:
                    os.remove(full)
            except Exception:
                pass
    except Exception:
        pass


def require_public_base_url() -> str:
    return PUBLIC_BASE_URL


def build_docx(req: CreateDocumentRequest) -> Tuple[str, str, str]:
    doc = Document()
    doc.add_heading(req.title, level=1)

    meta = []
    if req.candidate and req.candidate.full_name:
        meta.append(req.candidate.full_name)
    meta.append(datetime.now().strftime("%Y-%m-%d"))
    meta.append(f"Language: {req.language}")

    p = doc.add_paragraph(" | ".join(meta))
    for run in p.runs:
        run.font.size = Pt(9)

    doc.add_paragraph("")
    add_text_with_basic_markdown(doc, req.content.body_markdown)

    if req.content.sections:
        doc.add_page_break()
        doc.add_heading("Additional Sections", level=2)
        for section in req.content.sections:
            if section.heading:
                doc.add_heading(section.heading, level=3)
            add_text_with_basic_markdown(doc, section.text)

    # file_id determinista
    file_id = uuid.uuid4().hex
    safe_title = sanitize_filename(req.title)
    file_name = f"{safe_title}_{file_id[:12]}.docx"

    # path determinista por file_id: permite descargar aunque el registry se pierda
    file_path = os.path.join(OUTPUT_DIR, f"{file_id}.docx")

    doc.save(file_path)
    return file_id, file_name, file_path


def resolve_file_path(file_id: str) -> Optional[str]:
    # 1) si está en registry
    meta = FILE_REGISTRY.get(file_id)
    if meta:
        p = str(meta["path"])
        if os.path.exists(p):
            return p

    # 2) path determinista
    deterministic = os.path.join(OUTPUT_DIR, f"{file_id}.docx")
    if os.path.exists(deterministic):
        return deterministic

    return None


# ======================================================
# ENDPOINTS
# ======================================================

@app.post("/v1/documents")
def create_document(req: CreateDocumentRequest, authorization: Optional[str] = Header(None)):
    verify_bearer_token(authorization)

    file_id, file_name, file_path = build_docx(req)

    return FileResponse(
        path=file_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=file_name
    )
