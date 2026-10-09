from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .db import create_tables, get_db
from .models import AnswerRecord, Course, EvidenceLink, ExtractedItem, Formula, IngestionJob, Material, MaterialChunk, SourceDocument, Topic, ValidationResultRecord
from .schemas import AskRequest, CourseOut, EvidenceItem, ExtractedItemOut, IngestionJobOut, MaterialOut, MetricsOut, ReviewItemRequest, SearchRequest, SearchResponse, TopicOut, ValidateRequest, ValidateResponse, ValidationResultOut
from .services.agent import ValidationAgent
from .services.answering import LLMAnswerer, build_local_answer
from .services.ingestion.assist import AssistUnavailable, assist_extracted_item
from .services.ingestion.service import OCRUnavailable, create_ingestion_job, list_job_items, ocr_item, publish_job, save_upload
from .services.retrieval import evidence_dict, search_evidence
from .services.web_search import WebSearchProvider


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_tables()
    yield


app = FastAPI(title="多模态知识一致性校验与可追溯问答系统", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])


def _course_out(db: Session, course: Course) -> CourseOut:
    return CourseOut(id=course.id, name=course.name, description=course.description, topic_count=db.scalar(select(func.count(Topic.id)).where(Topic.course_id == course.id)) or 0, material_count=db.scalar(select(func.count(Material.id)).where(Material.course_id == course.id)) or 0)


def _job_out(db: Session, job: IngestionJob) -> IngestionJobOut:
    return IngestionJobOut(id=job.id, source_document_id=job.source_document_id, status=job.status, processed_pages=job.processed_pages, total_pages=job.total_pages, summary=job.summary or {}, items=[ExtractedItemOut.model_validate(item) for item in list_job_items(db, job.id)])


@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok", "database": "ok", "llm_provider": settings.llm_provider, "llm_configured": bool(settings.llm_base_url and settings.llm_api_key), "web_search_enabled": settings.web_search_enabled}


@app.get("/api/courses", response_model=list[CourseOut])
def courses(db: Session = Depends(get_db)):
    return [_course_out(db, course) for course in db.scalars(select(Course).order_by(Course.id))]


@app.get("/api/topics", response_model=list[TopicOut])
def topics(course_id: int | None = None, db: Session = Depends(get_db)):
    statement = select(Topic).order_by(Topic.course_id, Topic.id)
    if course_id is not None:
        statement = statement.where(Topic.course_id == course_id)
    return list(db.scalars(statement))


@app.get("/api/materials", response_model=list[MaterialOut])
def materials(course_id: int | None = None, topic_id: int | None = None, material_type: str | None = None, limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(get_db)):
    statement = select(Material).order_by(Material.id.desc()).limit(limit)
    if course_id is not None:
        statement = statement.where(Material.course_id == course_id)
    if topic_id is not None:
        statement = statement.where(Material.topic_id == topic_id)
    if material_type is not None:
        statement = statement.where(Material.material_type == material_type)
    return list(db.scalars(statement))


@app.post("/api/search", response_model=SearchResponse)
def search(request: SearchRequest, db: Session = Depends(get_db)):
    results = search_evidence(db, request.query, request.course_id, request.topic_id, request.material_types, limit=request.limit)
    return SearchResponse(query=request.query, results=[EvidenceItem(**evidence_dict(item)) for item in results], has_evidence=bool(results))


