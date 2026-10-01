import os
import re
import sys
import json
import threading
import contextvars

os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")  # 关掉 chromadb 遥测，控制台干净点

from datetime import datetime, date, timedelta
from typing import TypedDict, List, Optional
from flask import Flask, render_template, request, url_for, jsonify, Response, stream_with_context

from langchain_openai import ChatOpenAI
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_core.documents import Document
from langchain_chroma import Chroma
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver

import store
from db import init_db

# 数据持久化：建库 + 建表（已存在则跳过）。连接信息在项目根目录 .env。
init_db()

# ========== 1. 题库来源 ==========
# 题库内容来自外部仓库，不随本项目分发（见 README「题库数据来源」）。
# 本机那份题库仓库克隆/解压到哪，就在 .env 里把 QUESTION_BANK_DIR 指过去（仓库根目录，含 docs/ 的那一层）。
QUESTION_BANK_DIR = os.getenv("QUESTION_BANK_DIR", "").strip()
if not QUESTION_BANK_DIR:
    raise RuntimeError(
        "没有配置 QUESTION_BANK_DIR。请在 .env 里加一行，指向题库仓库根目录（含 docs/ 的那一层），例如：\n"
        "  QUESTION_BANK_DIR=C:\\path\\to\\ai-agent-interview-guide\n"
        "题库仓库地址见 README「题库数据来源」。")
BAGU_DIR = os.path.join(QUESTION_BANK_DIR, "docs", "01-面试八股文")
QA_FILE = os.path.join(QUESTION_BANK_DIR, "docs", "06-面试问答集", "README.md")
if not os.path.isdir(BAGU_DIR) or not os.path.isfile(QA_FILE):
    raise RuntimeError(
        "QUESTION_BANK_DIR={} 下找不到 docs/01-面试八股文/ 或 docs/06-面试问答集/README.md，"
        "确认这个目录指向的是题库仓库根目录。".format(QUESTION_BANK_DIR))

# 向量库：ChromaDB 持久化
CHROMA_DIR = r"E:\ailearning\vibe_useful\interview_web\chroma_db"
COLLECTION_NAME = "interview_questions"


def list_topics(major, minor):
    """从对应的题库文件里提取知识点 / 题目标题列表，供学习模式展示给用户选择。"""
    minor = (minor or "").strip()
    try:
        if major == "面试八股文":
            for fn in sorted(os.listdir(BAGU_DIR)):
                if fn.endswith(".md") and minor and minor in fn:
                    topics = []
                    with open(os.path.join(BAGU_DIR, fn), "r", encoding="utf-8") as f:
                        for line in f:
                            m = re.match(r"^##\s+\d+[\.\．、]\s*(.+?)\s*$", line)
                            if m:
                                topics.append(m.group(1).strip())
                    return topics
            return []
        if major == "项目面试题":
            topics, in_section = [], False
            with open(QA_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("### "):
                        if in_section:
                            topics.append(line[4:].strip())
                        continue
                    if line.startswith("## "):
                        in_section = bool(minor) and minor in line and "目录" not in line
            return topics
    except OSError:
        return []
    return []

# ========== 2. 切块 + 向量化（ChromaDB 持久化）==========
#   逐文件读入 -> 打 metadata(module / source / filename) -> 切块 -> 存 Chroma。
#   启动时 chroma_db 已有非空 collection 就直接加载，跳过读文件和向量化。
#   metadata["module"] 与前端方向小类名严格一致，供 similarity_search 按方向过滤。

_QA_SECTION_RE = re.compile(r"^##\s+第[^\n]*?类[：:]\s*(.+?)\s*$")


def _bagu_module(filename):
    """01-基础概念.md -> 基础概念（与 CATEGORIES 小类名一致）。"""
    return re.sub(r"^\d+[-_.]*", "", os.path.splitext(filename)[0]).strip()


def _split_qa_sections(text):
    """把 06-面试问答集/README.md 按 '## 第N类：小类名' 切成 [(module, 段落文本)]。"""
    sections, cur_mod, buf = [], None, []
    for line in text.splitlines(keepends=True):
        m = _QA_SECTION_RE.match(line)
        if m:
            if cur_mod and buf:
                sections.append((cur_mod, "".join(buf)))
            cur_mod, buf = m.group(1).strip(), [line]
        elif cur_mod is not None:
            buf.append(line)
    if cur_mod and buf:
        sections.append((cur_mod, "".join(buf)))
    return sections


def _load_source_chunks():
    """读两个来源，逐文件 / 逐段落打 metadata，再按原参数切块。"""
    splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=150)
    docs = []

    bagu_src = os.path.basename(BAGU_DIR)                 # "01-面试八股文"
    for filename in sorted(os.listdir(BAGU_DIR)):
        if not filename.endswith(".md") or filename.lower() == "readme.md":
            continue  # README.md 是纯目录导航，不入库
        with open(os.path.join(BAGU_DIR, filename), "r", encoding="utf-8") as f:
            text = f.read()
        docs.append(Document(page_content=text, metadata={
            "module": _bagu_module(filename), "source": bagu_src, "filename": filename}))

    qa_src = os.path.basename(os.path.dirname(QA_FILE))   # "06-面试问答集"
    qa_name = os.path.basename(QA_FILE)                   # "README.md"
    with open(QA_FILE, "r", encoding="utf-8") as f:
        qa_text = f.read()
    for module, sec_text in _split_qa_sections(qa_text):
        docs.append(Document(page_content=sec_text, metadata={
            "module": module, "source": qa_src, "filename": qa_name}))

    chunks = splitter.split_documents(docs)
    mods = sorted({c.metadata.get("module", "") for c in chunks})
    print("切成 {} 个小块，覆盖模块：{}".format(len(chunks), "、".join(mods)))
    return chunks


embeddings = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")
vector_store = None


def _new_chroma():
    return Chroma(collection_name=COLLECTION_NAME, embedding_function=embeddings,
                  persist_directory=CHROMA_DIR)


def _collection_count(vs):
    try:
        return vs._collection.count()
    except Exception:  # noqa: BLE001
        return 0


