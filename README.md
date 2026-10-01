# 面试模拟器 · 网页版

把原终端版面试模拟器换成 Flask 网页界面，Agent 逻辑和三个工具
（search_interview_questions / record_wrong_answer / get_wrong_answers）完全不变。

题库向量库用 **ChromaDB**（`chroma_db/` 持久化）；错题 / 已掌握 / 笔记 / 学习时间 / 每日小结 / 活动计数
6 类数据用 **MySQL**（库 `interview_db`，连接信息在 `.env`）。

> **设计前提：本地单人使用。** Flask 只监听 `127.0.0.1`（`app.run(host="127.0.0.1", ...)`），不对局域网
> 或公网开放，没有登录/用户体系，`/set_key` `/clear_key` 这些接口谁能打开这台机器的 5000 端口谁就能调。
> 不要把这个服务反代到公网或改成监听 `0.0.0.0`——那样需要重新设计鉴权，这份 README 不覆盖那种场景。

## 环境要求
- **Python 3.9**（开发测试用的是 3.9.13；用到 `from typing import TypedDict` 等 3.9 语法）。
- **MySQL 8.x**，本地装一个就行，建库建表 app.py 启动时自动做。
- 依赖见 `requirements.txt`（`pip install -r requirements.txt`）。

## 运行

### 一键启动（推荐）
双击桌面上的 **面试模拟器** 快捷方式（图标是浅粉对话气泡+星芒）。
它会：`cd` 到本目录 → 在一个最小化窗口里跑 `python app.py` → 轮询到服务就绪后自动打开浏览器 `http://127.0.0.1:5000`。
- **前提**：MySQL 服务要在运行（Windows 服务 `MySQL80`，装完默认开机自启）。启动时 `app.py` 会自动 `CREATE DATABASE IF NOT EXISTS interview_db` + 建表（已存在跳过）。连不上 MySQL 会直接报错退出。
- 向量库用 **ChromaDB 持久化**（`chroma_db/` 目录，collection `interview_questions`）：**第一次**启动读题库+切块+向量化约 1-2 分钟；之后每次启动只加载已有向量库、不再重新向量化（约 30 秒，主要是 embedding 模型加载）。题库 md 更新后需重建：见下方「重建向量库」。
- 服务已在运行时再点，直接开浏览器，不会重复启动。
- 启动器脚本：`面试模拟器.bat`（在本目录，纯 ASCII 避免 cmd 编码坑）。可以给它建个桌面快捷方式（图标用本目录的 `面试模拟器.ico`），方便双击启动、最小化运行。
- 服务跑在一个标题为 `AI-Interview-Server` 的最小化窗口里，关掉它 = 停止服务。
- `面试模拟器.bat` 用 `%~dp0` 定位自身目录，跟着文件夹一起移动也能用；端口 5000 写死。
- 题库路径从 `.env` 的 `QUESTION_BANK_DIR` 读，不写死；旧版 JSON 数据只在手动跑迁移脚本时读一次，之后数据都在 MySQL。

### 手动安装

