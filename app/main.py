"""FastAPI 入口：任务创建、结果查询与健康检查。"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from . import db
from .layout import analyze_page
from .schemas import AnalyzeRequest, TaskCreated, TaskStatus


def _run_task(task_id: str, request: dict) -> None:
    try:
        result = analyze_page(
            page_width=float(request["page_width"]),
            page_height=float(request["page_height"]),
            raw_words=request["words"],
        )
        db.finish_task(task_id, result)
    except Exception as exc:  # noqa: BLE001 - 任何分析异常都落库可查
        db.fail_task(task_id, f"{type(exc).__name__}: {exc}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="OCR 版面排序服务",
    description="按几何位置对 OCR 词框聚类成行并输出稳定阅读顺序（支持倾斜页面、行距不均、双栏）。",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/tasks", response_model=TaskCreated, status_code=201,
          summary="创建版面排序任务")
def create_task(payload: AnalyzeRequest, bg: BackgroundTasks) -> TaskCreated:
    task_id = uuid.uuid4().hex
    request = payload.model_dump()
    db.create_task(task_id, request)
    bg.add_task(_run_task, task_id, request)
    return TaskCreated(task_id=task_id, status="pending")


@app.get("/tasks/{task_id}", response_model=TaskStatus,
         summary="查询任务状态与排序结果")
def get_task(task_id: str) -> TaskStatus:
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return TaskStatus(**task)
