# 页问 · PageAsk

**让问题长在书页上。**

页问是一个 Web AI 伴读工具。在 PDF / EPUB 原文上保留少量有价值的划线，点击划线，就地展开一道选择题、相关知识与笔记气泡。它希望帮助读者辨认概念、检查前提、理解论证，把阅读从“看过”推进到“想明白”。

目前是可运行的单用户原型：原书先显示，AI 在后台批注；每次工具提交通过校验后，前端自动出现对应划线。

## 阅读体验

- **保留原书。** PDF 使用 PDF.js 渲染原件，纵向连续滚动，页间保留空隙；EPUB 显示书内章节、图片和样式。支持 TXT。
- **在文字旁思考。** 拖选文字可以手动划线；点击 AI 划线显示中文三选一问题、提示、答案解释及延伸追问，也可以记笔记。
- **划线问 AI。** 选中文字后点击“问 AI”，或在已有划线的气泡中切换“问 AI”；支持自由提问、快捷提问及连续追问，回答逐段显示。模型结合当前页文字、已有知识及 PDF 整页图像理解原文，不等待后台批注。可以停止回答，切页会取消当前问答；对话随划线保存在浏览器，也可存入笔记并导出。
- **公式与键盘。** 划线气泡、思考题与问答支持 LaTeX 行内及独立公式：`$…$`、`$$…$$`、`\(…\)`、`\[…\]`。KaTeX 与字体全部本地加载，不改变原书文字层。问 AI 中按 Enter 发送，Shift+Enter 换行；中文输入法确认不会误发送。
- **稀疏而有依据。** 每页 0–3 处，通常 1 处，总长度不超过正文的 14%；没有值得标记的内容时允许零划线。原文定位、重叠和覆盖率由程序校验。
- **跨页积累知识。** 模型先检索知识库，再创建概念；相同名称及别名复用同一 ID，新增出处，不反复重写定义。
- **阅读不等 AI。** 首次准备当前页及后面 9 页，每向前读 5 页补充后方 5 页；优先提交最前面的未完成页，随后每次最多两页。
- **跳页即切换。** 显式跳转会取消旧队列并从最新位置准备；旧任务迟到的写入被拒绝，已经完成的批注保留。
- **适合长时间阅读。** 暖白背景、深色文字、克制的陶土色，支持缩放、专注模式、书签和 Markdown 阅读记录导出。

每处划线拥有独立的对话历史。追问时会按顺序发送该处全部历史提问和 AI 回答，包括注明未完成状态的已显示回答片段；刷新后继续沿用，不按固定轮数截断，也不混入其他划线的对话。若超过模型实际上下文上限，会明确提示并保留历史。

为复用模型上下文缓存，每处选文会固定系统提示、文字、原图和知识快照，后续请求只追加完整对话。回答下方显示服务实际返回的缓存命中 token 数；首次冷请求、服务端缓存淘汰或服务未提供统计时会如实显示。不能保证每个请求都命中。三轮真实 PDF 问答中，后两轮均命中 1,408 个输入 token，详见 [缓存说明与实测](docs/question-cache.md)。

## 快速开始

需要 Git 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。项目使用 Python 3.12+，不需要 Node.js 或前端构建步骤。

```sh
git clone https://github.com/zzxzz12345/pageask.git
cd pageask
./start.sh
```

脚本会按 `uv.lock` 同步依赖并启动服务。打开 **http://127.0.0.1:8765/**，按 `Ctrl+C` 停止。也可以运行 `./start.sh 8766` 更换端口。

仓库附带原创中文阅读样例；可以直接导入自己的 PDF、EPUB 或 TXT。未配置模型密钥时，仍可阅读原件、手动划线及记笔记。

### 启用 AI 批注

```sh
uv run python scripts/configure_secret.py
./start.sh
```

配置脚本通过隐藏输入把模型密钥保存到本地 `.env.local`，不会进入浏览器或 Git。如果需要额外配置，可以参考 [.env.example](.env.example) 编辑该文件。修改后重启服务。

| 配置项 | 默认值 / 说明 |
| --- | --- |
| `COMMANDCODE_API_KEY` | 必填才能启用 AI；只由后端读取 |
| `COMMANDCODE_BASE_URL` | `https://api.commandcode.ai/provider/v1` |
| `READER_MODEL` | `deepseek/deepseek-v4.1-flash`；另支持 `xiaomi/mimo-v2.6-flash` |
| `READER_EFFORT` | `none`；也可选 `low`、`medium`、`high` |
| `READER_DATA_DIR` | `.reader-data`，保存后端原件、批注、知识库和队列 |
| `READER_ALLOWED_HOSTS` | 额外访问域名，逗号分隔，不含协议和端口 |

当前默认 DeepSeek 无思考配置通过 LangChain 调用网关的 `/responses`，使用 `reasoning.effort=none`，不依赖服务端会话；有思考配置使用 Chat Completions 适配器。此处针对当前网关实现，不保证任意 OpenAI 兼容服务都接受相同参数。

### 局域网访问

`start.sh` 默认监听 `0.0.0.0`，启动时打印本机 `.local` 名称及局域网 IPv4 地址。其他设备在同一网络内，可以使用类似 `http://reader.local:8765/` 或局域网 IP 的地址访问；`.local` 需要设备支持相应的名称解析。

后端自动允许本机主机名及解析出的 IPv4 地址。使用其他域名时，在 `.env.local` 添加：

```dotenv
READER_ALLOWED_HOSTS=reader.example.com,reader.lan
```

将域名解析到运行服务的机器，重启服务即可。当前没有账号与访问鉴权，适用于本机或可信局域网环境。

## AI 如何批注

