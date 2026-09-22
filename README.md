# OCR 版面排序服务

基于 **FastAPI + SQLite** 的 OCR 版面阅读顺序（reading order）服务。
客户端提交页面宽高与一组带**四角坐标、文本、置信度**的词框，服务按几何位置：

1. 估计并校正整页倾斜（置信度加权圆周平均）；
2. 把词框**自动聚类成文本行**——对页面倾斜、行距不均、词高不一、基线抖动均鲁棒；
3. 同一行内按**基线投影**从左到右排序；
4. 自动检测**栏间距**，识别跨栏行（页眉/标题/页脚），多栏严格按照
   **从上到下、再从左到右**（先读完左栏再读右栏）输出；
5. 无法归类的词框（退化几何、竖排等）单独返回。

任务异步执行、SQLite 持久化（WAL），重启后任务结果仍可查询。

---

## 一键启动（Docker）

```bash
docker compose up -d --build
```

服务监听 **端口 8000**：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/docs | Swagger 交互文档（可直接发请求） |
| http://localhost:8000/health | 健康检查 |

只用 Docker 不用 compose 也可以：

```bash
docker build -t ocr-layout-service .
docker run -d -p 8000:8000 -v ocr-data:/data --name ocr-layout ocr-layout-service
```

数据保存在容器内 `/data/tasks.db`（compose 使用命名卷 `ocr-data`，
`docker run` 示例同样挂载为卷）。

## 本地（无 Docker）运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 运行测试

```bash
pip install -r requirements.txt pytest
pytest -q
```

测试用合成的倾斜双栏页面（2.5° 倾斜、行距不均、跨栏标题、退化词框）验证算法。

---

## API

### 1. 创建任务

```
POST /tasks
```

请求体：

```json
{
  "page_width": 595,
  "page_height": 842,
  "words": [
    {
      "id": "w1",
      "text": "Hello",
      "confidence": 0.98,
      "quad": [[100, 100], [160, 102], [159, 126], [99, 124]],
      "height_hint": 24
    }
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `page_width` / `page_height` | number > 0 | 是 | 页面宽高（像素或点，坐标同系即可） |
| `words[].id` | string/number | 是 | 词框唯一标识，原样回传 |
| `words[].text` | string | 否 | OCR 文本，默认 `""` |
| `words[].confidence` | 0~1 | 否 | OCR 置信度，用于倾斜角加权 |
| `words[].quad` | `[[x,y]×4]` | 是 | 四角坐标，顺序 左上→右上→右下→左下（允许倾斜） |
| `words[].height_hint` | number > 0 | 否 | OCR 原始字高。竖排词框旋转 90° 后与水平窄框几何上不可区分，提供字高可消歧 |

返回 `201`：

```json
{ "task_id": "9f2c...", "status": "pending" }
```

### 2. 查询结果

```
GET /tasks/{task_id}
```

任务不存在返回 `404`。处理完成返回：

```json
{
  "task_id": "9f2c...",
  "status": "done",
  "created_at": "2026-09-22T10:00:00+00:00",
  "error": null,
  "result": {
    "skew_angle_deg": -0.91,
    "columns": [
      {"index": 0, "x": 64.13, "y": 0.0, "width": 201.26, "height": 842.0},
      {"index": 1, "x": 344.11, "y": 0.0, "width": 233.26, "height": 842.0}
    ],
    "line_count": 5,
    "lines": [
      {
        "line_index": 0,
        "column": null,
        "spanning": true,
        "baseline_y": 70.1,
        "direction_angle": -0.9,
        "bbox": {"x": 200.0, "y": 56.0, "width": 193.0, "height": 28.0},
        "confidence": 0.975,
        "word_ids": ["t1", "t2"],
        "text": "Reading Order",
        "word_count": 2
      }
    ],
    "ordered_text": "Reading Order\nThe quick brown fox\n...",
    "reading_order": [
      {"type": "spanning", "line_indices": [0]},
      {"type": "column", "column": 0, "line_indices": [1, 2]},
      {"type": "column", "column": 1, "line_indices": [3, 4]}
    ],
    "unclassified": [
      {"id": "bad-1", "text": "X", "reason": "degenerate_zero_length"}
    ],
    "unclassified_count": 1
  }
}
```

结果字段说明：

- **`lines[]`**：每个文本行含行内按基线排好序的 `word_ids`、拼接 `text`、
  原始坐标系 `bbox`、行方向角与平均置信度。
  - `column`：所属栏序号（从左到右，0 起）；跨栏行为 `null`。
  - `spanning`：是否横跨多栏（标题/页眉/页脚）。
- **`reading_order[]`**：阅读顺序块。每个块是一段连续文本：
  - `{"type": "spanning"}` = 跨栏行（按纵向位置穿插，如标题在最前）；
  - `{"type": "column", "column": k}` = 第 k 栏的连续若干行（栏内上→下）。
  - 块顺序即为阅读顺序：跨栏标题 → 左栏整块 → 右栏整块；
    页面中部出现跨栏小标题时会在该位置重新切段。
- **`ordered_text`**：按阅读顺序、每行一段拼接的纯文本（行间 `\n`）。
  CJK 文本词间不插空格，西文词间插一个空格。
- **`unclassified[]`**：无法归类的词框，附原因：
  - `quad_must_have_4_points` / `invalid_point` / `non_finite_coordinate`
  - `degenerate_zero_length`（四角重合等退化框）
  - `orientation_outlier`（与水平夹角 > 55°，如竖排文字）
- **`skew_angle_deg`**：估计出的整页倾斜角（度）。

状态取值：`pending` → `done` / `failed`（失败信息在 `error`）。

---

## 示例请求

仓库内置一份倾斜双栏示例 `examples/request.json`（跨栏标题 + 左右两栏）：

```bash
curl -s -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -d @examples/request.json
# => {"task_id":"<id>","status":"pending"}