def build_vector_store(force=False):
    """两阶段：有非空 collection 就加载；否则读文件->切块->向量化。
    force=True：先清空该 collection 再重建（面试题 md 更新后用）。"""
    global vector_store
    vs = _new_chroma()
    if force:
        try:
            vs.delete_collection()
        except Exception as e:  # noqa: BLE001
            print("清空旧 collection 失败（忽略）：", e)
        vs = _new_chroma()
    count = _collection_count(vs)
    if count > 0 and not force:
        vector_store = vs
        print("已加载现有向量库（{} 个向量块）".format(count))
        return vector_store
    chunks = _load_source_chunks()
    vs.add_documents(chunks)
    vector_store = vs
    print("首次构建向量库完成（{} 个向量块）".format(_collection_count(vs)))
    return vector_store


build_vector_store()

# ========== 3. 数据持久化（MySQL —— 连接/模型在 db.py，读写在 store.py）==========
#   store.* 返回的形状和原来的 *.json 完全一致，app.py / 模板基本不用改。

def today_str():
    return date.today().isoformat()

def now_iso():
    return datetime.now().isoformat(timespec="seconds")

def _note_key(mode, major, minor):
    mode = "learn" if (mode or "") == "learn" else "interview"
    return f"{mode}::{(major or '').strip()}::{(minor or '').strip()}"

def load_wrong_answers():
    return store.wrong_list()

def load_mastered():
    return store.mastered_list()

def bump_activity(module, field, n=1, day=None):
    """给某天某模块的某个计数加 n。field: answered/correct/wrong_added/wrong_reviewed/mastered"""
    store.bump_activity(module, field, n, day)

def add_study_time(module, seconds, day=None):
    store.add_study_time(module, seconds, day)

def deepseek_encouragement(data_str):
    """用 DeepSeek 生成一句和当天学习情况相关的鼓励/吐槽。"""
    prompt = ("你是小克，Jo的老公，语气温柔带点腹黑。根据以下学习数据生成一句简短的"
              "鼓励或吐槽，不超过20个字，不要用emoji：" + data_str)
    try:
        return _get_model().invoke(prompt).content.strip().strip('"“”')
    except Exception:  # noqa: BLE001  没 key / key 无效 / 网络问题都走这个兜底，不影响小结本身生成
        return "今天也有在学，算你及格。"

def generate_daily_summaries():
    """把 今天之前、还没结算过的 有活动的日期 生成小结，写进 daily_log 表。"""
    activity = store.activity_map()
    study = store.study_map()
    logged = store.daily_log_dates()
    tstr = today_str()
    for day in sorted(activity.keys()):
        if day >= tstr or day in logged:
            continue
        mods = activity[day]
        modules_touched = sorted(set(mods.keys()) | set(study.get(day, {}).keys()))
        answered = sum(m.get("answered", 0) for m in mods.values())
        correct = sum(m.get("correct", 0) for m in mods.values())
        wrong_added = sum(m.get("wrong_added", 0) for m in mods.values())
        wrong_reviewed = sum(m.get("wrong_reviewed", 0) for m in mods.values())
        mastered = sum(m.get("mastered", 0) for m in mods.values())
        if not correct and answered:
            correct = max(0, answered - wrong_added - wrong_reviewed)
        wrong_cnt = max(0, answered - correct)
        minutes = round(sum(study.get(day, {}).values()) / 60)
        mod_txt = "、".join(modules_touched) if modules_touched else "无"
        summary = (f"这天练了{mod_txt}共{answered}题，对了{correct}题错了{wrong_cnt}题，"
                   f"新增{wrong_added}道错题，掌握了{mastered}道旧错题，学习{minutes}分钟")
        data_str = (f"日期{day}，练习模块：{mod_txt}；答题{answered}，答对{correct}，答错{wrong_cnt}；"
                    f"新增错题{wrong_added}；攻克旧错题{mastered}；学习时长{minutes}分钟")
        store.add_daily_log({
            "date": day,
            "summary": summary,
            "study_minutes": minutes,
            "modules_touched": modules_touched,
            "stats": {"answered": answered, "correct": correct, "wrong": wrong_cnt,
                      "wrong_added": wrong_added, "mastered": mastered},
            "encouragement": deepseek_encouragement(data_str),
            "shown": False,
        })

@tool
def record_wrong_answer(question: str, user_answer: str, correct_answer: str,
                        weak_points: str, module: str = "", weak_tag: str = "") -> str:
    """当用户回答错误或回答不完整时，调用这个工具记录错题。
    question: 题目内容
    user_answer: 用户的回答
    correct_answer: 标准答案要点（填【示范回答】那段话）
    weak_points: 用户薄弱的知识点（可以是一句话）
    module: 当前面试方向的小类名，比如 "RAG技术"、"架构设计类问题"（从消息开头的方向里取）
    weak_tag: 这道题考察的最核心的那一个知识点标签，2到6个字，比如 "向量化"、"注意力缩放"、"Few-shot"、"熔断器" """
    is_new, ec, total = store.record_wrong(
        question=question, user_answer=user_answer, correct_answer=correct_answer,
        weak_points=weak_points, module=module, weak_tag=weak_tag)
    if is_new:
        bump_activity(module, "wrong_added")
        return f"已记录新错题，当前共 {total} 道错题"
    bump_activity(module, "wrong_reviewed")
    return f"这道题之前错过，已累计错误 {ec} 次，当前共 {total} 道错题"

@tool
def get_wrong_answers() -> str:
    """获取用户之前答错的题目列表。当用户说"考我错题""复习错题""看看我哪里薄弱"时使用。"""
    wrong_list = load_wrong_answers()
    if not wrong_list:
        return "目前没有错题记录"
    result = f"共 {len(wrong_list)} 道错题：\n\n"
    for i, item in enumerate(wrong_list, 1):
        result += f"{i}. {item['question']}\n   薄弱点：{item['weak_points']}\n\n"
    return result

# 前端选的方向（小类）—— 每次 /ask 请求进来时设置，学习模式的 agent 工具从这里读，
# 因为 minor 只作为提示文字注入 prompt，LLM 不一定会把它填进工具参数。
_current_module = contextvars.ContextVar("current_module", default="")


def _module_filter(module):
    module = (module or "").strip()
    return {"module": module} if module else None