@app.post("/api/answers/ask", response_model=ValidateResponse)
async def ask_answer(request: AskRequest, db: Session = Depends(get_db)):
    evidence = search_evidence(db, request.question, request.course_id, request.topic_id, limit=10)
    evidence_payload = [evidence_dict(item) for item in evidence]
    web_results = await WebSearchProvider().search(request.question) if request.use_web else []
    model_draft = await LLMAnswerer().complete(request.question, evidence_payload, web_results)
    draft = model_draft or build_local_answer(request.question, evidence_payload)
    answer = str(draft.get("answer", "")).strip() or "当前没有生成可校验的回答。"
    evidence_ids = {item.material_id for item in evidence}
    requested_ids = [item for item in draft.get("evidence_ids", []) if item in evidence_ids]
    evidence, results, confidence, traceability, agent_trace = ValidationAgent(db).run(request.question, answer, request.course_id, request.topic_id, requested_ids or None)
    agent_trace = [{"step": "answer_generation", "status": "completed", "provider": draft.get("provider", settings.llm_provider), "evidence_ids": sorted(evidence_ids), "web_results": len(web_results)}] + agent_trace
    record = AnswerRecord(question=request.question, answer=answer, course_id=request.course_id, topic_id=request.topic_id, result_json={"confidence": confidence, "traceability": traceability, "agent_trace": agent_trace})
    db.add(record)
    db.flush()
    for result in results:
        db.add(ValidationResultRecord(answer_id=record.id, category=result.category, status=result.status, message=result.message, claim=result.claim, evidence_ids=result.evidence_ids, details=result.details))
        for material_id in result.evidence_ids:
            item = next((candidate for candidate in evidence if candidate.material_id == material_id), None)
            if item:
                db.add(EvidenceLink(answer_id=record.id, claim=result.claim, material_id=item.material_id, chunk_id=item.chunk_id, relation="supports" if result.status == "pass" else "checks"))
    db.commit()
    return ValidateResponse(answer_id=record.id, question=request.question, answer=answer, checks=[ValidationResultOut(**result.as_dict()) for result in results], evidence=[EvidenceItem(**evidence_dict(item)) for item in evidence], confidence=confidence, risk_flags=confidence["reasons"], traceability=traceability, agent_trace=agent_trace)


@app.post("/api/answers/validate", response_model=ValidateResponse)
def validate_answer(request: ValidateRequest, db: Session = Depends(get_db)):
    if request.material_ids:
        found = set(db.scalars(select(Material.id).where(Material.id.in_(request.material_ids))))
        missing = sorted(set(request.material_ids) - found)
        if missing:
            raise HTTPException(status_code=400, detail={"code": "unknown_material_ids", "ids": missing})
    evidence, results, confidence, traceability, agent_trace = ValidationAgent(db).run(request.question, request.answer, request.course_id, request.topic_id, request.material_ids)
    record = AnswerRecord(question=request.question, answer=request.answer, course_id=request.course_id, topic_id=request.topic_id, result_json={"confidence": confidence, "traceability": traceability, "agent_trace": agent_trace})
    db.add(record)
    db.flush()
    for result in results:
        db.add(ValidationResultRecord(answer_id=record.id, category=result.category, status=result.status, message=result.message, claim=result.claim, evidence_ids=result.evidence_ids, details=result.details))
        for material_id in result.evidence_ids:
            item = next((candidate for candidate in evidence if candidate.material_id == material_id), None)
            if item:
                db.add(EvidenceLink(answer_id=record.id, claim=result.claim, material_id=item.material_id, chunk_id=item.chunk_id, relation="supports" if result.status == "pass" else "checks"))
    db.commit()
    risk_flags = confidence["reasons"] if confidence["level"] != "high" else []
    return ValidateResponse(answer_id=record.id, question=request.question, answer=request.answer, checks=[ValidationResultOut(**result.as_dict()) for result in results], evidence=[EvidenceItem(**evidence_dict(item)) for item in evidence], confidence=confidence, risk_flags=risk_flags, traceability=traceability, agent_trace=agent_trace)