curl -s http://localhost:8000/tasks/<id> | python3 -m json.tool
```

期望的阅读顺序为：

```
Reading Order            <- 跨栏标题
The quick brown fox      <- 左栏（上→下）
jumps over
Column two starts here.  <- 右栏（上→下）
Second line.
```

## 算法说明（简要）

1. **词框方向**：四角点协方差矩阵主特征向量 → 词框长轴与字高，
   对整体旋转、点序鲁棒；置信度加权圆周平均得全局倾斜角。
2. **去倾斜 + 行轨道截距**：页面绕中心旋转摆正后，第 k 行会整体落在
   斜轨道 `cy = -tan(skew)·cx + b_k` 上。用截距 `b = cy + tan(skew)·cx`
   而非原始 cy 判断"是否同行"，因此大角度倾斜下也不会把同一行拆成两行。
3. **行聚类**：贪心 + 迭代合并；要求纵向截距邻近（≤ 0.7 行高）
   **且**沿行方向投影邻近（容差由词宽中位数自适应），所以：
   - 行距紧贴/行距不均都能正确分合；
   - 两栏在同一高度的行不会被错误并成一行。
4. **栏检测**：对所有行的横向区间做覆盖扫描，得到"几乎没有普通行经过"
   的纵向白缝（栏间距）；跨栏整行只占少数，不会把栏缝填满。
   栏缝宽度需 ≥ max(3 倍行高, 页宽 3%)，每个栏至少 2 行，否则按单栏处理。
5. **阅读顺序**：跨栏行（行区间覆盖栏缝中点）作为分隔把页面切成纵向段；
   每段内跨栏块在前、随后各栏块按栏左缘从左到右；块内按行从上到下。

## 已知边界

- 竖排文字与横排混排、且竖词框恰好为轴对齐旋转 90° 时，仅靠四角几何
  无法与水平窄框区分（形状歧义）。此时请在词框上提供 `height_hint`
  （OCR 引擎通常知道原始字高），服务即可识别并把竖排词归入 `unclassified`。
- 算法面向"横排文档、矩形词框"；环绕曲线文字、表格单元格内复杂排版
  不在目标场景内。