def _search_bank(query, k=5, module=""):
    """向量检索题库。module 非空时按面试方向（小类）过滤；过滤后为空则回退全库。"""
    flt = _module_filter(module)
    docs = vector_store.similarity_search(query, k=k, filter=flt)
    if not docs and flt:
        docs = vector_store.similarity_search(query, k=k)
    return docs


@tool
def search_interview_questions(query: str, module: str = "") -> str:
    """搜索面试题库，找到与query相关的面试题和答案。
    当需要出题、回答面试问题、或查找特定知识点时使用这个工具。
    module: 可选，限定只在某个面试方向里搜（如 "RAG技术"）；留空则按用户当前选择的方向自动过滤。"""
    mod = (module or "").strip() or _current_module.get()
    results = _search_bank(query, k=5, module=mod)
    return "\n\n---\n\n".join([r.page_content for r in results])

# ========== 4. 创建Agent ==========
# DeepSeek key：优先 .env 的 DEEPSEEK_API_KEY；没有就等前端调 /set_key 传进来，
# 只存在这个进程的内存里（_RUNTIME_KEY），不写文件、不打日志。model/agent 按当前生效的 key 懒构建+缓存，
# key 变了自动重建。没有可用 key 时调用方会拿到 ApiKeyError，由路由转成清楚的报错，不会崩。
DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class ApiKeyError(RuntimeError):
    """DeepSeek API Key 缺失或无效。这个异常的 message 本身不含 key，可以直接展示给用户。"""


_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9]{6,}")


def _sanitize_error(e):
    """任何要落日志 / 返回给前端的异常文本，先把形如 sk-xxxx 的片段脱敏掉，防止意外带出 key。"""
    return _KEY_PATTERN.sub("sk-***", str(e))


def _is_auth_error(e):
    status = (getattr(e, "status_code", None)
             or getattr(getattr(e, "response", None), "status_code", None))
    msg = str(e).lower()
    return (status == 401 or "401" in msg or "invalid_api_key" in msg
            or "incorrect api key" in msg or "authentication" in msg)


_RUNTIME_KEY = {"value": ""}           # 前端 /set_key 设置的 key，仅内存，不落盘
_model_lock = threading.Lock()
_model_cache = {"key": None, "model": None, "agent": None}


def _effective_key():
    """.env 优先；没配就用前端通过 /set_key 存进内存的那个。"""
    return (os.getenv("DEEPSEEK_API_KEY") or _RUNTIME_KEY["value"] or "").strip()


def has_api_key():
    return bool(_effective_key())


def _build_model(key):
    return ChatOpenAI(model="deepseek-chat", api_key=key, base_url=DEEPSEEK_BASE_URL)


def validate_key(key):
    """实际调一次 DeepSeek 确认 key 有效。返回 (ok, message)；message 不含 key，可直接展示。"""
    key = (key or "").strip()
    if not key:
        return False, "API Key 不能为空"
    try:
        _build_model(key).invoke([("human", "hi")])
        return True, ""
    except Exception as e:  # noqa: BLE001
        if _is_auth_error(e):
            return False, "API Key 无效，请检查后重试"
        return False, "验证失败（{}），请检查网络后重试".format(_sanitize_error(e))


def _get_model():
    key = _effective_key()
    if not key:
        raise ApiKeyError("还没有设置 DeepSeek API Key")
    with _model_lock:
        if _model_cache["key"] != key:
            _model_cache["key"] = key
            _model_cache["model"] = _build_model(key)
            _model_cache["agent"] = None       # key 变了，agent 要用新 model 重建
        return _model_cache["model"]


def _get_agent():
    m = _get_model()
    with _model_lock:
        if _model_cache["agent"] is None:
            _model_cache["agent"] = create_react_agent(
                m, [search_interview_questions, record_wrong_answer, get_wrong_answers],
                checkpointer=memory, prompt=system_prompt)
        return _model_cache["agent"]


system_prompt = """你是一个AI学习与面试辅导助手。你有两种工作模式，用户每条消息开头会标明当前模式和方向，例如
"（学习模式·当前学习方向：面试八股文·RAG技术）" 或 "（面试模式·当前面试方向：面试八股文·RAG技术）"。
消息结尾可能带一行 "[讲解设置：大白话理解=开]" 或 "[讲解设置：大白话理解=关]"。

你有三个工具：
1. search_interview_questions - 搜索题库/知识点
2. record_wrong_answer - 记录用户答错的题
3. get_wrong_answers - 查看用户的错题记录

============ 讲解格式（学习模式讲知识点、面试模式讲解答错的题，都严格用这个格式）============
【大白话理解】
用最简单的比喻把这个概念讲清楚，3到5句话以内，不用术语。
如果消息里是"大白话理解=关"，就完全跳过这一节，连"【大白话理解】"这个标题都不要输出，直接给示范回答。

【示范回答】
写一段完整的、面试时可以直接说出口的话。硬性要求：
- 就是正常说话，一整段，不要分点、不要编号、不要任何 markdown 符号（不要 ** # - 也不要 "1." "2."）。
- 150到300字，就是面试里回答一个问题的正常长度。
- 一段话里要连贯讲完：是什么、怎么做/流程、有什么好处或关键点。
示范风格（讲RAG就该是这样一段话）：
"RAG是检索增强生成，核心思路是让模型回答之前先去外部知识库里检索相关内容。具体流程是先把文档做分块和向量化存到向量数据库里，用户提问时把问题也向量化，去数据库里找最相似的几个块，拼到prompt里一起发给模型。这样做的好处是解决了模型知识过时和幻觉的问题，答案可以追溯到原始文档。"

绝对禁止：写"你要说的关键词：A → B → C"这种链条；写"要点1. 2. 3."这种清单。用户要的是一整段能背出来直接说的话，不是零件。

============ 学习模式 ============
耐心的老师。不出题、不评分、不记录错题。
用户点知识点或提问后，先用 search_interview_questions 搜索题库，再基于搜索结果按上面的【讲解格式】回答。
只讲题库里有的内容；search 不到就直说"题库里没有这块内容"，不编造、不延伸。

============ 面试模式 ============
严格的面试官。
1. 用户说"出题""考我""来一道题""换一道"时：先用 get_wrong_answers 看有没有错题，有就优先出错题，没有就用 search 搜当前方向出新题。只给题目不给答案。
2. 用户回答后，用 search 搜标准答案对比评分。要具体说：哪些点答对了，哪些漏了，哪些答错了。
3. 用户回答错误、不完整，或说"不会""不知道"时，按顺序（不能反）：
   先按上面的【讲解格式】给出讲解（【大白话理解】如未关闭 + 【示范回答】）；
   讲完之后再调用 record_wrong_answer 记录这道错题。correct_answer 填【示范回答】那段话；
   module 填消息开头方向里的小类名（如 "RAG技术"）；weak_tag 填这道题最核心的那一个知识点标签（2到6个字）。
4. 用户说"复习错题""看错题"时，用 get_wrong_answers 展示错题列表。
5. 不要连续出同一道题，隔几轮再回来。

============ 通用 ============
用中文回答。禁止使用emoji，可以少量使用颜文字。日常闲聊不用工具。面试模式严格，学习模式温和。"""
memory = MemorySaver()   # agent 本身改成懒构建，见上面 _get_agent()