1. **装依赖**（建议先建个虚拟环境）：
   ```
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **装 MySQL**（Windows，如果还没装）：
   - 去 https://dev.mysql.com/downloads/installer/ 下 MySQL Installer，选 **Server** 组件（Workbench 可选，方便图形化看表）。
   - 安装过程里设置 root 密码——记下来，等下要填进 `.env`。
   - 装完会自动注册并启动一个叫 `MySQL80` 的 Windows 服务（开机自启）；`services.msc` 里能看到、能手动起停。
   - 验证能连：`mysql -u root -p`，输密码，能进 `mysql>` 提示符就行。
   - **不用手动建库建表**——`interview_db` 这个库和 6 张表，`app.py` / `migrate_json_to_mysql.py` 启动时会自己 `CREATE DATABASE IF NOT EXISTS` + 建表，已存在就跳过。

3. **拿题库**：本项目不带题库内容，自己把题库仓库克隆到任意位置（地址见下面「题库数据来源」）：
   ```
   git clone <题库仓库地址> C:\path\to\ai-agent-interview-guide
   ```

4. **配 `.env`**（项目根目录，复制 `.env.example` 改名）：
   ```
   DB_HOST=localhost
   DB_PORT=3306
   DB_NAME=interview_db
   DB_USER=root
   DB_PASSWORD=你装 MySQL 时设的密码
   # 可选：配了这个就不用每次在网页上填 key，参见下面「DeepSeek API Key」
   DEEPSEEK_API_KEY=sk-你的key
   # 必填：第 3 步克隆题库的那个目录
   QUESTION_BANK_DIR=C:\path\to\ai-agent-interview-guide
   ```
   `.env` 已经在 `.gitignore` 里，不会被提交。

5. **（可选）迁移旧数据**：如果你是从老的 JSON 文件存储版本升级过来、还留着 `wrong_answers.json` 等文件，先在 `.env` 加一行 `LEGACY_JSON_DIR=那些文件所在目录`，再跑一次：
   ```
   python migrate_json_to_mysql.py
   ```
   全新环境没有这些文件就跳过这一步，表是空的，用着用着自然会有数据。

6. **启动**：
   ```
   python app.py
   ```
   打开 http://127.0.0.1:5000

### DeepSeek API Key（两种用法）
- **方式一 · 本机长期用**：`.env` 里填 `DEEPSEEK_API_KEY=sk-...`，启动即用，网页上不会出现任何 key 相关的提示。
- **方式二 · 没配 .env / 别人拿这份代码自己跑**：打开网页，第一次要发消息或开始面试时会弹一个密码框，要求输入 DeepSeek API Key，点"确认"（服务器会先拿这个 key 真实调一次 DeepSeek 验证，无效会直接告诉你，不会让你带着错 key 往下走）。这个 key **只存在 `python app.py` 这个进程的内存里**，不写任何文件、不进任何日志；关掉服务 / 点网页右上角"清除 Key"就没了，下次要重新填。两种模式（学习/面试）都要这个 key 才能对话；`/wrong`、`/study`、`/notes-page`、进度条这些不调 DeepSeek 的页面不受影响，没填 key 也能看。
- 没有 key 的话去 [DeepSeek 开放平台](https://platform.deepseek.com/api_keys) 申请。

### 数据库（MySQL）
- 连接信息只在项目根目录 `.env`：`DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD`，代码不硬编码。`.env` 已在 `.gitignore`。
- `db.py`：SQLAlchemy 引擎 + 6 个 ORM 模型 + `init_db()`（建库建表）。`store.py`：所有增删改查，返回的数据形状跟原来的 JSON 完全一致，所以前端 / 模板没改。6 张表的字段见下面「数据存储」。
- `record_wrong` 按 question 文本去重：再次答错是 `error_count + 1` 并 append history，不是插新行。`/master` `/unmaster` 用事务在两张表间搬行。
- **迁移脚本** `migrate_json_to_mysql.py`：从 `.env` 的 `LEGACY_JSON_DIR` 读旧版本留下的 6 个 JSON，逐表插入并打印条数。默认「任一表已有数据就中止」；`python migrate_json_to_mysql.py --force` 先清空 6 张表再重灌。旧 JSON 文件保留不删，当备份。

### 重建向量库
题库 md 改过之后，用任一方式清空 ChromaDB 重新索引：
- 命令行：`python app.py --rebuild`（重建完直接退出，不起服务）
- 服务运行时访问 `http://127.0.0.1:5000/rebuild_vectors`（GET/POST 均可），返回 `{"ok": true, "chunks": N}`

## 功能