@app.get("/api/answers/{answer_id}", response_model=ValidateResponse)
def get_answer(answer_id: int, db: Session = Depends(get_db)):
    record = db.get(AnswerRecord, answer_id)
    if not record:
        raise HTTPException(status_code=404, detail="答案记录不存在")
    results = list(db.scalars(select(ValidationResultRecord).where(ValidationResultRecord.answer_id == answer_id).order_by(ValidationResultRecord.id)))
    material_ids = sorted({material_id for result in results for material_id in (result.evidence_ids or [])})
    evidence = []
    if material_ids:
        evidence = search_evidence(db, record.question, record.course_id, record.topic_id, material_ids=material_ids, limit=50)
    confidence = (record.result_json or {}).get("confidence", {"level": "low", "score": 0, "reasons": ["记录缺少可信度数据"]})
    traceability = (record.result_json or {}).get("traceability", {"status": "no_evidence", "message": "无可定位依据", "links": []})
    agent_trace = (record.result_json or {}).get("agent_trace", [])
    return ValidateResponse(answer_id=record.id, question=record.question, answer=record.answer, checks=[ValidationResultOut(category=item.category, status=item.status, message=item.message, claim=item.claim, evidence_ids=item.evidence_ids or [], details=item.details or {}) for item in results], evidence=[EvidenceItem(**evidence_dict(item)) for item in evidence], confidence=confidence, risk_flags=confidence.get("reasons", []), traceability=traceability, agent_trace=agent_trace)


@app.get("/api/metrics", response_model=MetricsOut)
def metrics(db: Session = Depends(get_db)):
    from backend.evaluation.runner import load_cases, run_cases
    courses_count = db.scalar(select(func.count(Course.id))) or 0
    topics_count = db.scalar(select(func.count(Topic.id))) or 0
    materials_count = db.scalar(select(func.count(Material.id))) or 0
    verified_count = db.scalar(select(func.count(Material.id)).where(Material.verified.is_(True))) or 0
    cases = load_cases()
    report = run_cases(db, cases) if cases else None
    return MetricsOut(course_count=courses_count, topic_count=topics_count, material_count=materials_count, verified_material_count=verified_count, evaluation_case_count=len(cases), evaluation_run=bool(report), traceability_rate=report["traceability_rate"] if report else 0.0, conflict_accuracy=report["conflict_accuracy"] if report else None, false_positive_rate=report["false_positive_rate"] if report else None, category_stats=report["category_stats"] if report else {})


@app.post("/api/ingestion/upload", response_model=IngestionJobOut)
async def upload_textbook(file: UploadFile = File(...), course_id: int | None = None, db: Session = Depends(get_db)):
    if not file.filename:
        raise HTTPException(status_code=422, detail="缺少文件名")
    content = await file.read()
    if len(content) > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件超过100MB限制")
    try:
        path = save_upload(file.filename, content)
        job = create_ingestion_job(db, path, course_id)
        return _job_out(db, job)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/ingestion/jobs/{job_id}", response_model=IngestionJobOut)
def get_ingestion_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(IngestionJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="导入任务不存在")
    return _job_out(db, job)


@app.patch("/api/ingestion/items/{item_id}", response_model=ExtractedItemOut)
def review_item(item_id: int, request: ReviewItemRequest, db: Session = Depends(get_db)):
    item = db.get(ExtractedItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="解析项不存在")
    if request.normalized_text is not None:
        item.normalized_text = request.normalized_text
    if request.payload is not None:
        item.payload = request.payload
    item.review_status = request.review_status
    if request.needs_review is not None:
        item.needs_review = request.needs_review
    db.commit()
    db.refresh(item)
    return ExtractedItemOut.model_validate(item)

@app.post("/api/ingestion/items/{item_id}/ocr", response_model=ExtractedItemOut)
def run_item_ocr(item_id: int, db: Session = Depends(get_db)):
    try:
        return ExtractedItemOut.model_validate(ocr_item(db, item_id))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OCRUnavailable, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/ingestion/items/{item_id}/assist", response_model=ExtractedItemOut)
def assist_ingestion_item(item_id: int, mode: str = Query(default="auto"), db: Session = Depends(get_db)):
    try:
        return ExtractedItemOut.model_validate(assist_extracted_item(db, item_id, mode=mode))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AssistUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/ingestion/jobs/{job_id}/publish")
def publish_ingestion(job_id: int, course_id: int, db: Session = Depends(get_db)):
    if not db.get(Course, course_id):
        raise HTTPException(status_code=404, detail="课程不存在")
    try:
        return publish_job(db, job_id, course_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