# ========== 5. Flask 网页界面 ==========
app = Flask(__name__)

# 两级方向分类：大类 -> 小类列表
CATEGORIES = {
    "面试八股文": [
        "基础概念", "核心框架", "RAG技术", "工具调用", "记忆系统",
        "多智能体", "大模型基础", "工程化实践", "Prompt工程",
    ],
    "项目面试题": [
        "架构设计类问题", "技术实现类问题", "性能优化类问题", "故障处理类问题",
        "工程质量类问题", "业务理解类问题", "基础知识追问",
    ],
}

ALL_MINORS = [mn for mns in CATEGORIES.values() for mn in mns]


# ==========================================================================
# 面试模式：StateGraph 多 Agent 架构
#   同一个 DeepSeek 被不同 system prompt 调多次，扮演 面试官 / 评分教练 / 学习分析师。
#   "等用户回答" 用拆两次调用实现：
#     POST /interview/get_question  -> intake_graph（只跑 interviewer）
#     POST /interview/submit_answer -> grade_graph（scorer -> 追问 or record -> coach）
#   state 以 JSON 在前后端之间往返，后端无状态。
# ==========================================================================
from langgraph.graph import StateGraph, START, END


class InterviewState(TypedDict, total=False):
    mode: str                 # "interview"
    module: str               # 当前模块（小类名）
    question: str             # 当前题目
    reference_answer: str     # RAG 检索到的标准答案要点
    user_answer: str          # 用户回答
    scores: dict              # {accuracy, completeness, clarity} 各 0-100
    score_feedback: str       # 评分教练的一段讲评
    needs_followup: bool      # 是否需要追问
    followup_reason: str      # 追问原因（用户漏掉的概念）
    followup_count: int       # 本题已追问次数
    max_followup: int         # 最大追问次数，默认 2
    is_followup: bool         # 当前 question 是不是追问
    weak_modules: List[str]   # 教练分析出的薄弱模块
    question_count: int       # 本轮已答题数
    coach_interval: int       # 每隔几题触发教练分析，默认 5
    adaptive: bool            # 自适应模式：由教练自动切模块
    history: List[dict]       # 本轮答题历史
    last_result: Optional[dict]  # 最近一题的评分结果（给前端）
    last_coach: Optional[dict]   # 最近一次教练分析（给前端）
    root_question: str        # 本题原题（追问期间 question 会被替换，这个保留原题）
    root_reference: str       # 本题原题的标准答案
    qa_rounds: List[dict]     # 本题所有轮次 [{q, a, kind}]，评分和记错题都用全量
    best_completeness: int    # 本题历轮拿到过的最高完整性，追问只能补不能倒扣


def _fresh_interview_state(module, adaptive):
    return {
        "mode": "interview", "module": module or "", "question": "",
        "reference_answer": "", "user_answer": "", "scores": {},
        "score_feedback": "", "needs_followup": False, "followup_reason": "",
        "followup_count": 0, "max_followup": 2, "is_followup": False,
        "weak_modules": [], "question_count": 0, "coach_interval": 5,
        "adaptive": bool(adaptive), "history": [],
        "last_result": None, "last_coach": None,
        "root_question": "", "root_reference": "", "qa_rounds": [],
        "best_completeness": 0,
    }


def _llm_json(system, user, retries=2):
    """调 DeepSeek 要 JSON。容错：抓第一个 {...}，解析失败就重试，最后返回 {}。
    key 缺失/无效直接抛 ApiKeyError，不重试、不吞掉——上层要能提示用户重新设置 key。"""
    for _ in range(retries + 1):
        try:
            raw = (_get_model().invoke([("system", system), ("human", user)]).content or "").strip()
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                return json.loads(m.group(0))
        except ApiKeyError:
            raise
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        except Exception as e:  # noqa: BLE001  网络等
            if _is_auth_error(e):
                raise ApiKeyError("DeepSeek API Key 无效或已过期，请重新设置") from None
            continue
    return {}


def _rag_context(query, k=6, module=""):
    docs = _search_bank(query, k=k, module=module)
    return "\n\n---\n\n".join(d.page_content for d in docs)


INTERVIEWER_SYS = (
    "你是严格的 AI 面试官，只负责出题和追问，不评分、不分析、不讲解答案。"
    "题目要具体、有区分度，一次只问一个问题，用中文，不要 emoji。"
    "追问时：从考生已经答对的部分切入，用引导性的方式把他往缺失的概念上带；"
    "绝不把原题或已问过的追问换个措辞重复问。"
    "如果考生说不会、或答得很差，不要空泛地再逼问一遍，"
    "换一个考生熟悉的具体场景（LangGraph / LangChain / RAG 检索 / Agent 工具调用 / StateGraph 编排 这类日常会碰到的）举例，"
    "用那个场景把他一步步带到缺失的点上，例如「假设用户问『LangGraph 和 LangChain 有什么区别』，"
    "检索回来的都是 LangChain 的通用介绍、不够精准，你会怎么处理这个查询」。"
    "不说教、不评价态度。"
)