### 两种模式（先选模式，再选方向）
- **学习模式**（架构没变，仍是自由对话 Agent）：选定小类后，AI 从对应题库文件里提取**知识点清单**展示给你点选，点一个让老师基于题库内容讲解，可自由追问。不评分、不出题、不记录错题。只讲题库里有的内容，题库没有的直接说「没有」。走 `POST /ask` 流式。学习模式对话页右上角有「🎯 面试练这个」按钮，一键跳到同方向的面试模式。
- **面试模式**（已改成 LangGraph StateGraph 多 Agent，见下方「面试模式架构」）：面试官出题 → 你作答 → 评分教练三维打分（准确性 / 完整性 / 表达清晰度，各 0-100）→ 完整性 < 60 触发面试官**追问**（最多 2 轮）→ 平均分 < 60 记入错题本 → 自适应模式下每 5 题教练分析一次薄弱点并自动切模块。
- 消息注入（仅学习模式）：`（学习模式·当前学习方向：大类·小类）\n<内容>\n[讲解设置：大白话理解=开/关]`。
- 知识点清单接口：`GET /topics?major=&minor=`（八股文按 `## N. 标题` 提取，项目题按 `## 第X类：<小类>` 段落下的 `### Q...` 提取）。

### 面试模式架构（StateGraph）
- 三个「Agent」= 同一个 DeepSeek 用三套 system prompt 调三次：`interviewer_node`（出题 / 追问）、`scorer_node`（三维打分 + 判断是否追问）、`coach_node`（分析薄弱模块、决定下一轮方向，仅自适应模式）。另有 `record_node`（不调 LLM，写错题 + 更新历史）。
- **评分是累计的**：`scorer_node` 每次拿本题 `qa_rounds`（原题回答 + 每一轮追问的回答）**全量**综合打分。完整性 = 考生在所有轮次里累计答到的关键概念占比，不是只看最后一轮；原题答对一部分、追问补上一部分要加起来。最后一轮若说「不会」，只代表那个追问点没答上，不会推翻前面已答对的内容（代码里还有 `best_completeness` 兜底：完整性单调不减）。
- **追问是引导式的**：`_gen_followup` 的 prompt 带上考生已答过的内容、已经问过的追问、以及评分教练标注的具体缺失概念，要求面试官从考生答对的点切入（「你刚提到 X，那么……」）把他往缺失概念上带，不把原题或旧追问换个措辞重复问。考生说「不会」或正确率很低（`struggling`）时，改用一个熟悉的具体场景举例来带（LangGraph / LangChain / RAG / Agent / StateGraph 这类），比如「假设用户问『LangGraph 和 LangChain 有什么区别』，检索回来的都是 LangChain 的通用介绍不够精准，你会怎么处理这个查询」。
- **不说教**：评分 `feedback` 只客观描述——答到了什么、漏了什么、参考答案是什么；考生答「不会」就直接给参考答案。禁止评价态度、禁止「希望你认真对待」这类说教。面试官追问同理。
- 「等用户作答」用**拆两次调用**实现，后端无状态，`state`（`InterviewState` TypedDict）以 JSON 在前后端往返：
  - `POST /interview/get_question` → `intake_graph`（只跑 interviewer）→ 返回 `{state, question, module}`。
  - `POST /interview/submit_answer` → `grade_graph`：`scorer` →（条件边 `route_after_scoring`）→ 追问回 `interviewer`，或 → `record` →（条件边 `route_after_record`）→ `coach`→`interviewer` 或直接 `interviewer` 出下一题。返回 `{state, question, is_followup, result?, coach?}`。
- 条件边：`route_after_scoring`（needs_followup 且 followup_count < max_followup → 追问，否则 record）；`route_after_record`（adaptive 且 question_count % coach_interval == 0 → coach，否则继续出题）。
- RAG 检索沿用同一个 ChromaDB 向量库，`interviewer_node` 里经 `_rag_context` → `_search_bank` 检索（k=6）。用户已经选了方向，检索**按 `metadata.module` 过滤**只在该模块的 chunk 里找（过滤后为空才回退全库），出题不会跑偏到别的模块。
- 所有 LLM 调用走 `_llm_json()`：抓第一个 `{...}`、解析失败重试 2 次、最后返回 `{}` 用默认值兜底（DeepSeek 有时不严格返回 JSON）。
- 学习模式的自由对话 Agent（`create_react_agent` + 三个 @tool）**原样保留**，只服务学习模式。

