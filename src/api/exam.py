"""Exam 数据浏览与阅卷 — 基于 data/ 目录的真实考试数据.

数据结构（约定）::

    data/
    └── <exam_id>/                       例如 exam1
        ├── paper/                        标准答案，按"覆盖的题号"分目录
        │   ├── 1/penning_trap.tex        第1题
        │   ├── 23/orbit.tex              第2、3题（一道物理题，两张答卷图）
        │   ├── 4/laser_cooling.tex       第4题
        │   └── ...
        ├── student_answers/              学生答卷，目录名 = 序号_总分_姓名
        │   ├── 1_214.5_刘天雨/
        │   │   ├── 第1题.jpg
        │   │   └── ...
        │   └── ...
        └── grading_results/              评分结果输出（自动生成）

paper 文件夹名中的每个数字对应一张学生答卷图 ``第N题.jpg``；如 ``23`` 表示
该物理题由 ``第2题.jpg`` 与 ``第3题.jpg`` 两张图共同作答。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/exams", tags=["exam"])

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".pdf"}
_PROBLEM_HEAD_RE = re.compile(r"\\begin\{problem\}\s*\[(\d+)\]\s*\{([^}]*)\}", re.DOTALL)
_STUDENT_DIR_RE = re.compile(r"^(?P<rank>\d+)_(?P<score>[\d.]+)_(?P<name>.+)$")


# ─────────────────────────────────────────────────────────────────────────────
# 路径与数据根
# ─────────────────────────────────────────────────────────────────────────────


def _data_root() -> Path:
    """data 目录根；可用环境变量 DATA_ROOT 覆盖。"""
    env = os.getenv("DATA_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    # src/api/exam.py → parents[2] = 仓库根
    return (Path(__file__).resolve().parents[2] / "data").resolve()


def _safe_child(parent: Path, name: str) -> Path:
    """在 *parent* 下按名称取子路径，防止路径穿越。"""
    candidate = (parent / name).resolve()
    if parent.resolve() not in candidate.parents and candidate != parent.resolve():
        raise HTTPException(400, f"Illegal path component: {name}")
    if not candidate.exists():
        raise HTTPException(404, f"Not found: {name}")
    return candidate


# ─────────────────────────────────────────────────────────────────────────────
# 解析辅助
# ─────────────────────────────────────────────────────────────────────────────


def _paper_tex(paper_dir: Path) -> Path | None:
    texs = sorted(paper_dir.glob("*.tex"))
    return texs[0] if texs else None


def _parse_paper_head(tex_path: Path) -> tuple[int, str]:
    """轻量提取 (满分, 标题)，失败时回退默认值。"""
    try:
        text = tex_path.read_text(encoding="utf-8")
    except Exception:
        return 0, tex_path.stem
    m = _PROBLEM_HEAD_RE.search(text)
    if not m:
        return 0, tex_path.stem
    return int(m.group(1)), m.group(2).strip()


def _problem_indices(paper_id: str) -> list[int]:
    """paper 文件夹名 → 覆盖的题号列表，如 '23' → [2, 3]。"""
    return [int(ch) for ch in paper_id if ch.isdigit()]


def _image_names(paper_id: str) -> list[str]:
    return [f"第{i}题.jpg" for i in _problem_indices(paper_id)]


def _list_papers(exam_dir: Path) -> list[dict]:
    paper_root = exam_dir / "paper"
    if not paper_root.is_dir():
        return []
    papers: list[dict] = []
    for sub in sorted(paper_root.iterdir(), key=lambda p: p.name):
        if not sub.is_dir():
            continue
        tex = _paper_tex(sub)
        if tex is None:
            continue
        max_score, title = _parse_paper_head(tex)
        papers.append({
            "id": sub.name,
            "title": title,
            "max_score": max_score,
            "tex_file": tex.name,
            "problem_indices": _problem_indices(sub.name),
            "image_names": _image_names(sub.name),
        })
    # 按首个题号排序（"1" < "23" < "4" ...）
    papers.sort(key=lambda p: p["problem_indices"][0] if p["problem_indices"] else 99)
    return papers


def _parse_student_dir(name: str) -> dict:
    m = _STUDENT_DIR_RE.match(name)
    if not m:
        return {"id": name, "rank": None, "name": name}
    return {
        "id": name,
        "rank": int(m.group("rank")),
        "name": m.group("name"),
    }


def _list_students(exam_dir: Path) -> list[dict]:
    stu_root = exam_dir / "student_answers"
    if not stu_root.is_dir():
        return []
    students: list[dict] = []
    for sub in sorted(stu_root.iterdir(), key=lambda p: p.name):
        if not sub.is_dir():
            continue
        info = _parse_student_dir(sub.name)
        info["available_images"] = sorted(
            p.name for p in sub.iterdir()
            if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES
        )
        students.append(info)
    students.sort(key=lambda s: (s["rank"] is None, s["rank"] or 0))
    return students


def _resolve_grade_inputs(
    exam_id: str, student_id: str, paper_id: str,
) -> tuple[Path, list[Path], Path]:
    """返回 (exam_dir, 学生答卷图片列表, 标准答案 tex)。"""
    data_root = _data_root()
    if not data_root.is_dir():
        raise HTTPException(404, f"Data root not found: {data_root}")
    exam_dir = _safe_child(data_root, exam_id)
    student_dir = _safe_child(exam_dir / "student_answers", student_id)
    paper_dir = _safe_child(exam_dir / "paper", paper_id)

    tex = _paper_tex(paper_dir)
    if tex is None:
        raise HTTPException(404, f"No .tex found in paper {paper_id}")

    images: list[Path] = []
    for name in _image_names(paper_id):
        img = student_dir / name
        if img.exists():
            images.append(img)
    if not images:
        raise HTTPException(
            404,
            f"学生 {student_id} 缺少第 {_problem_indices(paper_id)} 题答卷图片",
        )
    return exam_dir, images, tex


# ─────────────────────────────────────────────────────────────────────────────
# 浏览接口
# ─────────────────────────────────────────────────────────────────────────────


@router.get("")
def list_exams() -> dict:
    data_root = _data_root()
    if not data_root.is_dir():
        return {"data_root": str(data_root), "exams": []}
    exams: list[dict] = []
    for sub in sorted(data_root.iterdir(), key=lambda p: p.name):
        if not sub.is_dir():
            continue
        papers = _list_papers(sub)
        students = _list_students(sub)
        exams.append({
            "id": sub.name,
            "paper_count": len(papers),
            "student_count": len(students),
        })
    return {"data_root": str(data_root), "exams": exams}


@router.get("/{exam_id}")
def get_exam(exam_id: str) -> dict:
    data_root = _data_root()
    exam_dir = _safe_child(data_root, exam_id)
    return {
        "id": exam_id,
        "papers": _list_papers(exam_dir),
        "students": _list_students(exam_dir),
    }


@router.get("/{exam_id}/papers/{paper_id}/rubric")
def get_paper_rubric(exam_id: str, paper_id: str) -> dict:
    from src.judge.answer_parser import parse_scoring_rubric

    data_root = _data_root()
    exam_dir = _safe_child(data_root, exam_id)
    paper_dir = _safe_child(exam_dir / "paper", paper_id)
    tex = _paper_tex(paper_dir)
    if tex is None:
        raise HTTPException(404, f"No .tex found in paper {paper_id}")
    try:
        rubric = parse_scoring_rubric(tex)
    except Exception as exc:
        logger.exception("rubric parse failed")
        raise HTTPException(500, str(exc)) from exc
    return asdict(rubric)


@router.get("/{exam_id}/students/{student_id}/images/{image_name}")
def get_student_image(exam_id: str, student_id: str, image_name: str) -> FileResponse:
    data_root = _data_root()
    exam_dir = _safe_child(data_root, exam_id)
    student_dir = _safe_child(exam_dir / "student_answers", student_id)
    image_path = _safe_child(student_dir, image_name)
    if not image_path.is_file():
        raise HTTPException(404, f"Not an image file: {image_name}")
    return FileResponse(image_path)


# ─────────────────────────────────────────────────────────────────────────────
# 阅卷接口
# ─────────────────────────────────────────────────────────────────────────────


def _grade_one(
    exam_dir: Path, student_id: str, paper_id: str,
    images: list[Path], tex: Path,
    verbosity: int, strictness: int, rubric=None,
) -> dict:
    """对单个学生的单道题做 direct 评分，落盘并返回 JudgingResult dict。"""
    from src.judge.service import run_direct_judging

    out_dir = exam_dir / "grading_results" / student_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{paper_id}_direct.md"

    result = run_direct_judging(
        images, tex, output_path=out_path,
        verbosity=verbosity, strictness=strictness, rubric=rubric,
    )
    data = asdict(result)
    # 额外保存完整结果 JSON，便于前端重新加载已评结果
    (out_dir / f"{paper_id}_result.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return data


def _load_saved_result(exam_dir: Path, student_id: str, paper_id: str) -> dict | None:
    """读取已保存的单题评分结果（若存在）。"""
    json_path = exam_dir / "grading_results" / student_id / f"{paper_id}_result.json"
    if not json_path.is_file():
        return None
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("无法解析已保存结果: %s", json_path)
        return None


def _all_saved_results(exam_dir: Path) -> dict[str, dict[str, dict]]:
    """扫描 grading_results，返回 {student_id: {paper_id: result}}。"""
    root = exam_dir / "grading_results"
    out: dict[str, dict[str, dict]] = {}
    if not root.is_dir():
        return out
    for student_dir in root.iterdir():
        if not student_dir.is_dir():
            continue
        per_paper: dict[str, dict] = {}
        for jf in student_dir.glob("*_result.json"):
            paper_id = jf.name[: -len("_result.json")]
            try:
                per_paper[paper_id] = json.loads(jf.read_text(encoding="utf-8"))
            except Exception:
                logger.warning("无法解析已保存结果: %s", jf)
        if per_paper:
            out[student_dir.name] = per_paper
    return out


@router.post("/{exam_id}/grade")
def grade_one(
    exam_id: str,
    student_id: str = Body(...),
    paper_id: str = Body(...),
    verbosity: int = Body(1),
    strictness: int = Body(1),
) -> dict:
    exam_dir, images, tex = _resolve_grade_inputs(exam_id, student_id, paper_id)
    try:
        result = _grade_one(
            exam_dir, student_id, paper_id, images, tex, verbosity, strictness,
        )
    except Exception as exc:
        logger.exception("grade failed")
        raise HTTPException(500, str(exc)) from exc
    return {
        "exam_id": exam_id,
        "student_id": student_id,
        "paper_id": paper_id,
        "result": result,
    }


@router.post("/{exam_id}/grade/batch")
def grade_batch(
    exam_id: str,
    paper_id: str = Body(...),
    student_ids: list[str] | None = Body(None),
    verbosity: int = Body(1),
    strictness: int = Body(1),
) -> dict:
    """对一道题批量评分。未提供 student_ids 时评分全部学生。"""
    from src.judge.answer_parser import parse_scoring_rubric

    data_root = _data_root()
    exam_dir = _safe_child(data_root, exam_id)
    paper_dir = _safe_child(exam_dir / "paper", paper_id)
    tex = _paper_tex(paper_dir)
    if tex is None:
        raise HTTPException(404, f"No .tex found in paper {paper_id}")

    try:
        rubric = parse_scoring_rubric(tex)
    except Exception as exc:
        logger.exception("rubric parse failed")
        raise HTTPException(500, str(exc)) from exc

    all_students = {s["id"]: s for s in _list_students(exam_dir)}
    targets = student_ids or list(all_students.keys())

    entries: list[dict] = []
    result_count = failure_count = 0
    total_prompt = total_completion = total_total = 0

    for sid in targets:
        info = all_students.get(sid)
        student_dir = exam_dir / "student_answers" / sid
        images = [student_dir / n for n in _image_names(paper_id) if (student_dir / n).exists()]
        if not info or not images:
            failure_count += 1
            entries.append({
                "id": sid,
                "status": "failed",
                "score": 0,
                "max_score": rubric.total_score,
                "duration_seconds": 0,
                "output_file": "",
                "error": "缺少答卷图片" if info else "学生不存在",
            })
            continue
        try:
            result = _grade_one(
                exam_dir, sid, paper_id, images, tex, verbosity, strictness, rubric,
            )
            usage = result.get("usage") or {}
            total_prompt += int(usage.get("prompt_tokens", 0))
            total_completion += int(usage.get("completion_tokens", 0))
            total_total += int(usage.get("total_tokens", 0))
            result_count += 1
            entries.append({
                "id": sid,
                "name": info.get("name"),
                "rank": info.get("rank"),
                "status": "done",
                "score": result["total_score"],
                "max_score": result["max_score"],
                "duration_seconds": result["duration_seconds"],
                "usage": usage,
                "output_file": f"grading_results/{sid}/{paper_id}_direct.md",
                "result": result,
            })
        except Exception as exc:
            logger.exception("grade failed for %s", sid)
            failure_count += 1
            entries.append({
                "id": sid,
                "name": info.get("name"),
                "status": "failed",
                "score": 0,
                "max_score": rubric.total_score,
                "duration_seconds": 0,
                "output_file": "",
                "error": str(exc),
            })

    return {
        "mode": "direct_vlm",
        "exam_id": exam_id,
        "paper_id": paper_id,
        "paper_title": rubric.problem_title,
        "result_count": result_count,
        "failure_count": failure_count,
        "entries": entries,
        "total_usage": {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_total,
        },
    }


@router.get("/{exam_id}/results")
def get_saved_results(exam_id: str) -> dict:
    """返回已保存的全部评分结果，供前端恢复界面状态。

    形如 ``{student_id: {paper_id: JudgingResult}}``。
    """
    data_root = _data_root()
    exam_dir = _safe_child(data_root, exam_id)
    return {"exam_id": exam_id, "results": _all_saved_results(exam_dir)}


@router.post("/{exam_id}/grade/full")
def grade_full_paper(
    exam_id: str,
    student_id: str = Body(...),
    verbosity: int = Body(1),
    strictness: int = Body(1),
    skip_graded: bool = Body(True),
) -> dict:
    """对单个学生的整张试卷（所有题）评分，返回逐题结果与总分。

    *skip_graded* 为真时，已有结果的题目直接复用，不重复调用模型。
    """
    data_root = _data_root()
    exam_dir = _safe_child(data_root, exam_id)
    student_dir = _safe_child(exam_dir / "student_answers", student_id)
    papers = _list_papers(exam_dir)

    items: list[dict] = []
    total_score = total_max = 0
    total_prompt = total_completion = total_total = 0
    graded_count = failure_count = 0

    for paper in papers:
        paper_id = paper["id"]
        images = [student_dir / n for n in paper["image_names"] if (student_dir / n).exists()]
        if not images:
            failure_count += 1
            items.append({
                "paper_id": paper_id,
                "paper_title": paper["title"],
                "problem_indices": paper["problem_indices"],
                "status": "missing",
                "score": 0,
                "max_score": paper["max_score"],
                "error": "缺少答卷图片",
            })
            total_max += paper["max_score"]
            continue

        saved = _load_saved_result(exam_dir, student_id, paper_id) if skip_graded else None
        if saved is None:
            paper_dir = exam_dir / "paper" / paper_id
            tex = _paper_tex(paper_dir)
            if tex is None:
                failure_count += 1
                items.append({
                    "paper_id": paper_id,
                    "paper_title": paper["title"],
                    "problem_indices": paper["problem_indices"],
                    "status": "failed",
                    "score": 0,
                    "max_score": paper["max_score"],
                    "error": "缺少标准答案 .tex",
                })
                total_max += paper["max_score"]
                continue
            try:
                saved = _grade_one(
                    exam_dir, student_id, paper_id, images, tex, verbosity, strictness,
                )
            except Exception as exc:
                logger.exception("grade failed: %s / %s", student_id, paper_id)
                failure_count += 1
                items.append({
                    "paper_id": paper_id,
                    "paper_title": paper["title"],
                    "problem_indices": paper["problem_indices"],
                    "status": "failed",
                    "score": 0,
                    "max_score": paper["max_score"],
                    "error": str(exc),
                })
                total_max += paper["max_score"]
                continue

        usage = saved.get("usage") or {}
        total_prompt += int(usage.get("prompt_tokens", 0))
        total_completion += int(usage.get("completion_tokens", 0))
        total_total += int(usage.get("total_tokens", 0))
        total_score += int(saved.get("total_score", 0))
        total_max += int(saved.get("max_score", paper["max_score"]))
        graded_count += 1
        items.append({
            "paper_id": paper_id,
            "paper_title": paper["title"],
            "problem_indices": paper["problem_indices"],
            "status": "done",
            "score": saved.get("total_score", 0),
            "max_score": saved.get("max_score", paper["max_score"]),
            "result": saved,
        })

    return {
        "exam_id": exam_id,
        "student_id": student_id,
        "graded_count": graded_count,
        "failure_count": failure_count,
        "total_score": total_score,
        "total_max": total_max,
        "items": items,
        "total_usage": {
            "prompt_tokens": total_prompt,
            "completion_tokens": total_completion,
            "total_tokens": total_total,
        },
    }