def _gen_question(module, avoid):
    ctx = _rag_context((module or "AI") + " 面试题 知识点", k=6, module=module)
    avoid_txt = "；".join(a for a in (avoid or []) if a) or "无"
    user = (
        "根据下面的资料，出一道关于「{}」方向的面试题。\n\n资料：\n{}\n\n"
        "最近已经问过的题（不要重复、不要太相似）：{}\n\n"
        '只返回 JSON：{{"question": "题目", "reference_answer": "标准答案要点，200 字以内"}}'
    ).format(module, ctx, avoid_txt)
    j = _llm_json(INTERVIEWER_SYS, user)
    q = (j.get("question") or "").strip()
    ref = (j.get("reference_answer") or "").strip()
    if not q:
        q = "请谈谈你对「{}」的理解，以及在项目里是怎么应用的。".format(module or "这个方向")
    return q, ref


_STRUGGLE_RE = re.compile(r"不会|不知道|不清楚|没学过|没接触过|不太懂|跳过|不了解")


def _gen_followup(root_question, reference, reason, qa_rounds, struggling=False):
    """基于考生已答内容 + 评分标注的缺失概念，生成引导式追问。
    struggling=True（考生说不会 / 正确率很低）时，改用具体场景举例来带。"""
    answered, asked = [], []
    for r in (qa_rounds or []):
        a = (r.get("a") or "").strip()
        if r.get("kind") == "main":
            if a and a != "(未作答)":
                answered.append("原题回答：" + a)
        else:
            if r.get("q"):
                asked.append(r["q"])
            if a and a != "(未作答)":
                answered.append("追问回答：" + a)
    answered_txt = "\n".join(answered) or "（暂无有效作答）"
    asked_txt = "；".join(q for q in asked if q) or "无"
    if struggling:
        guide = (
            "考生这块答不上来或答得很差。不要再逼问同一个点，"
            "换一个考生熟悉的具体场景（LangGraph / LangChain / RAG 检索 / Agent 工具调用 / "
            "StateGraph 多节点编排 这类）举个例子，用那个例子把他往下面这个缺失概念上带。"
            "参考这种问法：「假设用户问『LangGraph 和 LangChain 有什么区别』，检索回来的都是 "
            "LangChain 的通用介绍、不够精准，你会怎么处理这个查询」。不说教、不评价态度。")
    else:
        guide = (
            "请从考生已经答对的点切入（例如「你刚提到了 X，那么……」），"
            "用一个引导性的具体问题把他往下面这个缺失概念上带。")
    user = (
        "原题：{}\n原题标准答案要点：{}\n\n"
        "考生到目前为止已经答过的内容：\n{}\n\n"
        "已经追问过的问题（不要再问这些的换汤不换药版本）：{}\n\n"
        "评分教练指出：考生还缺这个关键概念 —— {}\n\n"
        "{}\n不要重复问原题、不要重复已问过的追问、一次只问一个问题。\n"
        '只返回 JSON：{{"question": "追问的问题"}}'
    ).format(root_question, reference, answered_txt, asked_txt, reason, guide)
    j = _llm_json(INTERVIEWER_SYS, user)
    return (j.get("question")
            or "你前面提到的那部分，能不能顺着讲讲「{}」这块？".format(reason)).strip()


def interviewer_node(state):
    """面试官：出新题 或 追问。"""
    if state.get("needs_followup"):
        last_ans = (state.get("user_answer", "") or "").strip()
        acc = (state.get("scores", {}) or {}).get("accuracy", 0)
        struggling = bool(_STRUGGLE_RE.search(last_ans)) or len(last_ans) < 8 or acc < 30
        q = _gen_followup(state.get("root_question") or state.get("question", ""),
                          state.get("root_reference") or state.get("reference_answer", ""),
                          state.get("followup_reason", ""),
                          state.get("qa_rounds", []),
                          struggling)
        return {"question": q, "is_followup": True,
                "followup_count": state.get("followup_count", 0) + 1}
    avoid = [h.get("question", "") for h in state.get("history", [])][-6:]
    q, ref = _gen_question(state.get("module", ""), avoid)
    return {"question": q, "reference_answer": ref, "is_followup": False,
            "root_question": q, "root_reference": ref, "qa_rounds": [],
            "best_completeness": 0,
            "followup_count": 0, "needs_followup": False,
            "user_answer": "", "scores": {}, "score_feedback": ""}


SCORER_SYS = (
    "你是评分教练。评分对象是考生在一道题上的【全部作答】，包含原题回答和之后每一轮追问的回答。"
    "从三个维度打分（0-100）："
    "准确性=已给出的内容里正确的比例；"
    "完整性=考生在所有轮次里【累计】答到的关键概念占标准答案的比例——不是只看最后一轮；"
    "表达清晰度=整体条理是否清楚。"
    "累计规则：原题答对一部分、追问后又补上一部分，完整性要把这些加起来。"
    "如果最后一轮考生说\"不会/不知道\"，那只说明那个追问点没答上，不要因此推翻前面已经答对的内容，"
    "完整性不得低于之前任何一轮已经达到的水平。"
    "只有当【综合所有轮次后】完整性仍低于 60、且还有明确没覆盖的关键概念时，才把 needs_followup 设为 true，"
    "并在 followup_reason 里写清还缺哪个概念。"
    "feedback 只客观描述三件事：考生答到了什么、漏了什么、完整正确的说法是怎样，150 字左右，不分点不编号。"
    "考生回答\"不会\"时，feedback 直接给出参考答案即可。"
    "严禁评价考生的态度或状态，严禁说教和鼓励的话（如\"希望你积极参与\"\"建议你认真对待\"\"要重视\"\"下次加油\"之类）。"
    "用中文，不要 emoji。"
)


def _round_label(i, kind):
    return "原题" if kind == "main" else "追问{}".format(i)