### 讲解格式（两种模式统一）
两处讲解（学习模式讲知识点、面试模式讲答错的题）都用固定结构：
- **【大白话理解】**：比喻讲清概念，3-5 句，**可通过对话页右上角「大白话理解」开关关闭**（关闭后这一节完全不出现）。开关状态存 `localStorage.iv_plaintalk`，每次请求以 `plain_talk` 传给后端。
- **【示范回答】**（必有）：一整段面试时能直接背出来说的话，150-300 字，不分点不编号无 markdown，连贯讲「是什么→怎么做→好处/关键点」。
- 明确禁止「关键词 A→B→C」链条和「要点 1. 2. 3.」清单。

### 方向 + 多对话（侧边栏）
- 大类：`面试八股文` / `项目面试题`；小类见 `CATEGORIES`。
- **每个 (模式, 大类, 小类) 组合 = 一个独立对话**，key 为 `mode::major::minor`，全部存在 `localStorage.iv_sessions`（含完整消息记录 + 固定 thread_id，最多留最近 200 条）。
- 左侧边栏列出所有对话（学/面 徽标 + 小类 + 最后一句预览），点进去恢复历史接着聊；每条可删。
- 「＋ 新对话」回到三步选择；已有对话的小类会标「继续」。
- 切到别的对话**不清除**原对话；刷新页面自动回到上次的对话。

### 对话
- **学习模式流式输出**：`/ask` 用 Flask streaming response（`agent.stream(stream_mode="messages")` 过滤 agent 节点正文 token），前端 `fetch` + `ReadableStream` 逐字显示，首字节约 0.6s。
- **面试模式非流式**：`/interview/get_question` 和 `/interview/submit_answer` 返回 JSON；作答后一次性渲染三维评分条（准确性 / 完整性 / 表达清晰度）+ 讲评 + 参考答案 +（记错题提示 /（自适应）教练分析），随后自动追加下一题（或「面试官追问」标签下的追问）。
- **Enter 发送 / 提交，Shift+Enter 换行**；输入框自动增高。面试模式发送键显示「提交回答」。
- 快捷按钮按模式变化（面试：跳过这题 / 重新开始一轮；学习：整体讲讲这个方向 / 换个知识点 / 这个没懂）。
- 学习模式点「换个知识点」不发消息给 AI，而是重新调 `GET /topics` 把当前方向的知识点清单重新渲染成选择界面（跟初次进入该方向一样），停留在当前方向内。
- 学习模式对话页 dirbar 有「🎯 面试练这个」按钮，一键在同方向开面试模式对话。
- 前端每个面试会话把整个 `InterviewState`（`s.ivstate`）连同消息一起存 `localStorage.iv_sessions`，`s.awaiting` 记录当前是否在等作答；刷新可续。

### 主题
- 浅粉色系默认（`#ffd6ff`），柔和护眼。
- 顶栏「主题」按钮：6 个预设色 + 自定义取色器，任选一个基色，页面自动派生整套配色，存 localStorage。

