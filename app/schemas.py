"""请求 / 响应 Pydantic 模型。"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Point(BaseModel):
    x: float
    y: float

    @field_validator("x", "y")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("coordinate must be finite")
        return v


class WordInput(BaseModel):
    id: Any = Field(description="词框唯一标识，可为字符串或数字")
    text: str = ""
    confidence: float = Field(0.0, ge=0.0, le=1.0, description="OCR 置信度 0~1")
    # 四角坐标，顺序为 左上、右上、右下、左下（允许页面倾斜）
    quad: list[tuple[float, float]]
    # 可选字高提示：90° 旋转的竖词与横词仅靠四角几何无法区分，
    # OCR 引擎若知道原始字高可借此消歧（竖排检测用）。
    height_hint: float | None = Field(default=None, gt=0)

    @field_validator("quad")
    @classmethod
    def _four_points(cls, v: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if len(v) != 4:
            raise ValueError("quad must contain exactly 4 [x, y] points")
        for p in v:
            if len(p) != 2 or not all(math.isfinite(c) for c in p):
                raise ValueError("each point must be finite [x, y]")
        return v


class AnalyzeRequest(BaseModel):
    page_width: float = Field(gt=0, description="页面宽度（像素/点）")
    page_height: float = Field(gt=0, description="页面高度（像素/点）")
    words: list[WordInput] = Field(default_factory=list)


class TaskCreated(BaseModel):
    task_id: str
    status: str


class TaskStatus(BaseModel):
    task_id: str
    status: str
    created_at: str
    result: dict | None = None
    error: str | None = None