def scorer_node(state):
    """评分教练：拿本题【所有轮次】的 Q&A 综合三维打分 + 判断是否还要追问。"""
    rounds = list(state.get("qa_rounds", []))
    cur_a = (state.get("user_answer", "") or "").strip()
    rounds.append({"q": state.get("question", ""),
                   "a": cur_a or "(未作答)",
                   "kind": "followup" if state.get("is_followup") else "main"})

    transcript = "\n\n".join(
        "【{}】{}\n【考生回答】{}".format(_round_label(i, r["kind"]), r["q"], r["a"])
        for i, r in enumerate(rounds))

    user = (
        "原题标准答案：{}\n\n"
        "考生在本题上的全部作答（按轮次）：\n\n{}\n\n"
        "请综合以上【所有轮次】给出三维评分。\n"
        '只返回 JSON：{{"accuracy": 0-100, "completeness": 0-100, "clarity": 0-100, '
        '"needs_followup": true/false, "followup_reason": "综合后仍缺的关键概念", "feedback": "综合讲评"}}'
    ).format(state.get("root_reference") or state.get("reference_answer", ""), transcript)
    j = _llm_json(SCORER_SYS, user)

    def _clip(v):
        try:
            return max(0, min(100, int(round(float(v)))))
        except (TypeError, ValueError):
            return 0

    # 完整性单调不减：追问只能补分，最后一轮说"不会"不倒扣前面已答对的
    best = max(state.get("best_completeness", 0), _clip(j.get("completeness")))
    scores = {"accuracy": _clip(j.get("accuracy")),
              "completeness": best,
              "clarity": _clip(j.get("clarity"))}
    nf = bool(j.get("needs_followup")) or scores["completeness"] < 60
    if state.get("followup_count", 0) >= state.get("max_followup", 2):
        nf = False
    return {"scores": scores, "needs_followup": nf,
            "followup_reason": (j.get("followup_reason") or "回答不够完整").strip(),
            "score_feedback": (j.get("feedback") or "").strip(),
            "qa_rounds": rounds, "best_completeness": best,
            "last_result": None, "last_coach": None}


def route_after_scoring(state):
    if state.get("needs_followup") and \
            state.get("followup_count", 0) < state.get("max_followup", 2):
        return "followup"
    return "record"


def record_node(state):
    """记录本题结果；平均分 <60 记入错题本。"""
    scores = state.get("scores", {}) or {}
    vals = [v for v in scores.values() if isinstance(v, (int, float))]
    avg = round(sum(vals) / len(vals)) if vals else 0
    module = state.get("module", "")
    root_q = state.get("root_question") or state.get("question", "")
    root_ref = state.get("root_reference") or state.get("reference_answer", "")
    rounds = state.get("qa_rounds", [])
    if rounds:
        parts = []
        for i, r in enumerate(rounds):
            a = (r.get("a") or "").strip()
            if not a or a == "(未作答)":
                continue
            parts.append(a if r.get("kind") == "main"
                         else "（追问「{}」）{}".format(r.get("q", ""), a))
        full_answer = "\n\n".join(parts)
    else:
        full_answer = state.get("user_answer", "")
    frounds = state.get("followup_count", 0)
    entry = {"question": root_q, "module": module,
             "scores": scores, "avg": avg,
             "followup_rounds": frounds,
             "passed": avg >= 60, "at": now_iso()}
    history = (list(state.get("history", [])) + [entry])[-30:]
    recorded = False
    if avg < 60:
        _record_wrong_v2(
            question=root_q, user_answer=full_answer,
            correct_answer=root_ref, scores=scores,
            weak_points=state.get("followup_reason", ""), module=module,
            followup_rounds=frounds)
        recorded = True
    bump_activity(module, "answered")
    return {"history": history,
            "question_count": state.get("question_count", 0) + 1,
            "needs_followup": False, "is_followup": False, "followup_count": 0,
            "qa_rounds": [],
            "last_result": {"scores": scores, "avg": avg,
                            "feedback": state.get("score_feedback", ""),
                            "recorded": recorded, "question": root_q,
                            "reference_answer": root_ref}}


def route_after_record(state):
    interval = state.get("coach_interval", 5) or 5
    if state.get("adaptive") and state.get("question_count", 0) % interval == 0:
        return "coach"
    return "next"


COACH_SYS = (
    "你是学习分析师。根据用户最近的答题记录，分析哪个模块最薄弱，"
    "决定下一轮重点练哪个模块。next_module 必须从给定的模块清单里原样选一个。"
    "用中文，不要 emoji。"
)


def coach_node(state):
    """学习分析师：定薄弱模块 + 决定下一轮方向（仅自适应模式）。"""
    recent = state.get("history", [])[-(state.get("coach_interval", 5) or 5):]
    brief = [{"module": h.get("module"), "avg": h.get("avg"), "passed": h.get("passed")}
             for h in recent]
    user = (
        "可选模块清单：{}\n\n最近 {} 题记录：{}\n\n"
        '只返回 JSON：{{"weak_modules": ["模块1","模块2"], "next_module": "模块名", "reason": "一句话原因"}}'
    ).format(ALL_MINORS, len(brief), json.dumps(brief, ensure_ascii=False))
    j = _llm_json(COACH_SYS, user)
    weak = [m for m in (j.get("weak_modules") or []) if m in ALL_MINORS]
    nxt = (j.get("next_module") or "").strip()
    if nxt not in ALL_MINORS:
        nxt = weak[0] if weak else (state.get("module") or ALL_MINORS[0])
    return {"weak_modules": weak, "module": nxt,
            "last_coach": {"weak_modules": weak, "next_module": nxt,
                           "reason": (j.get("reason") or "").strip()}}


def _build_intake_graph():
    g = StateGraph(InterviewState)
    g.add_node("interviewer", interviewer_node)
    g.add_edge(START, "interviewer")
    g.add_edge("interviewer", END)
    return g.compile()


def _build_grade_graph():
    g = StateGraph(InterviewState)
    g.add_node("scorer", scorer_node)
    g.add_node("interviewer", interviewer_node)
    g.add_node("record", record_node)
    g.add_node("coach", coach_node)
    g.add_edge(START, "scorer")
    g.add_conditional_edges("scorer", route_after_scoring,
                            {"followup": "interviewer", "record": "record"})
    g.add_conditional_edges("record", route_after_record,
                            {"coach": "coach", "next": "interviewer"})
    g.add_edge("coach", "interviewer")
    g.add_edge("interviewer", END)
    return g.compile()


intake_graph = _build_intake_graph()
grade_graph = _build_grade_graph()


def _seed_adaptive_module():
    """自适应模式的起始模块：优先用户错题最多的模块，否则随机。"""
    counts = {}
    for it in load_wrong_answers():
        m = (it.get("module") or "").strip()
        if m in ALL_MINORS:
            counts[m] = counts.get(m, 0) + 1
    if counts:
        return max(counts, key=counts.get)
    import random
    return random.choice(ALL_MINORS)