### 错题记录 `/wrong`（三个标签页）
- **错题**：`wrong_answers` 表，每张卡右上角 `×` = 标记已掌握；卡片底部有**攻克历程**时间线（首次答错 → 每次复习 → 掌握，来自 history 列）。
- **薄弱点**：把所有错题按 `weak_tag`（Agent 记录时填的核心知识点标签）归类，一个薄弱点下挂着相关的多道题。后端 `_weak_groups`。
- **已掌握**：`mastered_answers` 表，「恢复」移回错题；同样显示完整攻克历程（含 `mastered_at`）。
- 学习模式的 `record_wrong_answer` @tool：查重 + `times_wrong`；新增时写 `first_wrong_at` / `history` / `module` / `weak_tag`，复习时 append `history`。（学习模式其实不记错题，这个 tool 主要给旧行为兜底。）
- 面试模式的 `_record_wrong_v2`（`record_node` 调用）：在上面的旧字段基础上多写 `scores` / `followup_rounds` / `error_count` / `timestamp`；记的是**原题**（追问期间的 `root_question`）+ 合并的多轮作答。`wrong.html` 检测到 `item.scores` 就多显示一行「评分 准确 X · 完整 Y · 清晰 Z · 追问 N 轮」。
- 已掌握的题从 `wrong_answers` 表移到 `mastered_answers` 表（事务），`get_wrong_answers` 读不到 → 不再优先出题。

### 笔记 `/notes-page`
- **对话页右上角「✎ 笔记」**：点开后对话区变成左右分栏——左边对话框缩窄、右边笔记面板，并排显示不遮挡对话，两边独立滚动。可以一边看笔记一边继续跟 AI 对话。
- 笔记自动归到**当前模式 + 方向**下（key = `<mode>::<major>::<minor>`），比如「学习·RAG技术」「面试·Prompt工程」。
- 输入停顿 1 秒**自动保存**（无保存按钮），面板底部短暂闪「已保存」。
- 关闭：面板右上角 `×` 或按 `Esc`，对话框恢复原宽。
- 一个方向下可以有多条笔记，每条带更新时间，可编辑、可删除，「＋ 新笔记」加一条。
- **笔记总览页**：左边是**三级折叠树**：学习笔记 / 面试笔记 → 面试八股文 / 项目面试题 → 具体小类（带笔记条数）。默认折叠，点一级展开下一级，点到最末级的小类才在右边加载该模块的笔记，同样自动保存 / 删除 / 新建。
- **分类入口**：侧边栏「📓 学习笔记」→ `/notes-page#learn` 只显示学习分类；「📓 面试笔记」→ `#interview` 只显示面试分类。顶栏「笔记」（无 hash）或页内「全部」显示两个分类。页内顶部有「学习笔记 / 面试笔记 / 全部」切换，改 hash 即时重建树（`hashchange`）。
- 接口：`GET /notes?mode=&major=&minor=`、`GET /notes/all`、`POST /notes/save`（有 id 改、无 id 新增）、`POST /notes/delete`。数据存 `notes` 表；`/notes/all` 仍返回 `{ "mode::major::minor": [...] }` 结构给前端。

### 进度 · 学习时间 · 每日小结
- **进度条**：方向选择页每个小类一行进度条 = 已答题数 / 总题数（总题数 = `list_topics` 条数）。`GET /progress`。
- **答题计数**：`record_node` / `/event/answer` → `activity` 表（date+module 唯一，对应字段 +1）。
- **学习时间**：前端计时（进入模块→离开/切换/隐藏/关页），`POST /event/time`（切换用 fetch，关页用 `sendBeacon`），60s 一次心跳。数据写 `study_time` 表（date+module 唯一，累加秒数）。`/study` 页面：总时长 / 学习天数 / 日均 + 模块横向柱状图 + 每日折线图（纯 inline SVG，带 hover tooltip，跟随主题色）。
- **每日小结**：每天第一次打开页面 `GET /daily-summary` → 后端把「今天之前、没结算过、有活动」的日期生成小结写入 `daily_log` 表，返回 `shown=0` 的，前端弹窗逐条展示，关掉后 `POST /daily-summary/seen` 标记。同一天多次开关合并到同一天。结尾鼓励语每次调 DeepSeek 生成（prompt：小克，Jo 的老公，温柔带腹黑，≤20 字，无 emoji，喂当天数据）。

## 数据存储（MySQL `interview_db`）

