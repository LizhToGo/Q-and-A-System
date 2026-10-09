# 多模态知识一致性校验与可追溯问答系统

课程设计项目：FastAPI + SQLite + React/Vite。系统以课程资料为检索证据，对回答执行文本、公式、数值/单位、波形/频谱四类检查，并返回可定位来源、置信度、风险提示和 Agent 工作流记录。

## 当前资料状态

仓库中已发现四本 PDF 教材：

- 数字信号处理教程（第五版）
- 信号与系统
- 模拟电子技术基础
- 数字电子技术基础

首次导入结果会进入“待审核”状态。数字信号处理 PDF 有可读取的 OCR/文本层；另外三本主要是扫描页，系统会记录 `ocr_required`，不会把空 OCR 结果发布为知识库事实。视觉模型不可用时必须人工补录或审核。

## 后端启动

Python 3.13 已验证；建议使用虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
```

初始化演示资料（会重置本地 `data\knowledge.db`，应在教材导入前执行）：

```powershell
python -m backend.seed --reset
```

启动 API：

```powershell
python -m uvicorn backend.app.main:app --reload --port 8000
```

打开 `http://127.0.0.1:8000/docs` 可查看 OpenAPI 文档。

## API 入口

- `GET /api/health`：数据库、模型和联网配置状态。
- `GET /api/courses`、`GET /api/topics`、`GET /api/materials`：课程资料浏览。
- `POST /api/search`：本地教材检索。
- `POST /api/answers/ask`：先本地 RAG，再可选 DeepSeek/网络补充，最后执行确定性校验。
- `POST /api/answers/validate`：校验用户提供的答案。
- `GET /api/answers/{answer_id}`：读取可复现答案记录。
- `GET /api/metrics`：返回真实评测覆盖和指标。
- `POST /api/ingestion/upload`：上传新教材并生成待审核任务。
- `GET /api/ingestion/jobs/{job_id}`：查看解析任务、候选页面和异常页。
- `POST /api/ingestion/items/{item_id}/ocr`：对指定扫描页执行本地 RapidOCR，结果仍需审核。
- `POST /api/ingestion/items/{item_id}/assist?mode=auto|text|vision`：调用可选文本/视觉模型辅助解析；结果仍是 pending。
- `PATCH /api/ingestion/items/{item_id}`：审核、修改或拒绝候选。
- `POST /api/ingestion/jobs/{job_id}/publish`：只发布非空且明确审核通过的候选。

## 导入教材
网页导入：启动后端和前端，进入“教材导入”，选择课程并上传 PDF/DOCX/Markdown。系统先解析页码、章节、正文、公式候选和图片数量；扫描页显示“需要 OCR/视觉辅助”，不自动发布。

命令行导入已有文件：

```powershell
python -m backend.import_textbooks ".\数字信号处理教程（第五版） (程佩青) (z-library.sk, 1lib.sk, z-lib.sk).pdf" --course-id 1
```

扫描页只在显式指定时执行本地 OCR，避免首次导入阻塞：

```powershell
python -m backend.import_textbooks ".\信号与系统 (孙国霞) (z-library.sk, 1lib.sk, z-lib.sk).pdf" --course-id 2 --ocr-pages 31,32
```

推荐流程：
1. 先导入一章或一本教材，读取 job 状态；
2. 检查章节、页码、正文和公式候选；
3. 对扫描页调用单页 OCR、配置视觉模型辅助或人工填写；
4. 对公式、数值、波形/频谱字段人工核验；
5. 将候选标为 `approved`；
6. 调用 publish；
7. 再通过检索和问答验证来源定位。


## DeepSeek 配置

后端默认使用 OpenAI-compatible Responses API；适配器也保留 Chat Completions 兼容路径。当前 SDU 服务配置：

```powershell
$env:LLM_PROVIDER = "deepseek"
$env:LLM_BASE_URL = "https://xplt.sdu.edu.cn:4000/v1/responses"
$env:LLM_API_KEY = "你的新令牌"
$env:LLM_MODEL = "ByteDance-volcengine/DeepSeek-V4.1-Flash"
```

SDU 当前的 `/v1/responses` 网关要求请求体同时带有 `messages`，并可能返回 Chat 风格的 `choices`；适配器仍使用 `/v1/responses` 地址，同时发送 `input/instructions` 和兼容字段，并解析 `output_text/output/choices` 三种结果形态。

令牌只写在本地未跟踪的 `.env` 或进程环境变量中，不要提交到 GitHub；已在聊天或日志中暴露的令牌应先撤销，再换新令牌。

模型结果必须经过来源页范围、证据 ID、SymPy 公式解析和 Pint 单位检查；失败则降级为 OCR/文本结果加人工审核。没有 API 配置时，系统仍可使用本地知识库、规则校验和本地 RAG 回答。

可选联网搜索适配器：

```powershell
$env:WEB_SEARCH_ENABLED = "true"
$env:WEB_SEARCH_URL = "你的搜索适配器地址"
$env:WEB_SEARCH_API_KEY = "可选密钥"
```

网络资料与本地教材资料分开记录；网络搜索不能伪装成教材页码依据。

## 前端启动

使用带 npm 的 Node.js：

```powershell
cd frontend
npm install
npm run dev
```

默认前端访问 `http://localhost:5173`，API 默认指向 `http://localhost:8000/api`。如需修改：

```powershell
$env:VITE_API_BASE_URL = "http://localhost:8000/api"
```

前端页面：

- 工作台：提交问题和答案，查看四类检查、证据片段、置信度和风险。
- 评估指标：查看课程/主题覆盖、144 条评测用例和分类指标。
- 教材导入：上传、审核、模型辅助和发布知识候选。

## 评测与测试

运行后端测试：

```powershell
python -m pytest -q backend/tests
```

运行 144 条确定性评测：

```powershell
python -m backend.evaluation.run_evaluation --output evaluation-report.json
```

报告会记录：

- 用例总数和正确数；
- 课程/主题覆盖；
- 各类 TP、FP、FN、TN；
- 准确率、可追溯率；
- 跨模态冲突准确率；
- 误报率；
- 失败用例的 Agent 轨迹和证据数量。

当前 `traceability_rate` 是答案级的严格链接指标：要求样本存在检索证据，且输出的每一条证据链接都指向本次检索结果并带有非空主张；公式等检查若没有可绑定主张，会使该样本不计为可追溯。它仍不等价于回答中每个拆分主张都已经独立完成证据覆盖。

这些指标只反映实际运行结果，不预设图片未规定的达标阈值。