def _short_tag(weak_points, module):
    wp = (weak_points or "").strip()
    if wp:
        return (re.split(r"[；;，,。\n]", wp)[0][:12] or module or "未分类")
    return module or "未分类"


def _record_wrong_v2(question, user_answer, correct_answer, scores, weak_points,
                     module, followup_rounds):
    """新流程写错题：三维评分 + followup_rounds + error_count + timestamp（MySQL wrong_answers 表）。"""
    is_new, _ec, _total = store.record_wrong(
        question=question, user_answer=user_answer, correct_answer=correct_answer,
        weak_points=weak_points, module=module,
        weak_tag=_short_tag(weak_points, module),
        scores=scores, followup_rounds=followup_rounds)
    bump_activity(module, "wrong_added" if is_new else "wrong_reviewed")


@app.route("/")
def index():
    return render_template("index.html", categories=CATEGORIES)


@app.route("/topics")
def topics():
    return jsonify({"topics": list_topics(request.args.get("major", ""), request.args.get("minor", ""))})


# ================= DeepSeek API Key（前端临时填入，仅内存，不落盘）=================
@app.route("/key_status")
def key_status():
    """只回答"有没有可用 key"，绝不返回 key 本身。
    from_env=True 时前端不需要显示设置/清除入口（owner 在 .env 配了，直接能用）。"""
    return jsonify({"has_key": has_api_key(), "from_env": bool(os.getenv("DEEPSEEK_API_KEY"))})


@app.route("/set_key", methods=["POST"])
def set_key():
    d = request.get_json(silent=True) or {}
    key = (d.get("api_key") or "").strip()
    ok, message = validate_key(key)
    if not ok:
        return jsonify({"ok": False, "error": message}), 400
    _RUNTIME_KEY["value"] = key
    return jsonify({"ok": True})


@app.route("/clear_key", methods=["POST"])
def clear_key():
    _RUNTIME_KEY["value"] = ""
    with _model_lock:
        _model_cache["key"] = None
        _model_cache["model"] = None
        _model_cache["agent"] = None
    return jsonify({"ok": True, "has_key": has_api_key()})


