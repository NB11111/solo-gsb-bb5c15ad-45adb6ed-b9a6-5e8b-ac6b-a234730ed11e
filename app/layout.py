"""版面几何分析核心算法。

输入页面尺寸与带四角坐标的词框，输出：

1. 全局倾斜角估计（置信度加权、循环平均），并把页面旋转到水平坐标系；
2. 按"行轨道截距"（cy + tan(skew)·cx）聚类文本行，对行距不均、
   词高不一、基线抖动以及整页倾斜均鲁棒，且不会跨栏合并；
3. 同行内按基线投影（中心点沿行方向的投影）排序；
4. 以行级横向覆盖扫描检测栏间距（白缝），识别跨栏行（标题/页眉页脚），
   并按跨栏行把版面切成纵向段，严格保证"双栏从上到下、再从左到右"；
5. 无法归类的词框（退化几何、竖排方向异常等）单独返回。

所有角度单位为弧度，内部范围 (-pi/2, pi/2]。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any

# 两词框中心竖直距离不超过该倍数行高即视为同行候选
# （0.7：单倍行距下中心距≈1.0 行高，阈值更严，同时吸收基线抖动）
_CENTER_DIST_RATIO = 0.7
# 横向邻近容差倍数（吸收词间空隙），远小于栏间距
_H_GAP_RATIO = 2.5
# 词框方向相对水平方向夹角超过该角度（约 55 度）视为竖排/无法归类
_ORIENT_LIMIT = math.radians(55.0)
# 栏间距最小宽度：max(3 倍行高中位数, 页面宽度 3%)
# 3 倍行高须大于聚类横向容差，否则同高度的两栏行可能被错误并成一行
_GUTTER_MIN_H_RATIO = 3.0
_GUTTER_MIN_W_RATIO = 0.03
# 每个有效栏的最小宽度与最小行数
_BAND_MIN_W_RATIO = 0.1
_BAND_MIN_LINES = 2


def _norm_angle(a: float) -> float:
    """把角度规范到 (-pi/2, pi/2]（词框方向无极性，pi 周期）。"""
    a = (a + math.pi / 2) % math.pi - math.pi / 2
    if a == -math.pi / 2:
        return math.pi / 2
    return a


def _weighted_circular_mean_angle(angles: list[tuple[float, float]]) -> float:
    """置信度加权的循环平均（pi 周期方向）。"""
    s = sum(w * math.sin(2 * a) for a, w in angles)
    c = sum(w * math.cos(2 * a) for a, w in angles)
    return _norm_angle(math.atan2(s, c) / 2)


def _rot(p: tuple[float, float], cos_a: float, sin_a: float,
         cx: float, cy: float) -> tuple[float, float]:
    """绕 (cx, cy) 旋转 +a：x'=cos*x - sin*y, y'=sin*x + cos*y。

    输入词框整体倾斜 +a 时，再施加 +a 的反向约定（见下方 sin 取反调用）
    即可将页面摆正。
    """
    dx, dy = p[0] - cx, p[1] - cy
    return (
        cx + cos_a * dx - sin_a * dy,
        cy + sin_a * dx + cos_a * dy,
    )


@dataclass
class Word:
    id: Any
    text: str
    conf: float
    quad: list[tuple[float, float]]          # 原始四角
    ang: float                               # 长边方向角
    h: float                                 # 字高（短边均值）
    cx0: float
    cy0: float                               # 原始中心
    cx: float = 0.0
    cy: float = 0.0                          # 去倾斜后中心
    rquad: list[tuple[float, float]] = field(default_factory=list)  # 去倾斜后四角


@dataclass
class _Line:
    """聚类中的行（全部坐标位于去倾斜坐标系）。"""

    words: list[Word]
    cy: float
    cx: float
    h: float
    miny: float
    maxy: float
    minx: float
    maxx: float
    prmin: float = 0.0  # 沿行方向投影范围
    prmax: float = 0.0
    off: float = 0.0  # 沿全局行方向的截距 cy - tan(θ)·cx

    @classmethod
    def from_word(cls, w: Word, tan_s: float = 0.0,
                  pr: tuple[float, float] = (0.0, 0.0)) -> "_Line":
        ys = [p[1] for p in w.rquad]
        xs = [p[0] for p in w.rquad]
        line = cls([w], w.cy, w.cx, w.h, min(ys), max(ys), min(xs), max(xs),
                   pr[0], pr[1])
        line.off = line.cy + tan_s * line.cx
        return line

    def add(self, w: Word, tan_s: float = 0.0,
            pr: tuple[float, float] | None = None) -> None:
        ys = [p[1] for p in w.rquad]
        xs = [p[0] for p in w.rquad]
        n = len(self.words)
        self.cy = (self.cy * n + w.cy) / (n + 1)
        self.cx = (self.cx * n + w.cx) / (n + 1)
        self.h = (self.h * n + w.h) / (n + 1)
        self.miny = min(self.miny, min(ys))
        self.maxy = max(self.maxy, max(ys))
        self.minx = min(self.minx, min(xs))
        self.maxx = max(self.maxx, max(xs))
        if pr is not None:
            self.prmin = min(self.prmin, pr[0])
            self.prmax = max(self.prmax, pr[1])
        self.off = self.cy + tan_s * self.cx
        self.words.append(w)


def _build_word(raw: dict) -> tuple[Word | None, str | None]:
    """从请求项构造 Word；失败时返回 (None, 原因)。"""
    quad = raw.get("quad")
    if not isinstance(quad, list) or len(quad) != 4:
        return None, "quad_must_have_4_points"
    pts: list[tuple[float, float]] = []
    for p in quad:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            return None, "invalid_point"
        x, y = p
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return None, "invalid_point"
        if not math.isfinite(x) or not math.isfinite(y):
            return None, "non_finite_coordinate"
        pts.append((float(x), float(y)))

    # 词框长轴（文本基线方向）：四点（去中心）协方差矩阵的主特征向量，
    # 对整体旋转与标签环绕顺序都鲁棒。
    cx = sum(p[0] for p in pts) / 4
    cy = sum(p[1] for p in pts) / 4
    sxx = sum((p[0] - cx) ** 2 for p in pts)
    syy = sum((p[1] - cy) ** 2 for p in pts)
    sxy = sum((p[0] - cx) * (p[1] - cy) for p in pts)
    if sxx + syy <= 1e-12:
        return None, "degenerate_zero_length"
    # 主特征向量角（结果在 [-pi/4, pi/4] 附近，主轴朝 x）
    eig_ang = 0.5 * math.atan2(2 * sxy, sxx - syy)
    # 沿该方向与正交方向的方差
    ca, sa = math.cos(eig_ang), math.sin(eig_ang)
    var_along = ca * ca * sxx + 2 * ca * sa * sxy + sa * sa * syy
    var_cross = sa * sa * sxx - 2 * ca * sa * sxy + ca * ca * syy

    # 几何消歧：方差沿哪个方向更大，长轴就指向哪。
    # 注意：90° 轴对齐旋转在几何上与水平窄框不可区分，此时若客户端提供
    # height_hint（OCR 原始字高），用它选择与字高垂直的方向为长轴。
    hint = raw.get("height_hint")
    if isinstance(hint, (int, float)) and math.isfinite(hint) and hint > 0:
        # 与最终 h 完全同尺度地比较两个候选长轴方向的框厚（2×四点平均
        # 垂直距离），取更接近客户端字高提示者，消歧 90° 轴对齐旋转。
        def thickness(theta: float) -> float:
            nnx, nny = -math.sin(theta), math.cos(theta)
            return 2 * sum(abs((p[0] - cx) * nnx + (p[1] - cy) * nny) for p in pts) / 4

        h_if_eig = thickness(eig_ang)
        h_if_perp = thickness(eig_ang + math.pi / 2)
        if abs(h_if_eig - hint) <= abs(h_if_perp - hint):
            ang = _norm_angle(eig_ang)
        else:
            ang = _norm_angle(eig_ang + math.pi / 2)
    elif var_along + 1e-12 >= var_cross:
        ang = _norm_angle(eig_ang)
    else:
        ang = _norm_angle(eig_ang + math.pi / 2)

    # 字高：四点到长轴的平均垂直距离 ×2
    nx, ny = -math.sin(ang), math.cos(ang)
    h = 2 * sum(abs((p[0] - cx) * nx + (p[1] - cy) * ny) for p in pts) / 4
    if h <= 1e-9:
        return None, "degenerate_zero_length"
    conf = raw.get("confidence", 0.0)

    return Word(
        id=raw.get("id"),
        text=str(raw.get("text", "")),
        conf=float(conf) if isinstance(conf, (int, float)) and math.isfinite(conf) else 0.0,
        quad=pts,
        ang=ang,
        h=h,
        cx0=cx,
        cy0=cy,
    ), None


def _estimate_global_skew(words: list[Word]) -> float:
    samples = [(w.ang, max(w.conf, 1e-3)) for w in words]
    return _weighted_circular_mean_angle(samples)


def _deskew(words: list[Word], skew: float, pw: float, ph: float) -> None:
    # 页面倾斜 +skew，施加反向旋转 -skew 将其摆正
    cos_a, sin_a = math.cos(skew), -math.sin(skew)
    cx, cy = pw / 2, ph / 2
    for w in words:
        w.rquad = [_rot(p, cos_a, sin_a, cx, cy) for p in w.quad]
        w.cx = sum(p[0] for p in w.rquad) / 4
        w.cy = sum(p[1] for p in w.rquad) / 4


def _cluster_lines(words: list[Word], skew: float) -> list[_Line]:
    """按去倾斜后的纵向位置聚类成文本行。

    摆正后各行水平，但页面是绕中心旋转的，因此第 k 条行会整体落在斜轨道
    cy = -tan(skew)·cx + b_k 上（截距 b_k 才是真正的"第几行"）。判据：

      b = cy + tan(skew)·cx

    同一行所有词 b 相同；相邻行 b 差一个行距；两栏同一高度的行 b 也相同，
    再用沿水平方向的投影邻近度把它们区分开。
    """
    tan_s, cos_s = math.tan(skew), max(math.cos(skew), 1e-6)
    ux, uy = 1.0, 0.0  # 摆正后行方向为绝对水平

    def proj_range_pts(quad: list[tuple[float, float]]) -> tuple[float, float]:
        ps = [p[0] * ux + p[1] * uy for p in quad]
        return min(ps), max(ps)

    # 数据驱动的横向邻近容差：吸收同一条行内词间空隙，又远小于正常栏间距。
    # 用去倾斜后词框沿行方向的实际投影（斜排词投影更长，词间空隙也会被放大）。
    med_w = statistics.median(
        proj_range_pts(w.rquad)[1] - proj_range_pts(w.rquad)[0] for w in words
    )

    def h_gap_tol(hh: float) -> float:
        return max(_H_GAP_RATIO * hh, 1.5 * med_w)

    def b_of(cx: float, cy: float) -> float:
        return cy + tan_s * cx

    order = sorted(
        range(len(words)),
        key=lambda i: (b_of(words[i].cx, words[i].cy), words[i].cx),
    )
    lines: list[_Line] = []
    for i in order:
        w = words[i]
        p0, p1 = proj_range_pts(w.rquad)
        wb = b_of(w.cx, w.cy)
        best: _Line | None = None
        best_key = 1e18
        # 行按截距 b 有序，倒序只看在 w "上方"的候选行
        for ln in reversed(lines):
            if ln.off * cos_s - _CENTER_DIST_RATIO * ln.h > wb * cos_s:
                break
            # 沿行方向必须邻近：两栏在同一高度的行不能仅因纵向接近而合并。
            # 容差取数倍行高/词宽以吸收词间空隙，仍远小于栏间距。
            gap_tol = h_gap_tol(max(w.h, ln.h))
            if p0 > ln.prmax + gap_tol or p1 < ln.prmin - gap_tol:
                continue
            # 纵向判据用行轨道截距（页面整体倾斜不影响同行判断）
            dist = abs((w.cy - ln.cy) + tan_s * (w.cx - ln.cx))
            if dist * cos_s <= _CENTER_DIST_RATIO * max(w.h, ln.h) and dist < best_key:
                best, best_key = ln, dist
        if best is None:
            lines.append(_Line.from_word(w, tan_s, (p0, p1)))
        else:
            best.add(w, tan_s, (p0, p1))

    # 后处理：同一行里先到达的词可能已建立独立行，需要把同行的小行合并。
    # 反复扫描直到没有可合并的行对（截距邻近 + 沿行方向邻近）。
    merged = True
    while merged and len(lines) > 1:
        merged = False
        lines.sort(key=lambda ln: ln.off)
        for i in range(len(lines) - 1, -1, -1):
            ln = lines[i]
            target: _Line | None = None
            target_key = 1e18
            for j in range(i - 1, -1, -1):
                other = lines[j]
                if other.off * cos_s + _CENTER_DIST_RATIO * max(ln.h, other.h) < ln.off * cos_s:
                    break
                gap_tol = h_gap_tol(max(ln.h, other.h))
                if ln.prmin > other.prmax + gap_tol or ln.prmax < other.prmin - gap_tol:
                    continue
                d = abs((ln.cy - other.cy) + tan_s * (ln.cx - other.cx))
                if d * cos_s <= _CENTER_DIST_RATIO * max(ln.h, other.h) and d < target_key:
                    target, target_key = other, d
            if target is not None:
                for w in ln.words:
                    target.add(w, tan_s, proj_range_pts(w.rquad))
                lines.pop(i)
                merged = True
                break
    return lines


def _detect_gutters(lines: list[_Line], page_w: float) -> list[tuple[float, float]]:
    """扫描行级横向覆盖，找出"几乎没有普通行经过"的纵向白缝（栏间距）。

    跨栏的标题/页眉行横跨整页，若计入覆盖会填满所有栏缝，因此改用
    「经过该 x 的行数 / 总行数」覆盖率判据：双栏中普通行各占一侧，
    栏缝覆盖率接近 0，而跨栏行只把它抬到 1/N，可被阈值排除。
    """
    spans = []
    for ln in lines:
        xmin = min(min(p[0] for p in w.rquad) for w in ln.words)
        xmax = max(max(p[0] for p in w.rquad) for w in ln.words)
        if xmax > xmin:
            spans.append((xmin, xmax))
    n = len(spans)
    if n < 4:
        return []

    dom_lo = min(s[0] for s in spans)
    dom_hi = max(s[1] for s in spans)
    if dom_hi - dom_lo <= 1e-9:
        return []

    # 覆盖计数阶跃事件：起点 +1，终点 -1
    events: list[tuple[float, int]] = []
    for x0, x1 in spans:
        events.append((x0, 1))
        events.append((x1, -1))
    events.sort(key=lambda e: (e[0], e[1]))

    med_h = statistics.median([ln.h for ln in lines])
    min_w = max(_GUTTER_MIN_H_RATIO * med_h, _GUTTER_MIN_W_RATIO * page_w)
    # 双栏普通缝覆盖率 ≈ 0；允许极少量噪声行（如个别跨栏短行）
    max_cover = max(1, int(n * 0.15))

    gutters: list[tuple[float, float]] = []
    cover = 0
    seg_start = dom_lo
    for x, d in events:
        if cover <= max_cover and x > seg_start:
            if seg_start > dom_lo + 1e-9 and x < dom_hi - 1e-9 and (x - seg_start) >= min_w:
                gutters.append((seg_start, x))
        cover += d
        seg_start = x
    return gutters


def _split_bands(gutters: list[tuple[float, float]],
                 dom_lo: float, dom_hi: float,
                 med_h: float, page_w: float) -> list[tuple[float, float]]:
    """按栏间距切出栏带，过滤掉过窄的候选栏。"""
    cuts = [dom_lo]
    for g0, g1 in gutters:
        cuts.extend((g0, g1))
    cuts.append(dom_hi)
    bands: list[tuple[float, float]] = []
    min_band_w = max(3.0 * med_h, _BAND_MIN_W_RATIO * page_w)
    for i in range(0, len(cuts) - 1, 2):
        lo, hi = cuts[i], cuts[i + 1]
        if hi - lo >= min_band_w:
            bands.append((lo, hi))
    return bands


def _line_span(ln: _Line) -> tuple[float, float]:
    xmin = min(min(p[0] for p in w.rquad) for w in ln.words)
    xmax = max(max(p[0] for p in w.rquad) for w in ln.words)
    return xmin, xmax


def _finalize_line(ln: _Line, index: int, col: int | None, spanning: bool,
                   skew: float) -> dict:
    """行内排序并构造输出结构。bbox 以原始页面坐标系返回。"""
    # 摆正后行内残余方向（对全局 skew 做差），用循环均值汇总行内词方向
    s = sum(math.sin(2 * (_norm_angle(w.ang - skew))) for w in ln.words)
    c = sum(math.cos(2 * (_norm_angle(w.ang - skew))) for w in ln.words)
    dir_ang = _norm_angle(math.atan2(s, c) / 2)

    dx, dy = math.cos(dir_ang), math.sin(dir_ang)
    if dx < 0:  # 投影方向统一朝右
        dx, dy = -dx, -dy

    # 以本行词框中心均值为原点做基线投影排序
    ox = sum(w.cx for w in ln.words) / len(ln.words)
    oy = sum(w.cy for w in ln.words) / len(ln.words)
    words_sorted = sorted(ln.words, key=lambda w: (w.cx - ox) * dx + (w.cy - oy) * dy)

    # 行包围盒（原始坐标）
    all_pts = [p for w in words_sorted for p in w.quad]
    bbox = {
        "x": min(p[0] for p in all_pts),
        "y": min(p[1] for p in all_pts),
        "width": max(p[0] for p in all_pts) - min(p[0] for p in all_pts),
        "height": max(p[1] for p in all_pts) - min(p[1] for p in all_pts),
    }
    conf = sum(w.conf for w in words_sorted) / len(words_sorted)

    return {
        "line_index": index,
        "column": col,
        "spanning": spanning,
        "baseline_y": oy,
        "direction_angle": math.degrees(_norm_angle(skew + dir_ang)),
        "bbox": bbox,
        "confidence": round(conf, 4),
        "word_ids": [w.id for w in words_sorted],
        "text": _join_text([w.text for w in words_sorted]),
        "word_count": len(words_sorted),
    }


_CJK_RANGES = (
    (0x3000, 0x303F),   # CJK 标点
    (0x3040, 0x30FF),   # 平假名 / 片假名
    (0x3400, 0x4DBF),   # 扩展 A
    (0x4E00, 0x9FFF),   # 基本汉字
    (0xF900, 0xFAFF),   # 兼容表意文字
    (0xFF00, 0xFFEF),   # 全角符号
)


def _is_cjk(text: str) -> bool:
    """CJK 表意文字占非空白字符 40% 以上视为中日韩文本（词间不插空格）。"""
    chars = [ch for ch in text if not ch.isspace()]
    if not chars:
        return False
    cjk = sum(
        1 for ch in chars
        if any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES)
    )
    return cjk / len(chars) >= 0.4


def _join_text(texts: list[str]) -> str:
    parts = [t for t in texts if t]
    if not parts:
        return ""
    sep = "" if _is_cjk("".join(parts)) else " "
    return sep.join(parts)


def analyze_page(page_width: float, page_height: float,
                 raw_words: list[dict]) -> dict:
    """版面排序主入口。

    返回行分组、阅读顺序、拼接文本与无法归类词框，详见 README 响应示例。
    """
    words: list[Word] = []
    rejected: list[dict] = []
    for raw in raw_words:
        w, reason = _build_word(raw)
        if w is None:
            rejected.append(
                {
                    "id": raw.get("id") if isinstance(raw, dict) else None,
                    "text": raw.get("text", "") if isinstance(raw, dict) else "",
                    "reason": reason,
                }
            )
        else:
            words.append(w)

    if not words:
        return {
            "skew_angle_deg": 0.0,
            "columns": [],
            "line_count": 0,
            "lines": [],
            "ordered_text": "",
            "reading_order": [],
            "unclassified": rejected,
            "unclassified_count": len(rejected),
        }

    # 1) 全局倾斜估计 + 去倾斜
    skew = _estimate_global_skew(words)
    _deskew(words, skew, page_width, page_height)

    # 2) 方向异常（竖排/严重倾斜）的词不参与行列聚类。
    # 只按词框自身方向判断，避免被全局平均角度稀释；词框方向无极性，
    # 与水平夹角接近 90° 即视为竖排。
    regular: list[Word] = []
    for w in words:
        if abs(_norm_angle(w.ang)) > _ORIENT_LIMIT:
            rejected.append(
                {
                    "id": w.id,
                    "text": w.text,
                    "reason": "orientation_outlier",
                    "confidence": round(w.conf, 4),
                    "bbox": {
                        "x": min(p[0] for p in w.quad),
                        "y": min(p[1] for p in w.quad),
                        "width": max(p[0] for p in w.quad) - min(p[0] for p in w.quad),
                        "height": max(p[1] for p in w.quad) - min(p[1] for p in w.quad),
                    },
                }
            )
        else:
            regular.append(w)

    # 3) 聚类成行
    lines = _cluster_lines(regular, skew)

    # 4) 栏检测
    gutters = _detect_gutters(lines, page_width)
    med_h = statistics.median([ln.h for ln in lines])
    dom_lo = min(_line_span(ln)[0] for ln in lines)
    dom_hi = max(_line_span(ln)[1] for ln in lines)
    bands = _split_bands(gutters, dom_lo, dom_hi, med_h, page_width) if gutters else []

    # 行分配到栏：跨栏行（横跨栏缝中点，如标题/页眉）先标记；
    # 普通行按横向重叠分配，不与任何栏重叠则按区间距离归入最近栏。
    def crosses_gutter(x0: float, x1: float, g0: float, g1: float) -> bool:
        gm = (g0 + g1) / 2
        return x0 <= gm <= x1

    band_line_counts = [0] * len(bands)
    line_meta: dict[int, dict] = {}
    for ln in lines:
        x0, x1 = _line_span(ln)
        meta: dict = {}
        if bands and any(crosses_gutter(x0, x1, g0, g1) for g0, g1 in gutters):
            meta["spanning"], meta["band"] = True, None
        else:
            meta["spanning"] = False
            if bands:
                overlaps = [
                    (bi, min(x1, b1) - max(x0, b0))
                    for bi, (b0, b1) in enumerate(bands)
                ]
                bi, ov = max(overlaps, key=lambda t: t[1])
                if ov > 0:
                    meta["band"] = bi
                else:
                    center = (x0 + x1) / 2
                    meta["band"] = min(
                        range(len(bands)),
                        key=lambda k: max(0.0, bands[k][0] - center, center - bands[k][1]),
                    )
                band_line_counts[meta["band"]] += 1
            else:
                meta["band"] = None
        line_meta[id(ln)] = meta

    # 每个有效栏至少包含 _BAND_MIN_LINES 行，否则退化为单栏
    if not bands or any(c < _BAND_MIN_LINES for c in band_line_counts):
        bands = []
        for ln in lines:
            line_meta[id(ln)] = {"band": None, "spanning": False}

    # 6) 构造阅读顺序块
    # ---- 单栏：全部按纵向阅读顺序 ----
    if not bands:
        ordered_lines = sorted(lines, key=lambda ln: (ln.off, ln.cx))
        line_out: list[dict] = []
        ordered_ids: list[int] = []
        for i, ln in enumerate(ordered_lines):
            lo = _finalize_line(ln, i, None, False, skew)
            line_out.append(lo)
            ordered_ids.append(i)
        return _result(skew, [], line_out,
                       [{"type": "body", "line_indices": ordered_ids}],
                       rejected)

    # ---- 多栏：跨栏行按纵向切段，段内"跨栏块在前，各栏从左到右" ----
    spanning = [ln for ln in lines if line_meta[id(ln)]["spanning"]]
    cuts = sorted({s for ln in spanning for s in (ln.miny, ln.maxy)})
    segments: list[tuple[float, float]] = []
    prev = -math.inf
    for ct in cuts:
        segments.append((prev, ct))
        prev = ct
    segments.append((prev, math.inf))

    def seg_of(ln: _Line) -> int:
        midy = (ln.miny + ln.maxy) / 2
        for si, (s0, s1) in enumerate(segments):
            if s0 <= midy < s1:
                return si
        return len(segments) - 1

    blocks: list[tuple[int, int, float, dict]] = []
    for si, (s0, s1) in enumerate(segments):
        seg_spans = [ln for ln in spanning if seg_of(ln) == si]
        if seg_spans:
            seg_spans.sort(key=lambda ln: (ln.off, ln.cx))
            blocks.append((
                si, 0, min(ln.cx for ln in seg_spans),
                {"type": "spanning", "segment": si, "lines": seg_spans},
            ))
        for bi in range(len(bands)):
            bl = [
                ln for ln in lines
                if not line_meta[id(ln)]["spanning"]
                and line_meta[id(ln)]["band"] == bi
                and seg_of(ln) == si
            ]
            if bl:
                bl.sort(key=lambda ln: (ln.off, ln.cx))
                # 同段同类型的栏块按栏左缘（从左到右）排序
                blocks.append((
                    si, 1 + bi, bands[bi][0],
                    {"type": "column", "segment": si, "column": bi, "lines": bl},
                ))

    blocks.sort(key=lambda b: (b[0], b[1], b[2]))

    # 7) 统一编号并输出
    line_out: list[dict] = []
    reading_order: list[dict] = []
    idx = 0
    for _, _, _, blk in blocks:
        if blk["type"] == "spanning":
            ids = []
            for ln in blk["lines"]:
                line_out.append(_finalize_line(ln, idx, None, True, skew))
                ids.append(idx)
                idx += 1
            reading_order.append({"type": "spanning", "line_indices": ids})
        else:
            ids = []
            for ln in blk["lines"]:
                line_out.append(_finalize_line(ln, idx, blk["column"], False, skew))
                ids.append(idx)
                idx += 1
            reading_order.append(
                {"type": "column", "column": blk["column"], "line_indices": ids}
            )

    columns_out = [
        {
            "index": bi,
            "x": round(b0, 2),
            "y": 0.0,
            "width": round(b1 - b0, 2),
            "height": round(page_height, 2),
        }
        for bi, (b0, b1) in enumerate(bands)
    ]
    return _result(skew, columns_out, line_out, reading_order, rejected)


def _result(skew: float, columns: list[dict], lines: list[dict],
            reading_order: list[dict], rejected: list[dict]) -> dict:
    ordered_text = "\n".join(ln["text"] for ln in lines)
    return {
        "skew_angle_deg": round(math.degrees(skew), 4),
        "columns": columns,
        "line_count": len(lines),
        "lines": lines,
        "ordered_text": ordered_text,
        "reading_order": reading_order,
        "unclassified": rejected,
        "unclassified_count": len(rejected),
    }