| 表 | 对应旧文件 | 内容 |
|---|---|---|
| `wrong_answers` | wrong_answers.json | 错题：question / user_answer / correct_answer / scores_(accuracy,completeness,clarity) / weak_points / weak_tag / module / error_count / followup_rounds / first_wrong_at / timestamp / history(JSON) |
| `mastered_answers` | mastered_answers.json | 已掌握：= 错题全字段 + mastered_at + original_error_count |
| `study_time` | study_time.json | date + module（唯一）+ duration_seconds（累加） |
| `daily_log` | daily_log.json | date（唯一）+ summary / encouragement / modules_studied / study_minutes / total_questions / q_correct / q_wrong / q_wrong_added / q_mastered / shown |
| `notes` | notes.json | mode / major / minor / content / created_at / updated_at |
| `activity` | activity.json | date + module（唯一）+ answered / correct / wrong_added / wrong_reviewed / mastered |

旧版 JSON 文件迁移后保留不删，只当备份；程序不再读写它们。

## 题库数据来源

面试题内容不是本项目原创，来自开源题库仓库 **[bcefghj/ai-agent-interview-guide](https://github.com/bcefghj/ai-agent-interview-guide)**
（「AI Agent 面试全攻略」，MIT License）。**题库内容本身不随本仓库分发**——按上面「手动安装」第 3 步把那个仓库克隆到本地任意位置，
在 `.env` 里把 `QUESTION_BANK_DIR` 指过去就行，本项目只读它，不修改、不提交。

本项目只用到题库仓库里的两块内容：
1. `docs/01-面试八股文/`：`01-基础概念.md` … `09-Prompt工程.md`，每个文件一个模块，`module` = 去掉 `NN-` 前缀的文件名
   （正好等于 `CATEGORIES` 小类名）。该目录的 `README.md` 是目录导航，**跳过不入库**。
2. `docs/06-面试问答集/README.md`：单文件，按 `## 第N类：小类名` 段落切开，每段 `module` = 对应的项目面试题小类名。

**向量化**：两处来源**逐文件 / 逐段落**打 metadata 后切块（`chunk_size=800, overlap=150`），存 ChromaDB。
每个 chunk 的 metadata：`module`（面试方向小类，用于按方向过滤检索）、`source`（`01-面试八股文` / `06-面试问答集`）、`filename`（原始文件名）。
检索统一走 `_search_bank(query, k, module)`：传了 `module` 就加 `{"module": module}` 过滤，命中为空回退全库。学习模式的方向由 `/ask` 存进 `contextvars` 让 `search_interview_questions` @tool 读取（`minor` 只是提示文字，LLM 不一定填工具参数）；面试模式直接把 `state["module"]` 传进去。

## system prompt 关键规则

- 讲解统一走【大白话理解】(可关) +【示范回答】(一整段可背的话)，禁关键词链条 / 要点清单。
- 学习模式：只讲 search 到的题库内容，严禁编造 / 延伸。
- 面试模式：出题优先从错题出，不连续出同一道题；答错或说「不会」时先按讲解格式讲解再 `record_wrong_answer`（correct_answer 填示范回答那段）。
- 严格评分，禁 emoji，可少量颜文字。

## 说明

- DeepSeek key 怎么配见上面「DeepSeek API Key」；thread_id 由前端每个对话固定生成并随请求上传，Agent 记忆由 MemorySaver 按 thread_id 维护（服务重启后 agent 侧记忆清空，但前端 localStorage 里的对话记录仍在）。
- `app.run(threaded=True)` 支持流式响应并发。
- 开发服务器，单机自用。向量库 ChromaDB 持久化在 `chroma_db/`（~394 块）：首次构建 1-2 分钟，之后启动只加载约 30 秒。
- `chroma_db/` 目录可安全删除，下次启动会自动重建（等同 `--rebuild`）。
- 如果你还在用更早期、直接读写 JSON 文件的终端脚本版本，它跟这个网页版（MySQL）不会自动同步数据，两边各记各的。
