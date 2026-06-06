"""FastAPI 服务 — 将 recognize / judge / direct 流水线封装为 HTTP 接口.

启动:
    uv run ai-scoring-api                 # 或
    uv run uvicorn src.api.server:app --reload

接口:
    GET  /api/health            健康检查 + 当前模型配置
    POST /api/recognize         上传图片 → VLM 转录 (TranscriptionResult)
    POST /api/rubric            上传 .tex → 解析评分标准 (ScoringRubric)
    POST /api/judge             上传学生 .md + 标准 .tex → 评分 (JudgingResult)
    POST /api/direct            上传图片 + 标准 .tex → 一步评分 (JudgingResult)
    POST /api/direct/batch      上传多张图片 + 标准 .tex → 批量评分 (BatchSummary)
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from src.log import setup_logging

logger = logging.getLogger(__name__)

_ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".pdf"}


# ─────────────────────────────────────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────────────────────────────────────


def _suffix_of(filename: str | None, fallback: str) -> str:
    if not filename:
        return fallback
    suffix = Path(filename).suffix.lower()
    return suffix or fallback


def _save_upload(tmp_dir: Path, upload: UploadFile, content: bytes) -> Path:
    """把上传文件落盘到临时目录，返回路径。"""
    name = Path(upload.filename or "upload").name
    dest = tmp_dir / name
    # 避免重名覆盖
    counter = 1
    while dest.exists():
        dest = tmp_dir / f"{dest.stem}_{counter}{dest.suffix}"
        counter += 1
    dest.write_bytes(content)
    return dest


def _transcription_payload(result, warnings: list[str]) -> dict:
    data = asdict(result)
    data["warnings"] = warnings
    return data


# ─────────────────────────────────────────────────────────────────────────────
# 应用工厂
# ─────────────────────────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    setup_logging(1)
    app = FastAPI(
        title="CPHOS AI 自动阅卷系统 API",
        description="基于 VLM/LLM 的物理竞赛手写答卷自动评分服务",
        version="0.1.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── 健康检查 ──────────────────────────────────────────────────────────
    @app.get("/api/health")
    def health() -> dict:
        from src.config.settings import get_settings

        info: dict[str, object] = {"status": "ok"}
        models: dict[str, str] = {}
        for profile in ("default", "judge", "direct"):
            try:
                models[profile] = get_settings(profile).model
            except Exception as exc:  # 缺少配置时不致命
                models[profile] = f"<unconfigured: {exc}>"
        info["models"] = models
        return info

    # ── Recognize ─────────────────────────────────────────────────────────
    @app.post("/api/recognize")
    async def recognize(file: UploadFile = File(...)) -> dict:
        from src.recognize.service import run_transcription
        from src.recognize.validation import validate_transcription

        suffix = _suffix_of(file.filename, ".png")
        if suffix not in _ALLOWED_IMAGE_SUFFIXES:
            raise HTTPException(400, f"Unsupported file type: {suffix}")

        content = await file.read()
        with tempfile.TemporaryDirectory(prefix="recognize_") as tmp:
            path = _save_upload(Path(tmp), file, content)
            try:
                result = run_transcription([path])
            except Exception as exc:
                logger.exception("recognize failed")
                raise HTTPException(500, str(exc)) from exc
            warnings = validate_transcription(result.transcription)
        return _transcription_payload(result, warnings)

    # ── Rubric 解析 ───────────────────────────────────────────────────────
    @app.post("/api/rubric")
    async def rubric(standard: UploadFile = File(...)) -> dict:
        from src.judge.answer_parser import parse_scoring_rubric

        content = await standard.read()
        with tempfile.TemporaryDirectory(prefix="rubric_") as tmp:
            path = _save_upload(Path(tmp), standard, content)
            try:
                parsed = parse_scoring_rubric(path)
            except Exception as exc:
                logger.exception("rubric parse failed")
                raise HTTPException(500, str(exc)) from exc
        return asdict(parsed)

    # ── Judge（文本评分）─────────────────────────────────────────────────
    @app.post("/api/judge")
    async def judge(
        student: UploadFile = File(...),
        standard: UploadFile = File(...),
        verbosity: int = Form(1),
        strictness: int = Form(1),
    ) -> dict:
        from src.judge.service import run_judging

        student_bytes = await student.read()
        standard_bytes = await standard.read()
        with tempfile.TemporaryDirectory(prefix="judge_") as tmp:
            tmp_dir = Path(tmp)
            student_path = _save_upload(tmp_dir, student, student_bytes)
            standard_path = _save_upload(tmp_dir, standard, standard_bytes)
            try:
                result = run_judging(
                    student_path, standard_path,
                    verbosity=verbosity, strictness=strictness,
                )
            except Exception as exc:
                logger.exception("judge failed")
                raise HTTPException(500, str(exc)) from exc
        return asdict(result)

    # ── Direct（直接评分）───────────────────────────────────────────────
    @app.post("/api/direct")
    async def direct(
        file: UploadFile = File(...),
        standard: UploadFile = File(...),
        verbosity: int = Form(1),
        strictness: int = Form(1),
    ) -> dict:
        from src.judge.service import run_direct_judging

        suffix = _suffix_of(file.filename, ".png")
        if suffix not in _ALLOWED_IMAGE_SUFFIXES:
            raise HTTPException(400, f"Unsupported file type: {suffix}")

        image_bytes = await file.read()
        standard_bytes = await standard.read()
        with tempfile.TemporaryDirectory(prefix="direct_") as tmp:
            tmp_dir = Path(tmp)
            image_path = _save_upload(tmp_dir, file, image_bytes)
            standard_path = _save_upload(tmp_dir, standard, standard_bytes)
            try:
                result = run_direct_judging(
                    [image_path], standard_path,
                    verbosity=verbosity, strictness=strictness,
                )
            except Exception as exc:
                logger.exception("direct failed")
                raise HTTPException(500, str(exc)) from exc
        return asdict(result)

    # ── Direct 批量 ──────────────────────────────────────────────────────
    @app.post("/api/direct/batch")
    async def direct_batch(
        files: list[UploadFile] = File(...),
        standard: UploadFile = File(...),
        verbosity: int = Form(1),
        strictness: int = Form(1),
        concurrency: int = Form(1),
    ) -> dict:
        from src.judge.answer_parser import parse_scoring_rubric
        from src.judge.service import run_direct_judging

        if not files:
            raise HTTPException(400, "No image files uploaded")

        standard_bytes = await standard.read()
        # 预读所有图片内容
        uploads: list[tuple[UploadFile, bytes]] = []
        for f in files:
            suffix = _suffix_of(f.filename, ".png")
            if suffix not in _ALLOWED_IMAGE_SUFFIXES:
                continue
            uploads.append((f, await f.read()))

        if not uploads:
            raise HTTPException(400, "No supported image files uploaded")

        with tempfile.TemporaryDirectory(prefix="batch_") as tmp:
            tmp_dir = Path(tmp)
            standard_path = _save_upload(tmp_dir, standard, standard_bytes)
            try:
                shared_rubric = parse_scoring_rubric(standard_path)
            except Exception as exc:
                logger.exception("rubric parse failed")
                raise HTTPException(500, str(exc)) from exc

            entries: list[dict] = []
            total_prompt = total_completion = total_total = 0
            result_count = failure_count = 0

            for upload, content in uploads:
                image_path = _save_upload(tmp_dir, upload, content)
                image_id = Path(upload.filename or image_path.name).name
                try:
                    result = run_direct_judging(
                        [image_path], standard_path,
                        verbosity=verbosity, strictness=strictness,
                        rubric=shared_rubric,
                    )
                    usage = result.usage or {}
                    total_prompt += int(usage.get("prompt_tokens", 0))
                    total_completion += int(usage.get("completion_tokens", 0))
                    total_total += int(usage.get("total_tokens", 0))
                    result_count += 1
                    entries.append({
                        "id": image_id,
                        "status": "done",
                        "score": result.total_score,
                        "max_score": result.max_score,
                        "duration_seconds": result.duration_seconds,
                        "usage": usage,
                        "output_file": f"{Path(image_id).stem}_direct.md",
                        "result": asdict(result),
                    })
                except Exception as exc:
                    failure_count += 1
                    entries.append({
                        "id": image_id,
                        "status": "failed",
                        "score": 0,
                        "max_score": shared_rubric.total_score,
                        "duration_seconds": 0,
                        "output_file": "",
                        "error": str(exc),
                    })

        return {
            "mode": "direct_vlm",
            "source_directory": "<uploaded>",
            "standard_answer": Path(standard.filename or "answer.tex").name,
            "result_count": result_count,
            "failure_count": failure_count,
            "entries": entries,
            "total_usage": {
                "prompt_tokens": total_prompt,
                "completion_tokens": total_completion,
                "total_tokens": total_total,
            },
        }

    # ── Exam 数据浏览 + 阅卷 ──────────────────────────────────────────────
    from .exam import router as exam_router

    app.include_router(exam_router)

    return app


app = create_app()


def main() -> int:
    """`ai-scoring-api` 控制台脚本入口。"""
    import os

    import uvicorn

    host = os.getenv("API_HOST", "127.0.0.1")
    port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run("src.api.server:app", host=host, port=port, reload=False)
    return 0