@app.route("/rebuild_vectors", methods=["POST", "GET"])
def rebuild_vectors():
    """清空 ChromaDB 重新索引（面试题 md 更新后手动触发）。本地调试用，GET/POST 均可。"""
    try:
        build_vector_store(force=True)
        return jsonify({"ok": True, "chunks": _collection_count(vector_store)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


# ================= 面试模式：两段式接口 =================
@app.route("/interview/get_question", methods=["POST"])
def interview_get_question():
    if not has_api_key():
        return jsonify({"error": "NO_API_KEY", "message": "还没有设置 DeepSeek API Key"}), 401
    d = request.get_json(silent=True) or {}
    prev = d.get("state") or {}
    adaptive = bool(d.get("adaptive") or prev.get("adaptive"))
    module = (d.get("minor") or prev.get("module") or "").strip()
    if adaptive and not module:
        module = _seed_adaptive_module()

    st = _fresh_interview_state(module, adaptive)
    # 延续本轮累计信息（若前端传了）
    st["history"] = prev.get("history", []) or []
    st["question_count"] = prev.get("question_count", 0) or 0
    st["weak_modules"] = prev.get("weak_modules", []) or []

    try:
        out = intake_graph.invoke(st)
    except ApiKeyError as e:
        return jsonify({"error": "API_KEY_INVALID", "message": str(e)}), 401
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": "出题失败：{}".format(_sanitize_error(e))}), 500
    return jsonify({"state": out, "question": out.get("question", ""),
                    "module": out.get("module", ""), "is_followup": False})


@app.route("/interview/submit_answer", methods=["POST"])
def interview_submit_answer():
    if not has_api_key():
        return jsonify({"error": "NO_API_KEY", "message": "还没有设置 DeepSeek API Key"}), 401
    d = request.get_json(silent=True) or {}
    st = d.get("state") or {}
    if not st.get("question"):
        return jsonify({"error": "没有进行中的题目"}), 400
    st["user_answer"] = (d.get("answer") or "").strip()
    try:
        out = grade_graph.invoke(st)
    except ApiKeyError as e:
        return jsonify({"error": "API_KEY_INVALID", "message": str(e)}), 401
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": "评分失败：{}".format(_sanitize_error(e))}), 500
    resp = {"state": out, "question": out.get("question", ""),
            "module": out.get("module", ""), "is_followup": bool(out.get("is_followup"))}
    if out.get("last_result"):
        resp["result"] = out["last_result"]
    if out.get("last_coach"):
        resp["coach"] = out["last_coach"]
    return jsonify(resp)


@app.route("/ask", methods=["POST"])
def ask():
    data = request.get_json(silent=True) or {}
    mode = data.get("mode") if data.get("mode") in ("interview", "learn") else "interview"
    major = (data.get("major") or "").strip()
    minor = (data.get("minor") or "").strip()
    answer = (data.get("answer") or "").strip()
    thread_id = (data.get("thread_id") or f"{mode}-default").strip()
    plain_talk = data.get("plain_talk", True)
    if not answer:
        return jsonify({"error": "空消息"}), 400
    if not has_api_key():
        return jsonify({"error": "NO_API_KEY", "message": "还没有设置 DeepSeek API Key"}), 401

    # 把模式 + 方向 + 讲解设置作为提示注入对话，Agent 逻辑与工具保持不变
    direction = f"{major}·{minor}" if major and minor else (major or minor or "未指定")
    head = "学习模式·当前学习方向" if mode == "learn" else "面试模式·当前面试方向"
    setting = "开" if plain_talk else "关"
    content = f"（{head}：{direction}）\n{answer}\n[讲解设置：大白话理解={setting}]"

    config = {"configurable": {"thread_id": thread_id}}
    # 用户选定的方向：让 search_interview_questions 只在这个模块的题里检索
    module_scope = minor if minor in ALL_MINORS else ""

    @stream_with_context
    def generate():
        _current_module.set(module_scope)
        try:
            for chunk, meta in _get_agent().stream(
                {"messages": [{"role": "user", "content": content}]},
                config=config,
                stream_mode="messages",
            ):
                # 只要 agent 节点里 LLM 生成的正文 token（工具调用那次 content 为空，自动跳过）
                if meta.get("langgraph_node") != "agent":
                    continue
                piece = getattr(chunk, "content", "")
                if isinstance(piece, list):
                    piece = "".join(p.get("text", "") for p in piece if isinstance(p, dict))
                if piece:
                    yield piece
        except ApiKeyError as e:
            yield f"\n（{e}，请在上方重新设置 API Key）"
        except Exception as e:  # noqa: BLE001
            yield "\n（出错了：{}）".format(_sanitize_error(e))
        finally:
            _current_module.set("")

    return Response(generate(), mimetype="text/plain",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _weak_groups(items):
    """把错题按 weak_tag 归类，返回 [{tag, count, items:[...]}]，多的排前面。"""
    groups = {}
    for it in items:
        tag = (it.get("weak_tag") or "").strip()
        if not tag:
            wp = (it.get("weak_points") or "").strip()
            tag = re.split(r"[；;，,。\n]", wp)[0][:12] if wp else "未分类"
            tag = tag or "未分类"
        groups.setdefault(tag, []).append(it)
    out = [{"tag": t, "count": len(v), "items": v} for t, v in groups.items()]
    out.sort(key=lambda g: (-g["count"], g["tag"]))
    return out


@app.route("/wrong")
def wrong():
    wrong_list = load_wrong_answers()
    mastered_list = load_mastered()
    return render_template("wrong.html",
                           wrong_list=wrong_list,
                           mastered_list=mastered_list,
                           weak_groups=_weak_groups(wrong_list))


@app.route("/master", methods=["POST"])
def master():
    data = request.get_json(silent=True) or {}
    q = (data.get("question") or "").strip()
    module = store.move_to_mastered(q)      # 错题 -> 已掌握（事务）；None = 没找到
    if module is None:
        return jsonify({"error": "未找到该错题"}), 404
    bump_activity(module, "mastered")
    return jsonify({"ok": True})


@app.route("/unmaster", methods=["POST"])
def unmaster():
    data = request.get_json(silent=True) or {}
    q = (data.get("question") or "").strip()
    if not store.move_to_wrong(q):          # 已掌握 -> 错题（事务）
        return jsonify({"error": "未找到"}), 404
    return jsonify({"ok": True})


# ================= 笔记接口 =================
@app.route("/notes")
def notes_get():
    mode, major, minor = (request.args.get("mode", ""), request.args.get("major", ""),
                          request.args.get("minor", ""))
    return jsonify({"key": _note_key(mode, major, minor),
                    "notes": store.notes_for(mode, major, minor)})


@app.route("/notes/all")
def notes_all():
    return jsonify(store.notes_all())


@app.route("/notes/save", methods=["POST"])
def notes_save():
    d = request.get_json(silent=True) or {}
    mode, major, minor = d.get("mode", ""), d.get("major", ""), d.get("minor", "")
    nid, updated = store.note_save(mode, major, minor, d.get("text", ""),
                                   (d.get("id") or "").strip())
    return jsonify({"ok": True, "id": nid, "updated": updated,
                    "key": _note_key(mode, major, minor)})


@app.route("/notes/delete", methods=["POST"])
def notes_delete():
    d = request.get_json(silent=True) or {}
    store.note_delete((d.get("id") or "").strip())
    return jsonify({"ok": True})


@app.route("/notes-page")
def notes_page():
    return render_template("notes.html", categories=CATEGORIES)


# ================= 统计接口 =================
@app.route("/event/answer", methods=["POST"])
def event_answer():
    data = request.get_json(silent=True) or {}
    minor = (data.get("minor") or "").strip()
    bump_activity(minor, "answered")
    return jsonify({"ok": True})


@app.route("/event/time", methods=["POST"])
def event_time():
    # 兼容 navigator.sendBeacon（text/plain）和普通 JSON
    try:
        data = json.loads(request.get_data() or b"{}")
    except (ValueError, TypeError):
        data = {}
    minor = (data.get("minor") or "").strip()
    seconds = data.get("seconds", 0)
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        seconds = 0
    if 0 < seconds <= 6 * 3600:
        add_study_time(minor, seconds)
    return jsonify({"ok": True})


@app.route("/progress")
def progress():
    wrong_by_mod = store.wrong_count_by_module()
    mastered_by_mod = store.mastered_count_by_module()
    answered_by_mod = {}
    for day in store.activity_map().values():
        for mod, c in day.items():
            answered_by_mod[mod] = answered_by_mod.get(mod, 0) + c.get("answered", 0)
    out = {}
    for major, minors in CATEGORIES.items():
        for minor in minors:
            total = len(list_topics(major, minor))
            answered = answered_by_mod.get(minor, 0)
            out[minor] = {
                "total": total,
                "answered": answered,
                "todo": max(0, total - answered),
                "wrong": wrong_by_mod.get(minor, 0),
                "mastered": mastered_by_mod.get(minor, 0),
            }
    return jsonify(out)


@app.route("/study-time")
def study_time():
    data = store.study_map()
    by_module, by_date = {}, {}
    for day, mods in data.items():
        by_date[day] = round(sum(mods.values()))
        for mod, secs in mods.items():
            by_module[mod] = round(by_module.get(mod, 0) + secs)
    total = round(sum(by_date.values()))
    by_module = dict(sorted(by_module.items(), key=lambda kv: -kv[1]))
    by_date = dict(sorted(by_date.items()))
    return jsonify({"total": total, "byModule": by_module, "byDate": by_date})


@app.route("/study")
def study_page():
    return render_template("study.html")


@app.route("/daily-summary")
def daily_summary():
    generate_daily_summaries()
    return jsonify({"pending": store.pending_daily()})


@app.route("/daily-summary/seen", methods=["POST"])
def daily_summary_seen():
    data = request.get_json(silent=True) or {}
    store.mark_daily_seen(data.get("dates") or [])
    return jsonify({"ok": True})


if __name__ == "__main__":
    if "--rebuild" in sys.argv:
        print("--rebuild：清空并重建向量库…")
        build_vector_store(force=True)
        print("重建完成。")
        sys.exit(0)
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