```mermaid
flowchart LR
    A[原书文字与整页图像] --> B[LangChain 模型会话]
    B --> C[检索与复用知识]
    C --> D[提交当页批注]
    D --> E[原文与密度校验]
    E --> F[SQLite 保存并产生事件]
    F --> G[SSE 推送划线与选择题]
```

模型获得当前窗口逐页配对的提取文字和整页图像，可以对照公式、表格及布局。它通过限定工具读取文字、布局与图像，检索或创建知识、关联出处、提交批注及查询进度，没有任意 shell 或文件系统执行权限。

批注优先选择决定理解的定义、前提、论证连接及易误解的边界。问题应针对原文中的具体判断，干扰项应体现真实的理解分歧。手动划线未关联到 AI 题目时会明确提示，不用固定模板冒充已生成问题。

每页结果保存后立即通过 SSE 推送；断线按事件游标补收，必要时轮询兜底。图文准备、模型往返及校验重试仍需时间，结构校验也不能保证每一道题都达到人工编辑水平。一次十页图文、三个目标页的无思考冷启动实测中，第一页约 48 秒、三页约 138 秒；这是特定材料及网关下的单次记录。

进一步了解：[批注原则与工具契约](docs/annotation-design.md) · [逐页发布与跳页抢占](docs/progressive-delivery.md) · [模型历史实测](docs/model-benchmark.md)。

## 数据与上传范围

| 数据 | 保存位置 | 是否包含在 Git 仓库 |
| --- | --- | --- |
| 模型密钥及个人配置 | `.env.local` | 否 |
| 导入的书籍原件 | 浏览器 IndexedDB、后端 `.reader-data/` | 否 |
| 手动划线、问 AI 对话、答案、笔记、阅读位置 | 浏览器 localStorage | 否 |
| AI 批注、知识库、任务及缓存 | `.reader-data/` 中的 SQLite 等文件 | 否 |
| 问答固定上下文及原图快照 | 同一 SQLite 的 `question_prefixes` 表 | 否 |
| 调试输出、截图及测试报告 | `tmp/`、本地资源文件等 | 否 |
| 原创阅读样例 | `examples/reading-practice.txt` | 是 |

启用 AI 后，当前批次的文字及整页图像会发送到配置的模型服务。本地存储不等于模型推理也在本机。导入材料前请确认自己有相应使用权限。

用户提供的测试书《打开量化投资的黑箱》及其封面、书页截图不随仓库分发。若自行将原件以该文件名放在项目根目录，可使用原有第 55 页示例；否则自动打开原创中文样例。书籍、数据库、密钥和包含完整原件的 `demo.html` 已通过 `.gitignore` 排除。

### 可选：生成离线 HTML

```sh
uv run python scripts/build-demo.py
```

生成的 `demo.html` 可直接打开，支持手动划线和笔记；在线 AI 批注需要后端。存在本地测试 PDF 时，生成文件会内嵌整本原件，**请勿将该文件上传到仓库**。未提供原件时，构建使用原创文字样例。仅本地原书第 55 页包含明确标记的预设题，其他页不会自动生成模板题。

## 开发

```sh
uv sync --locked
uv run pytest -q
# 可选：需要本机 Node.js，使用内置测试工具检查前端完整历史与刷新恢复
node --test tests/test_question_history.mjs
```

常规测试使用隔离数据和模拟模型，不需要真实密钥。以下命令会请求真实模型并可能产生费用：

```sh
uv run python scripts/check_thinking.py --assert-off
# 需要自行提供上述测试书，用于复现同一组书页的历史对照
uv run python scripts/run_benchmark.py
```

```text
├── index.html / styles.css   阅读界面
├── app.js                   阅读状态、手动划线、选择题气泡、笔记
├── pdf-reader.js            PDF 连续滚动与视口附近渲染
├── ai-reader.js             后端连接、SSE、进度与知识库
├── ask-reader.js            划线问 AI、流式回答与本地对话
├── math-render.js           伴读界面的本地 LaTeX 渲染
├── reader_backend/
│   ├── api.py               FastAPI 路由与同源检查
│   ├── agent.py / tools.py  LangChain、提示词、模型工具
│   ├── documents.py         原件解析、文字、布局与整页图像
│   ├── questions.py         原文核验、问答上下文及流式回答
│   └── storage.py / scheduler.py / events.py
├── examples/                可随仓库分发的原创材料
├── scripts/                 启动辅助、演示构建、基准验证
├── tests/                   批注、知识复用、预取、取消、SSE 等测试
└── vendor/                  本地 PDF.js、fflate 及许可证
```

## 当前边界

- PDF 按原页连续显示；EPUB 按 spine 章节、TXT 按文本分组，因此它们的“10/5 页”实际表示阅读节。
- PDF 文字提取采用 PyMuPDF，公式等内容结合整页图像理解，不尝试恢复原始 LaTeX。扫描件 OCR 暂未实现。
- EPUB / TXT 的模型图像目前是文字重排预览，尚不包含书内嵌图片公式；阅读端仍可显示 EPUB 原图。
- 文件上限 20 MB；不支持加密 PDF 或 DRM EPUB。单批过长文本需要拆分。
- 手动记录按浏览器保存，清除浏览器数据会丢失；当前没有账号同步，也不把批注写回 PDF / EPUB 原件。
- 针对历史样例中特定缺失 ToUnicode 映射的字体有兼容修复，不能作为通用乱码或公式提取保证。

第三方前端组件为 PDF.js 4.10.38（Apache-2.0）、fflate 0.8.2（MIT）和 KaTeX 0.19.0（MIT），许可证随 `vendor/` 保留。Python 依赖版本见 `uv.lock`。
