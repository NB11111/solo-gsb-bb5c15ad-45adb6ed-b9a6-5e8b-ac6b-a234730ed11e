"""合成倾斜双栏页面验证版面算法（不依赖网络与 OCR）。

页面布局（逻辑坐标，之后整体旋转 skew）：
    - 顶部一行跨栏标题
    - 左右两栏各 6 行，每行 4 个词，行距不均、基线有抖动
    - 输入顺序故意打乱
"""

from __future__ import annotations

import math
import random

import pytest

from app.layout import analyze_page

PAGE_W, PAGE_H = 800.0, 1200.0
SKEW = math.radians(2.5)
WORD_H = 20.0
COS, SIN = math.cos(SKEW), math.sin(SKEW)
CX0, CY0 = PAGE_W / 2, PAGE_H / 2


def _rotate(x: float, y: float) -> tuple[float, float]:
    """逻辑坐标 -> 原始（倾斜页面）坐标。"""
    dx, dy = x - CX0, y - CY0
    return (
        CX0 + COS * dx - SIN * dy,
        CY0 + SIN * dx + COS * dy,
    )


def make_word(wid: str, text: str, x: float, y: float,
              w: float = 48.0, h: float = WORD_H) -> dict:
    # 逻辑坐标系下的水平词框四角（加微小随机抖动），再旋转回页面坐标
    jx = random.uniform(-1.2, 1.2)
    jy = random.uniform(-1.2, 1.2)
    local = [
        (x - w / 2 + jx, y - h / 2 + jy),
        (x + w / 2 + jx, y - h / 2 - jy),
        (x + w / 2 - jx, y + h / 2 + jy),
        (x - w / 2 - jx, y + h / 2 - jy),
    ]
    quad = [list(_rotate(px, py)) for px, py in local]
    return {"id": wid, "text": text, "confidence": 0.9, "quad": quad}


def build_page() -> list[dict]:
    words: list[dict] = []

    # 跨栏标题（3 个词）
    for i, t in enumerate(["跨栏标题", "第", "一行"]):
        words.append(make_word(f"title-{i}", t, 400 + (i - 1) * 110, 70,
                               w=90 if i == 0 else 50))

    # 两栏各 6 行，行距故意不均匀
    col_centers = [190.0, 590.0]
    gaps = [60, 110, 70, 130, 80]  # 非均匀行距
    for col, cc in enumerate(col_centers):
        y = 180.0
        for line in range(6):
            for k in range(4):
                t = f"{'左右'[col]}{'甲乙丙丁戊己'[line]}{k}"
                words.append(make_word(f"c{col}-l{line}-w{k}", t,
                                       cc - 90 + k * 60, y + random.uniform(-2, 2),
                                       w=50))
            if line < 5:
                y += gaps[line]

    # 一个退化词框（四角重合）
    words.append({"id": "bad-1", "text": "X", "confidence": 0.5,
                  "quad": [[10, 10], [10, 10], [10, 10], [10, 10]]})

    random.shuffle(words)
    return words


@pytest.fixture(scope="module")
def result():
    random.seed(42)
    return analyze_page(PAGE_W, PAGE_H, build_page())


def test_skew_estimate(result):
    assert abs(result["skew_angle_deg"] - math.degrees(SKEW)) < 0.5


def test_line_count(result):
    # 标题 1 + 两栏各 6 行
    assert result["line_count"] == 13


def test_two_columns_detected(result):
    assert len(result["columns"]) == 2


def test_unclassified(result):
    ids = {u["id"] for u in result["unclassified"]}
    assert "bad-1" in ids


def test_reading_order_title_then_left_then_right(result):
    order = result["reading_order"]
    assert order[0]["type"] == "spanning"
    title_line = result["lines"][order[0]["line_indices"][0]]
    assert title_line["word_ids"] == ["title-0", "title-1", "title-2"]

    body = [
        result["lines"][li]["column"]
        for b in order if b["type"] == "column"
        for li in b["line_indices"]
    ]
    assert body == [0] * 6 + [1] * 6


def test_words_sorted_by_baseline(result):
    for ln in result["lines"]:
        if ln["word_ids"] and ln["word_ids"][0].startswith("c"):
            kk = [int(i.rsplit("-w", 1)[1]) for i in ln["word_ids"]]
            assert kk == sorted(kk)


def test_left_column_top_to_bottom(result):
    left_block = next(
        b for b in result["reading_order"]
        if b["type"] == "column" and b["column"] == 0
    )
    for line, li in enumerate(left_block["line_indices"]):
        assert tuple(result["lines"][li]["word_ids"]) == tuple(
            f"c0-l{line}-w{k}" for k in range(4)
        )


def test_ordered_text(result):
    lines = result["ordered_text"].splitlines()
    assert lines[0]  # 非空
    assert "左甲0" in result["ordered_text"]


if __name__ == "__main__":
    random.seed(42)
    res = analyze_page(PAGE_W, PAGE_H, build_page())
    print(f"skew={res['skew_angle_deg']:.3f}° lines={res['line_count']} "
          f"cols={len(res['columns'])} unclassified={res['unclassified_count']}")
    for b in res["reading_order"]:
        for li in b["line_indices"]:
            print(f"  [{b['type']:8s}] {res['lines'][li]['text']}")
