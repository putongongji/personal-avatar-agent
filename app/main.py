from __future__ import annotations

import argparse
import hashlib
import json
import math
import mimetypes
import os
import random
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = PROJECT_ROOT / "app" / "static"
DATA_DIR = (
    Path(os.environ["PAA_DATA_DIR"])
    if os.environ.get("PAA_DATA_DIR")
    else Path("/tmp/personal-avatar-agent")
    if os.environ.get("VERCEL")
    else PROJECT_ROOT / "data"
)
DB_PATH = DATA_DIR / "avatar_agent.sqlite3"
EMBEDDING_CACHE_PATH = DATA_DIR / "embeddings.json"
SEED_EMBEDDING_CACHE_PATH = PROJECT_ROOT / "knowledge" / "generated" / "embeddings-bge-m3.json"
SOURCE_REGISTRY_PATH = PROJECT_ROOT / "knowledge" / "sources" / "source-registry.json"
ENV_PATH = PROJECT_ROOT / ".env"
SESSION_COOKIE_NAME = "paa_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 365

MAX_CHUNK_CHARS = 1300
MIN_SCORE = 1.8
LLM_TIMEOUT_SECONDS = 45
EMBEDDING_TIMEOUT_SECONDS = 90
CAREER_SOURCE_ID = "career_panorama"
DEFAULT_RETRIEVAL_LIMIT = 6
CLASSIFICATION_TIMEOUT_SECONDS = 8
CLASSIFICATION_CATEGORIES = {
    "career_experience",
    "project_case",
    "ai_pm_fit",
    "iot_hardware",
    "personal_style",
    "risk_boundary",
    "meta_question",
    "general",
    "unrelated",
}

INITIAL_SUGGESTIONS = [
    "请用 1 分钟介绍一下他。",
    "他适合什么类型的 AI 产品经理岗位？",
    "他有哪些真实 AI 产品经验？",
    "他做过哪些智能硬件和 IoT 项目？",
    "他和普通产品经理相比有什么差异？",
    "如果我是面试官，应该重点追问他什么？",
]

FOLLOWUP_SUGGESTIONS: dict[str, list[str]] = {
    "ai_pm_fit": [
        "他的 AI 产品经验里哪些是公司项目，哪些是个人实践？",
        "他做 AI 产品最核心的方法论是什么？",
        "如果面试 AI 产品经理，应该追问他哪些问题？",
    ],
    "iot_hardware": [
        "他在智能门锁项目里具体负责哪些部分？",
        "他对 IoT 设备平台的理解体现在哪里？",
        "这些智能硬件经验如何迁移到 AI 产品岗位？",
    ],
    "career_experience": [
        "他的职业主线可以怎么概括？",
        "他在哪些公司做过什么产品？",
        "他的经历里最能证明产品判断力的是哪一段？",
    ],
    "project_case": [
        "哪个项目最适合放进简历重点讲？",
        "这些项目的风险口径有哪些？",
        "如果要讲一个代表案例，应该选哪个？",
    ],
    "general": [
        "他的核心优势是什么？",
        "他适合什么类型的团队？",
        "用招聘方视角看，他最值得追问什么？",
    ],
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_local_env() -> None:
    if not ENV_PATH.exists():
        return
    for raw_line in ENV_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_value(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def admin_email() -> str:
    return env_value("ADMIN_EMAIL", "li-ten@foxmali.com").lower()


def admin_password() -> str:
    return env_value("ADMIN_PASSWORD")


def selected_llm_provider() -> str:
    return "minimax" if env_value("MINIMAX_API_KEY") else "local"


def embedding_configured() -> bool:
    return bool(embedding_api_key())


def embedding_api_key() -> str:
    return (
        env_value("SILICONFLOW_API_KEY")
        or env_value("api_key")
        or ""
    )


def embedding_model() -> str:
    return env_value("EMBEDDING_MODEL", "BAAI/bge-m3")


def embedding_base_url() -> str:
    return env_value(
        "SILICONFLOW_BASE_URL",
        env_value("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1"),
    ).rstrip("/")


def siliconflow_base_url() -> str:
    return env_value("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/")


def classification_model() -> str:
    return env_value("CLASSIFICATION_MODEL", "Qwen/Qwen3-8B")


def classification_fallback_models() -> list[str]:
    configured = env_value("CLASSIFICATION_FALLBACK_MODELS", "Qwen/Qwen3-8B,Qwen/Qwen2.5-7B-Instruct")
    models = [item.strip() for item in configured.split(",") if item.strip()]
    return [model for model in models if model != classification_model()]


def sanitize_error(message: str) -> str:
    compact = re.sub(r"sk-[A-Za-z0-9*_\\-]+", "sk-***", message)
    compact = re.sub(r"SJS[A-Za-z0-9*_\\-]+", "SJS***", compact)
    compact = re.sub(r"AIza[A-Za-z0-9*_\\-]+", "AIza***", compact)
    return compact[:240]


def strip_thinking(text: str) -> str:
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL | re.IGNORECASE).strip()


def remove_thinking_for_stream(text: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL | re.IGNORECASE)
    open_match = re.search(r"<think>", cleaned, flags=re.IGNORECASE)
    if open_match:
        cleaned = cleaned[: open_match.start()]
    return cleaned


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                sources_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS eval_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                question TEXT NOT NULL,
                expected_answer TEXT NOT NULL DEFAULT '',
                expected_sources_json TEXT NOT NULL DEFAULT '[]',
                actual_answer TEXT NOT NULL DEFAULT '',
                actual_sources_json TEXT NOT NULL DEFAULT '[]',
                category TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new',
                severity TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                source_question_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        eval_columns = {row[1] for row in conn.execute("PRAGMA table_info(eval_cases)").fetchall()}
        eval_column_defs = {
            "score": "REAL NOT NULL DEFAULT 0",
            "details_json": "TEXT NOT NULL DEFAULT '{}'",
            "last_run_at": "TEXT NOT NULL DEFAULT ''",
            "latency_ms": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, definition in eval_column_defs.items():
            if column not in eval_columns:
                conn.execute(f"ALTER TABLE eval_cases ADD COLUMN {column} {definition}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question TEXT NOT NULL,
                user_type TEXT NOT NULL,
                category TEXT NOT NULL,
                answer TEXT NOT NULL,
                sources_json TEXT NOT NULL,
                quality_score REAL NOT NULL,
                was_answered INTEGER NOT NULL,
                missing_info TEXT NOT NULL,
                answer_provider TEXT NOT NULL DEFAULT 'local_template',
                feedback TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new',
                created_at TEXT NOT NULL
            )
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(questions)").fetchall()}
        question_columns = {
            "user_id": "TEXT NOT NULL DEFAULT ''",
            "user_display_name": "TEXT NOT NULL DEFAULT ''",
            "user_role": "TEXT NOT NULL DEFAULT ''",
            "answer_provider": "TEXT NOT NULL DEFAULT 'local_template'",
            "conversation_id": "TEXT NOT NULL DEFAULT ''",
            "message_id": "INTEGER NOT NULL DEFAULT 0",
            "classification_provider": "TEXT NOT NULL DEFAULT ''",
            "classification_reason": "TEXT NOT NULL DEFAULT ''",
            "rewritten_question": "TEXT NOT NULL DEFAULT ''",
            "retrieval_method": "TEXT NOT NULL DEFAULT ''",
            "retrieval_stats_json": "TEXT NOT NULL DEFAULT '{}'",
            "retrieved_chunks_json": "TEXT NOT NULL DEFAULT '[]'",
            "conversation_turn_count": "INTEGER NOT NULL DEFAULT 0",
            "latency_ms": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, definition in question_columns.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE questions ADD COLUMN {column} {definition}")
        conversation_columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()}
        if "user_id" not in conversation_columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
        message_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "user_id" not in message_columns:
            conn.execute("ALTER TABLE messages ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
        conn.commit()


def load_registry() -> list[dict[str, Any]]:
    if not SOURCE_REGISTRY_PATH.exists():
        return []
    data = json.loads(SOURCE_REGISTRY_PATH.read_text(encoding="utf-8"))
    return [item for item in data.get("sources", []) if item.get("enabled", True)]


def normalize_path(path_value: str) -> Path:
    path = Path(os.path.expandvars(path_value)).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def source_path_for(source: dict[str, Any]) -> Path:
    if source.get("id") == CAREER_SOURCE_ID and env_value("CAREER_ARCHIVE_PATH"):
        return normalize_path(env_value("CAREER_ARCHIVE_PATH"))
    return normalize_path(source["path"])


def split_markdown(text: str, source: dict[str, Any]) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    heading_stack: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        content = "\n".join(line for line in buffer).strip()
        buffer.clear()
        if not is_meaningful_chunk(content):
            return
        while len(content) > MAX_CHUNK_CHARS:
            cut = content.rfind("\n", 0, MAX_CHUNK_CHARS)
            if cut < 450:
                cut = MAX_CHUNK_CHARS
            emit = content[:cut].strip()
            content = content[cut:].strip()
            if emit:
                chunks.append(build_chunk(emit, source, heading_stack))
        if content:
            chunks.append(build_chunk(content, source, heading_stack))

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if match:
            flush()
            level = len(match.group(1))
            title = match.group(2).strip()
            heading_stack = heading_stack[: level - 1] + [title]
            buffer.append(line)
        else:
            buffer.append(line)
    flush()
    return chunks


def is_meaningful_chunk(content: str) -> bool:
    if not content:
        return False
    body = re.sub(r"^#{1,6}\s+.+$", "", content, flags=re.MULTILINE)
    body = re.sub(r"\s+", "", body)
    return len(body) >= 18


def build_chunk(content: str, source: dict[str, Any], heading_stack: list[str]) -> dict[str, Any]:
    heading_path = " > ".join(heading_stack) if heading_stack else source["title"]
    source_path = source_path_for(source)
    digest = hashlib.sha256(f"{source['id']}\n{heading_path}\n{content}".encode("utf-8")).hexdigest()
    parent_heading = heading_stack[1] if len(heading_stack) > 1 else (heading_stack[0] if heading_stack else source["title"])
    section_title = heading_stack[-1] if heading_stack else source["title"]
    return {
        "chunk_id": f"{source['id']}:{digest[:20]}",
        "source_id": source["id"],
        "source_title": source["title"],
        "source_path": str(source_path),
        "heading_path": heading_path,
        "parent_heading": parent_heading,
        "section_title": section_title,
        "section_kind": infer_section_kind(section_title),
        "is_personal_practice": "个人实践" in heading_path or "个人实践" in content,
        "is_pending": any(marker in heading_path or marker in content for marker in ["待核实", "需核实", "建议确认"]),
        "access": source.get("access", "public"),
        "category": source.get("category", "general"),
        "confirmed": bool(source.get("confirmed", False)),
        "content": content,
    }


def infer_section_kind(title: str) -> str:
    if "定位" in title:
        return "positioning"
    if "职责" in title:
        return "responsibility"
    if "关键产品判断" in title or "判断" in title:
        return "judgment"
    if "关键设计" in title or "设计" in title:
        return "design"
    if "结果" in title or "证据" in title:
        return "evidence"
    if "能力" in title:
        return "capability"
    if "口径" in title or "核实" in title:
        return "risk_boundary"
    if "叙事" in title or "索引" in title:
        return "narrative"
    return "general"


def build_index() -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for source in load_registry():
        if source.get("id") != CAREER_SOURCE_ID:
            continue
        path = source_path_for(source)
        if path.exists() and path.suffix.lower() in {".md", ".txt"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
        elif source.get("id") == CAREER_SOURCE_ID:
            text = bundled_career_archive()
        else:
            continue
        if not text:
            continue
        chunks.extend(split_markdown(text, source))
    return chunks


def bundled_career_archive() -> str:
    try:
        from app.bundled_knowledge import CAREER_PANORAMA_MD
    except Exception:
        return ""
    return CAREER_PANORAMA_MD


INDEX: list[dict[str, Any]] = []
EMBEDDING_CACHE: dict[str, list[float]] = {}
EMBEDDING_READY = False
EMBEDDING_LAST_ERROR = ""


def refresh_index() -> dict[str, int]:
    global INDEX, EMBEDDING_CACHE, EMBEDDING_READY, EMBEDDING_LAST_ERROR
    EMBEDDING_LAST_ERROR = ""
    INDEX = build_index()
    EMBEDDING_CACHE = load_embedding_cache()
    EMBEDDING_READY = ensure_index_embeddings(INDEX)
    return {"sources": len(load_registry()), "chunks": len(INDEX), "embeddings": indexed_embedding_count()}


def indexed_embedding_count() -> int:
    return sum(1 for chunk in INDEX if is_vector(chunk.get("embedding")))


def load_embedding_cache() -> dict[str, list[float]]:
    cache_path = EMBEDDING_CACHE_PATH if EMBEDDING_CACHE_PATH.exists() else SEED_EMBEDDING_CACHE_PATH
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if data.get("model") != embedding_model():
        return {}
    vectors = data.get("vectors", {})
    if not isinstance(vectors, dict):
        return {}
    return {key: value for key, value in vectors.items() if is_vector(value)}


def save_embedding_cache() -> None:
    if not EMBEDDING_CACHE:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"model": embedding_model(), "vectors": EMBEDDING_CACHE, "updated_at": now_iso()}
    EMBEDDING_CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def is_vector(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(item, (int, float)) for item in value)


def embedding_text(chunk: dict[str, Any]) -> str:
    return f"{chunk['source_title']} > {chunk['heading_path']}\n{chunk['content']}"


def ensure_index_embeddings(chunks: list[dict[str, Any]]) -> bool:
    global EMBEDDING_LAST_ERROR
    if not embedding_configured() or not chunks:
        attach_cached_embeddings(chunks)
        return False

    missing = [chunk for chunk in chunks if chunk["chunk_id"] not in EMBEDDING_CACHE]
    for start in range(0, len(missing), 16):
        batch = missing[start : start + 16]
        texts = [embedding_text(chunk) for chunk in batch]
        try:
            vectors = call_embedding_api(texts)
        except Exception as exc:
            EMBEDDING_LAST_ERROR = sanitize_error(str(exc))
            attach_cached_embeddings(chunks)
            return False
        for chunk, vector in zip(batch, vectors):
            EMBEDDING_CACHE[chunk["chunk_id"]] = vector
    save_embedding_cache()
    attach_cached_embeddings(chunks)
    return all(is_vector(chunk.get("embedding")) for chunk in chunks)


def attach_cached_embeddings(chunks: list[dict[str, Any]]) -> None:
    for chunk in chunks:
        vector = EMBEDDING_CACHE.get(chunk["chunk_id"])
        if vector:
            chunk["embedding"] = vector


def tokenize(text: str) -> list[str]:
    lower = text.lower()
    words = re.findall(r"[a-z0-9]+(?:[-_][a-z0-9]+)?", lower)
    chinese = re.findall(r"[\u4e00-\u9fff]", lower)
    bigrams = [a + b for a, b in zip(chinese, chinese[1:])]
    return words + chinese + bigrams


QUESTION_CATEGORIES: dict[str, list[str]] = {
    "career_experience": ["经历", "公司", "工作", "履历", "时间线", "职责", "负责", "任职", "介绍", "1 分钟", "一分钟"],
    "project_case": ["项目", "案例", "产品线", "代表", "成果", "指标", "做过", "落地"],
    "ai_pm_fit": ["ai", "agent", "rag", "产品经理", "适合", "匹配", "岗位", "能力", "优势"],
    "iot_hardware": ["iot", "物联网", "智能硬件", "门锁", "设备", "硬件", "售后", "工单", "平台"],
    "personal_style": ["风格", "协作", "偏好", "性格", "判断", "表达", "价值观", "工作方式"],
    "risk_boundary": ["隐私", "薪资", "联系方式", "住址", "身份证", "手机号", "家庭", "日记"],
    "meta_question": ["来源", "依据", "你是谁", "这个agent", "怎么回答", "资料"],
}

CAREER_QUERY_HINTS = [
    "他", "你", "叁金", "李鑫", "简历", "职业", "经历", "工作", "项目", "产品", "产品经理",
    "ai", "agent", "rag", "iot", "物联网", "智能硬件", "门锁", "工单", "售后", "能力",
    "优势", "岗位", "匹配", "面试", "公司", "负责", "成果", "案例", "技能",
]

UNRELATED_HINTS = [
    "天气", "股票", "汇率", "新闻", "菜谱", "旅游", "电影", "数学题", "代码怎么写",
    "翻译", "历史", "世界杯", "nba", "今天几号", "几点",
]

CATEGORY_QUERY_EXPANSIONS: dict[str, list[str]] = {
    "ai_pm_fit": ["AI 应用产品经理", "工单 AI", "AI 对话", "业务流程型 AI PM", "业务流程", "标注样本", "人工确认", "核心优势"],
    "iot_hardware": ["智能门锁", "IoT 设备管理", "物模型", "OTA", "FSM", "售后", "硬件", "设备平台"],
    "career_experience": ["职业主线", "时间线", "方得科技", "全民认证", "职责", "产品线"],
    "project_case": ["项目案例", "产品线", "关键工作", "关键产品判断", "结果", "证据"],
}


def classify_question(question: str) -> str:
    return classify_question_by_rules(question)


def is_unrelated_question(question: str) -> bool:
    if any(hint in question for hint in CAREER_QUERY_HINTS):
        return False
    return any(hint in question for hint in UNRELATED_HINTS)


def classify_question_with_llm(question: str, history: list[dict[str, Any]] | None = None) -> dict[str, str]:
    fallback_category = classify_question_by_rules(question)
    if not embedding_api_key():
        return {
            "category": fallback_category,
            "rewritten_question": question,
            "reason": "硅基流动 API key 未配置，使用规则分类。",
            "provider": "rule_fallback",
        }
    prompt = build_classification_prompt(question, history or [])
    errors: list[str] = []
    for model in [classification_model(), *classification_fallback_models()]:
        try:
            content = call_siliconflow_chat(
                prompt,
                model,
                max_tokens=360,
                temperature=0,
                timeout=CLASSIFICATION_TIMEOUT_SECONDS,
            )
            if not content:
                raise RuntimeError("SiliconFlow returned empty classification response")
            parsed = parse_json_object(content)
            category = str(parsed.get("category", fallback_category)).strip()
            if category not in CLASSIFICATION_CATEGORIES:
                category = fallback_category
            if category == "general" and fallback_category != "general":
                category = fallback_category
            rewritten = str(parsed.get("rewritten_question", question)).strip() or question
            reason = str(parsed.get("reason", "")).strip()[:300]
            if errors:
                reason = (reason + f"；备用模型接管，主模型错误：{errors[0]}").strip("；")[:300]
            return {
                "category": category,
                "rewritten_question": rewritten[:800],
                "reason": reason,
                "provider": f"siliconflow:{model}",
            }
        except Exception as exc:
            errors.append(f"{model}: {sanitize_error(str(exc))}")

    detail = "；".join(errors[:2]) if errors else "未知错误"
    return {
        "category": fallback_category,
        "rewritten_question": question,
        "reason": f"LLM 分类失败，使用规则分类：{sanitize_error(detail)}",
        "provider": "rule_fallback",
    }


def build_classification_prompt(question: str, history: list[dict[str, Any]]) -> str:
    history_lines = []
    for item in history[-6:]:
        role = "用户" if item.get("role") == "user" else "Agent"
        content = str(item.get("content", "")).strip().replace("\n", " ")
        if content:
            history_lines.append(f"{role}: {content[:240]}")
    history_text = "\n".join(history_lines) if history_lines else "无"
    return f"""你是个人职业档案问答系统的问题分类器。请只输出 JSON，不要输出解释。

可选 category：
- career_experience：职业经历、公司、时间线、职责、履历
- project_case：项目案例、产品线、成果、指标、落地过程
- ai_pm_fit：AI 产品经理匹配、岗位、优势、能力、面试卖点
- iot_hardware：IoT、智能硬件、门锁、设备平台、售后、工单
- personal_style：协作风格、工作方式、价值观、表达风格
- risk_boundary：隐私、联系方式、家庭、薪资底线、私人日记等不应公开回答的问题
- meta_question：问资料来源、系统如何回答、引用依据、这个 Agent 的工作方式
- general：关于李鑫但无法归入以上类别的一般问题
- unrelated：天气、股票、旅游、代码、通用知识等与李鑫职业档案无关的问题

任务：
1. 判断 category。
2. 结合对话上下文，把当前问题改写成适合检索职业档案的完整问题 rewritten_question；如果当前问题已经完整，原样返回。
3. reason 用一句话说明分类依据。

对话上下文：
{history_text}

当前问题：
{question}

输出 JSON 格式：
{{"category":"...","rewritten_question":"...","reason":"..."}}
"""


def classify_question_by_rules(question: str) -> str:
    q = question.lower()
    if is_unrelated_question(q):
        return "unrelated"
    scores: dict[str, int] = {}
    for category, keywords in QUESTION_CATEGORIES.items():
        scores[category] = sum(1 for keyword in keywords if keyword in q)
    top_category, top_score = max(scores.items(), key=lambda item: item[1])
    return top_category if top_score > 0 else "general"


def retrieve(question: str, category: str, limit: int = 6) -> list[dict[str, Any]]:
    chunks, _stats = retrieve_with_stats(question, category, limit)
    return chunks


def retrieve_with_stats(question: str, category: str, limit: int = 6) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    expanded_question = question + " " + " ".join(CATEGORY_QUERY_EXPANSIONS.get(category, []))
    q_tokens = Counter(tokenize(expanded_question))
    query_vector = embed_query(expanded_question)
    if not q_tokens and not query_vector:
        return [], {"index_count": len(INDEX), "candidate_count": 0, "returned_count": 0, "method": "none"}
    results: list[dict[str, Any]] = []
    method = "vector" if query_vector else "lexical"
    for chunk in INDEX:
        lexical_score = score_lexical(q_tokens, chunk, category) if q_tokens else 0.0
        vector_score = cosine_similarity(query_vector, chunk.get("embedding")) if query_vector else None
        if vector_score is not None:
            category_boost = 1.08 if category_matches_source(category, chunk["category"]) else 1.0
            confirmed_boost = 1.03 if chunk["confirmed"] else 1.0
            metadata_boost = metadata_retrieval_boost(question, category, chunk)
            score = (vector_score * 100 + min(lexical_score, 20) * 0.35) * category_boost * confirmed_boost * metadata_boost
            retrieval_method = "vector"
        else:
            score = lexical_score * metadata_retrieval_boost(question, category, chunk)
            retrieval_method = "lexical"
        if score <= 0:
            continue
        safe_chunk = {key: value for key, value in chunk.items() if key != "embedding"}
        results.append({**safe_chunk, "score": round(score, 3), "retrieval_method": retrieval_method})
    results.sort(key=lambda item: item["score"], reverse=True)
    selected = diversify_results(results, limit)
    selected = pin_required_chunks(question, category, results, selected, limit)
    return selected, {
        "index_count": len(INDEX),
        "candidate_count": len(results),
        "returned_count": len(selected),
        "method": selected[0]["retrieval_method"] if selected else method,
    }


def metadata_retrieval_boost(question: str, category: str, chunk: dict[str, Any]) -> float:
    q = question.lower()
    boost = 1.0
    section_kind = chunk.get("section_kind", "")
    if category == "ai_pm_fit" and section_kind in {"capability", "judgment", "narrative"}:
        boost *= 1.08
    if category == "career_experience" and section_kind in {"positioning", "responsibility", "capability"}:
        boost *= 1.04
    if category == "career_experience" and "时间线" in str(chunk.get("parent_heading", "")):
        boost *= 1.14
    if category == "career_experience" and section_kind == "responsibility":
        boost *= 1.08
    asks_agent = any(token in q for token in ["agent", "rag", "知识库", "langgraph", "个人实践"])
    if chunk.get("is_personal_practice") and not asks_agent:
        boost *= 0.9
    asks_risk = any(token in q for token in ["核实", "风险", "口径", "数据", "指标", "夸大", "真实吗"])
    if chunk.get("is_pending") and not asks_risk:
        boost *= 0.84
    parent = str(chunk.get("parent_heading", ""))
    heading = str(chunk.get("heading_path", ""))
    if "全民认证" in question:
        boost *= 1.12 if "全民认证" in parent or "全民认证" in heading else 0.86
    if "方得" in question or "方得科技" in question:
        boost *= 1.12 if "方得" in parent or "方得" in heading else 0.86
    return boost


def diversify_results(results: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    parent_counts: Counter[str] = Counter()
    for item in results:
        parent = item.get("parent_heading") or item.get("heading_path", "")
        if parent_counts[parent] >= 2 and len(selected) < max(3, limit - 1):
            continue
        selected.append(item)
        parent_counts[parent] += 1
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        selected_ids = {item["chunk_id"] for item in selected}
        for item in results:
            if item["chunk_id"] not in selected_ids:
                selected.append(item)
                if len(selected) >= limit:
                    break
    return selected


def pin_required_chunks(
    question: str,
    category: str,
    results: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    q = question.lower()
    pinned: list[dict[str, Any]] = []
    if category == "career_experience" and any(token in q for token in ["经历", "履历", "公司", "时间", "职业"]):
        pinned = [item for item in results if "时间线" in item.get("parent_heading", "")][:3]
    if not pinned:
        return selected
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in pinned + selected:
        if item["chunk_id"] in seen:
            continue
        merged.append(item)
        seen.add(item["chunk_id"])
        if len(merged) >= limit:
            break
    return merged


def score_lexical(q_tokens: Counter[str], chunk: dict[str, Any], category: str) -> float:
    heading_tokens = Counter(tokenize(chunk["heading_path"]))
    content_tokens = Counter(tokenize(chunk["content"]))
    combined_tokens = content_tokens + Counter({token: count * 2 for token, count in heading_tokens.items()})
    heading_overlap = sum(min(q_tokens[t], heading_tokens.get(t, 0)) for t in q_tokens)
    combined_overlap = sum(min(q_tokens[t], combined_tokens.get(t, 0)) for t in q_tokens)
    if combined_overlap == 0:
        return 0.0
    unique_overlap = len([t for t in q_tokens if combined_tokens.get(t, 0)])
    query_coverage = unique_overlap / max(1, len(q_tokens))
    category_boost = 1.35 if category_matches_source(category, chunk["category"]) else 1.0
    confirmed_boost = 1.1 if chunk["confirmed"] else 1.0
    heading_boost = 1 + min(0.8, heading_overlap * 0.08)
    score = (combined_overlap + unique_overlap * 0.65 + query_coverage * 3) * category_boost * confirmed_boost * heading_boost
    return score / math.sqrt(max(1, len(content_tokens) / 160))


def embed_query(text: str) -> list[float] | None:
    if not EMBEDDING_READY:
        return None
    try:
        vectors = call_embedding_api([text])
    except Exception:
        return None
    return vectors[0] if vectors else None


def cosine_similarity(left: Any, right: Any) -> float | None:
    if not is_vector(left) or not is_vector(right) or len(left) != len(right):
        return None
    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(a) * float(a) for a in left))
    right_norm = math.sqrt(sum(float(b) * float(b) for b in right))
    if left_norm == 0 or right_norm == 0:
        return None
    return dot / (left_norm * right_norm)


def category_matches_source(question_category: str, source_category: str) -> bool:
    if question_category == "ai_pm_fit" and source_category in {"career", "avatar", "project"}:
        return True
    if question_category == "project_case" and source_category in {"career", "project"}:
        return True
    if question_category == "personal_style" and source_category == "avatar":
        return True
    if question_category == "iot_hardware" and source_category in {"career", "project"}:
        return True
    return question_category.startswith(source_category)


def excerpt(text: str, length: int = 220) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact[:length] + ("..." if len(compact) > length else "")


def best_source_sentence(question: str, content: str) -> str:
    cleaned_lines = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^(?:[-*]\s+|\d+[.、]\s+)", "", line)
        line = re.sub(r"[*_`]+", "", line).strip()
        if line:
            cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)
    sentences = []
    for sentence in re.split(r"(?<=[。！？；.!?])\s+|\n+", cleaned):
        sentence = sentence.strip(" -\n\t")
        if not sentence or len(sentence) < 12 or sentence.endswith(("：", ":")):
            continue
        sentences.append(sentence)
    if not sentences:
        return excerpt(content, 160)
    q_tokens = Counter(tokenize(question))
    if not q_tokens:
        return excerpt(sentences[0], 180)
    scored = []
    for sentence in sentences:
        s_tokens = Counter(tokenize(sentence))
        overlap = sum(min(q_tokens[token], s_tokens.get(token, 0)) for token in q_tokens)
        scored.append((overlap, len(sentence), sentence))
    scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    return excerpt(scored[0][2], 180)


def build_sources(chunks: list[dict[str, Any]], question: str = "") -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    sources: list[dict[str, Any]] = []
    for chunk in chunks:
        key = (chunk["source_id"], chunk["heading_path"])
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "source_id": chunk["source_id"],
                "title": chunk["source_title"],
                "heading": chunk["heading_path"],
                "parent_heading": chunk.get("parent_heading", ""),
                "section_kind": chunk.get("section_kind", ""),
                "is_personal_practice": bool(chunk.get("is_personal_practice")),
                "is_pending": bool(chunk.get("is_pending")),
                "path": chunk["source_path"],
                "access": chunk["access"],
                "confirmed": chunk["confirmed"],
                "score": chunk["score"],
                "retrieval_method": chunk.get("retrieval_method", "lexical"),
                "excerpt": excerpt(chunk["content"]),
                "quote": best_source_sentence(question, chunk["content"]),
            }
        )
    return sources


def summarize_retrieved_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary = []
    for index, chunk in enumerate(chunks, start=1):
        summary.append(
            {
                "rank": index,
                "chunk_id": chunk["chunk_id"],
                "source_id": chunk["source_id"],
                "heading": chunk["heading_path"],
                "score": chunk.get("score", 0),
                "retrieval_method": chunk.get("retrieval_method", ""),
                "excerpt": excerpt(chunk["content"], 180),
            }
        )
    return summary


def generate_answer(question: str, category: str, chunks: list[dict[str, Any]]) -> tuple[str, str, int, str]:
    if category == "unrelated":
        return (
            "这个问题不属于李鑫职业经历、项目经验、能力判断或求职相关信息的范围，我不能基于职业生涯档案回答。\n\n"
            "你可以改问他的职业经历、项目案例、AI 产品经验、IoT / 智能硬件经验、岗位匹配或面试追问。",
            "问题与李鑫职业资料无关。",
            0,
            "policy",
        )

    if category == "risk_boundary":
        return (
            "这个问题可能涉及隐私或未授权信息，我不能回答。\n\n"
            "当前系统只回答职业经历、项目案例、能力判断和岗位匹配相关内容，不回答联系方式、住址、身份证、私人日记、薪资底线等敏感信息。",
            "问题涉及隐私或权限边界。",
            0,
            "policy",
        )

    if not chunks or chunks[0]["score"] < MIN_SCORE:
        return (
            "职业生涯档案里没有检索到足够相关的依据，因此不能可靠回答这个问题。",
            "没有检索到足够相关的公开资料。",
            0,
            "retrieval_guard",
        )

    try:
        answer, provider = generate_llm_answer(question, category, chunks)
        if answer:
            return answer, "", 1, provider
    except Exception as exc:
        fallback_answer = generate_template_answer(question, category, chunks)
        safe_error = sanitize_error(str(exc))
        return (
            fallback_answer
            + f"\n\n系统备注：LLM 调用失败，已回退到本地资料摘要。错误：{safe_error}",
            f"LLM 调用失败：{safe_error}",
            1,
            "local_template_fallback",
        )

    return generate_template_answer(question, category, chunks), "", 1, "local_template"


def generate_template_answer(question: str, category: str, chunks: list[dict[str, Any]]) -> str:

    source_lines = []
    evidence_lines = []
    for index, chunk in enumerate(chunks[:4], start=1):
        evidence_lines.append(f"{index}. {excerpt(chunk['content'], 260)}")
        source_lines.append(f"{index}. {chunk['source_title']} > {chunk['heading_path']}")

    category_intro = {
        "career_experience": "这个问题主要涉及职业经历。现有资料显示：",
        "project_case": "这个问题主要涉及项目案例。现有资料里最相关的信息是：",
        "ai_pm_fit": "这个问题主要涉及 AI 产品经理匹配度。需要把已确认事实和推断分开看：",
        "iot_hardware": "这个问题主要涉及智能硬件 / IoT / 设备平台经验。现有资料显示：",
        "personal_style": "这个问题主要涉及个人工作方式或协作偏好。现有资料显示：",
        "meta_question": "这个问题主要涉及资料来源或回答方式。系统依据如下：",
        "general": "根据当前资料库，相关信息如下：",
    }.get(category, "根据当前资料库，相关信息如下：")

    answer = (
        f"结论：{category_intro}\n\n"
        "依据：\n"
        + "\n".join(evidence_lines)
        + "\n\n来源：\n"
        + "\n".join(source_lines)
        + "\n\n如果问题涉及更精确的数字、时间范围或归因关系，需要继续查看档案中的核实说明。"
    )
    return answer


def generate_llm_answer(question: str, category: str, chunks: list[dict[str, Any]]) -> tuple[str, str]:
    provider = selected_llm_provider()
    if provider == "local":
        return "", "local_template"
    prompt = build_llm_user_prompt(question, category, chunks)
    model = env_value("MINIMAX_MODEL", "MiniMax-M2.7")
    return strip_thinking(call_minimax(prompt)), f"minimax:{model}"


AVATAR_SYSTEM_PROMPT = """你是“李鑫职业生涯档案问答 Agent”。你的唯一任务是：根据用户提供的《职业生涯全景档案》片段，回答用户关于李鑫职业经历、项目经验、能力、岗位匹配、面试追问、简历表达的问题。

资料边界：
- 只能使用用户消息中的资料区内容回答，不要补充资料外事实。
- 如果用户问的问题与李鑫本人、职业经历、项目经验、能力或求职表达无关，直接拒绝回答。
- 不回答联系方式、住址、身份证、私人日记、家庭、薪资底线等隐私或未授权信息。
- 必须区分：公司项目 / 个人实践 / 待核实口径 / 你的推断。
- “知识库 Agent / LangGraph RAG”如果出现，只能按资料写为个人实践或补充素材，不能包装成公司上线项目。
- 待核实素材只能谨慎引用，并说明“资料中标注为待核实”，不要当作确定成果。

回答目标：
- 回答要像一个懂招聘和产品经理评估的人，而不是摘要工具。
- 先给直接结论，再给证据和解释。
- 根据问题决定详略：用户问“有哪些/为什么/适合什么”时，要全面；用户问单点事实时，要短。
- 不要机械堆砌原文，要把资料转化成清晰判断。
- 不要每次都写“当前资料无法确认”。只有以下情况才写“无法确认/需核实”：用户明确问了资料里没有的信息；检索片段里出现“待核实/需核实/建议确认”等口径；回答中涉及指标、归因、上线范围、时间周期等容易被追问的内容。

输出格式：
- 用 Markdown 输出。
- 一般结构是“## 结论”“## 依据”“## 说明”，但如果问题很简单，可以更短。
- 依据部分用 2-5 条说明，每条都要自然引用来源编号，例如 [S1]、[S2]。
- 不要输出“来源”章节的重复列表，因为来源由前端悬浮引用显示。
"""


def build_llm_user_prompt(
    question: str,
    category: str,
    chunks: list[dict[str, Any]],
    history: list[dict[str, Any]] | None = None,
    rewritten_question: str = "",
) -> str:
    source_blocks = []
    for index, chunk in enumerate(chunks[:6], start=1):
        source_blocks.append(
            "\n".join(
                [
                    f"[S{index}]",
                    f"标题：{chunk['source_title']} > {chunk['heading_path']}",
                    f"确认事实：{'是' if chunk['confirmed'] else '否'}",
                    f"资料：{chunk['content']}",
                ]
            )
        )
    history_lines = []
    for item in (history or [])[-6:]:
        role = "用户" if item.get("role") == "user" else "Agent"
        content = str(item.get("content", "")).strip().replace("\n", " ")
        if content:
            history_lines.append(f"{role}: {content[:360]}")
    history_text = "\n".join(history_lines) if history_lines else "无"
    return f"""# 当前问题
问题分类：{category}
用户问题：{question}
检索改写问题：{rewritten_question or question}

# 对话上下文
{history_text}

# 资料区
{chr(10).join(source_blocks)}
"""


def call_minimax(prompt: str, system_prompt: str = AVATAR_SYSTEM_PROMPT) -> str:
    api_key = env_value("MINIMAX_API_KEY")
    if not api_key:
        return ""
    model = env_value("MINIMAX_MODEL", "MiniMax-M2.7")
    base_url = env_value("MINIMAX_BASE_URL", "https://api.minimax.io/v1").rstrip("/")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 1200,
        "stream": False,
    }
    data = post_json(
        f"{base_url}/chat/completions",
        payload,
        {"Authorization": f"Bearer {api_key}"},
    )
    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError(f"MiniMax returned no choices: {json.dumps(data, ensure_ascii=False)[:500]}")
    return str(choices[0].get("message", {}).get("content", "")).strip()


def stream_minimax(prompt: str, system_prompt: str = AVATAR_SYSTEM_PROMPT) -> tuple[str, Any]:
    api_key = env_value("MINIMAX_API_KEY")
    if not api_key:
        return "local_template", iter(())
    model = env_value("MINIMAX_MODEL", "MiniMax-M2.7")
    base_url = env_value("MINIMAX_BASE_URL", "https://api.minimax.io/v1").rstrip("/")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 1200,
        "stream": True,
    }
    return f"minimax:{model}", stream_chat_completions(
        f"{base_url}/chat/completions",
        payload,
        {"Authorization": f"Bearer {api_key}"},
    )


def stream_chat_completions(url: str, payload: dict[str, Any], headers: dict[str, str]) -> Any:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=LLM_TIMEOUT_SECONDS) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for choice in data.get("choices", []):
                    delta = choice.get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield str(content)
                    message = choice.get("message", {})
                    if message.get("content"):
                        yield str(message["content"])
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"LLM HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM network error: {exc.reason}") from exc


def post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: int = LLM_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"LLM HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM network error: {exc.reason}") from exc


def call_embedding_api(texts: list[str]) -> list[list[float]]:
    api_key = embedding_api_key()
    if not api_key:
        return []
    model = embedding_model()
    base_url = embedding_base_url()
    payload = {"model": model, "input": texts}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/embeddings",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=EMBEDDING_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Embedding HTTP {exc.code}: {sanitize_error(detail[:500])}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Embedding network error: {exc.reason}") from exc

    items = sorted(data.get("data", []), key=lambda item: item.get("index", 0))
    vectors = [item.get("embedding") for item in items]
    if len(vectors) != len(texts) or not all(is_vector(vector) for vector in vectors):
        raise RuntimeError(f"Embedding returned invalid response: {sanitize_error(json.dumps(data, ensure_ascii=False)[:500])}")
    return [[float(value) for value in vector] for vector in vectors]


def call_siliconflow_chat(
    prompt: str,
    model: str,
    max_tokens: int = 512,
    temperature: float = 0,
    timeout: int = LLM_TIMEOUT_SECONDS,
) -> str:
    api_key = embedding_api_key()
    if not api_key:
        return ""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "enable_thinking": False,
    }
    data = post_json(
        f"{siliconflow_base_url()}/chat/completions",
        payload,
        {"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )
    choices = data.get("choices", [])
    if not choices:
        raise RuntimeError(f"SiliconFlow returned no choices: {json.dumps(data, ensure_ascii=False)[:500]}")
    return str(choices[0].get("message", {}).get("content", "")).strip()


def parse_json_object(text: str) -> dict[str, Any]:
    compact = text.strip()
    if compact.startswith("```"):
        compact = re.sub(r"^```(?:json)?\s*", "", compact)
        compact = re.sub(r"\s*```$", "", compact)
    match = re.search(r"\{.*\}", compact, flags=re.DOTALL)
    if match:
        compact = match.group(0)
    data = json.loads(compact)
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    return data


def evaluate_quality(answer: str, sources: list[dict[str, Any]], was_answered: int, missing_info: str) -> float:
    score = 0.0
    if was_answered:
        score += 45
    if sources:
        score += min(25, len(sources) * 6)
    if re.search(r"\bS\d+\b", answer):
        score += 15
    if missing_info:
        score -= 10
    return max(0.0, min(100.0, score))


def make_user_id(role: str) -> str:
    return f"{role}_{uuid.uuid4().hex[:12]}"


def create_or_update_admin_user() -> dict[str, Any]:
    email = admin_email()
    now = now_iso()
    user_id = f"admin_{hashlib.sha256(email.encode('utf-8')).hexdigest()[:12]}"
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO users (id, email, display_name, role, created_at, last_seen_at)
            VALUES (?, ?, ?, 'admin', ?, ?)
            ON CONFLICT(id) DO UPDATE SET email = excluded.email, last_seen_at = excluded.last_seen_at
            """,
            (user_id, email, "管理员", now, now),
        )
        conn.commit()
    return {"id": user_id, "email": email, "display_name": "管理员", "role": "admin"}


def create_visitor_user() -> dict[str, Any]:
    now = now_iso()
    suffix = random.randint(1000, 9999)
    user_id = make_user_id("visitor")
    display_name = f"访客 {suffix}"
    email = f"visitor_{suffix}_{user_id[-4:]}@guest.local"
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO users (id, email, display_name, role, created_at, last_seen_at) VALUES (?, ?, ?, 'visitor', ?, ?)",
            (user_id, email, display_name, now, now),
        )
        conn.commit()
    return {"id": user_id, "email": email, "display_name": display_name, "role": "visitor"}


def create_session(user: dict[str, Any]) -> str:
    session_id = uuid.uuid4().hex + uuid.uuid4().hex
    now = now_iso()
    expires_at = datetime.fromtimestamp(time.time() + SESSION_MAX_AGE_SECONDS).isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO sessions (id, user_id, created_at, last_seen_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, user["id"], now, now, expires_at),
        )
        conn.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (now, user["id"]))
        conn.commit()
    return session_id


def get_session_user(session_id: str) -> dict[str, Any] | None:
    if not session_id:
        return None
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT u.id, u.email, u.display_name, u.role, s.expires_at
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < now_iso():
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
            return None
        now = now_iso()
        conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (now, session_id))
        conn.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (now, row["id"]))
        conn.commit()
    return {"id": row["id"], "email": row["email"], "display_name": row["display_name"], "role": row["role"]}


def delete_session(session_id: str) -> None:
    if not session_id:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()


def public_user(user: dict[str, Any] | None) -> dict[str, Any] | None:
    if not user:
        return None
    return {
        "id": user["id"],
        "email": user.get("email", ""),
        "display_name": user.get("display_name", ""),
        "role": user.get("role", ""),
    }


def get_or_create_conversation(user: dict[str, Any] | None, conversation_id: str, first_question: str = "") -> str:
    now = now_iso()
    candidate = conversation_id.strip() if conversation_id else ""
    user_id = user.get("id", "") if user else ""
    with sqlite3.connect(DB_PATH) as conn:
        if candidate:
            if user and user.get("role") == "admin":
                row = conn.execute("SELECT id FROM conversations WHERE id = ?", (candidate,)).fetchone()
            else:
                row = conn.execute("SELECT id FROM conversations WHERE id = ? AND user_id = ?", (candidate, user_id)).fetchone()
            if row:
                conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, candidate))
                conn.commit()
                return candidate
        new_id = uuid.uuid4().hex
        title = first_question.strip()[:60] or "新的对话"
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, updated_at, user_id) VALUES (?, ?, ?, ?, ?)",
            (new_id, title, now, now, user_id),
        )
        conn.commit()
        return new_id


def fetch_conversation_history(conversation_id: str, limit: int = 12, user: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if not conversation_id:
        return []
    user_clause = ""
    params: list[Any] = [conversation_id]
    if user and user.get("role") != "admin":
        user_clause = " AND user_id = ?"
        params.append(user["id"])
    params.append(limit)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT role, content, category, sources_json, metadata_json, created_at
            FROM messages
            WHERE conversation_id = ? """ + user_clause + """
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def log_message(
    user: dict[str, Any] | None,
    conversation_id: str,
    role: str,
    content: str,
    category: str = "",
    sources: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages (conversation_id, user_id, role, content, category, sources_json, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                user.get("id", "") if user else "",
                role,
                content,
                category,
                json.dumps(sources or [], ensure_ascii=False),
                json.dumps(metadata or {}, ensure_ascii=False),
                now_iso(),
            ),
        )
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now_iso(), conversation_id))
        conn.commit()
        return int(cursor.lastrowid)


def log_question(
    user: dict[str, Any] | None,
    question: str,
    user_type: str,
    category: str,
    answer: str,
    sources: list[dict[str, Any]],
    quality_score: float,
    was_answered: int,
    missing_info: str,
    answer_provider: str,
    conversation_id: str = "",
    message_id: int = 0,
    classification_provider: str = "",
    classification_reason: str = "",
    rewritten_question: str = "",
    retrieval_method: str = "",
    retrieval_stats: dict[str, Any] | None = None,
    retrieved_chunks: list[dict[str, Any]] | None = None,
    conversation_turn_count: int = 0,
    latency_ms: int = 0,
) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO questions (
                user_id, user_display_name, user_role, question, user_type, category, answer, sources_json, quality_score,
                was_answered, missing_info, answer_provider, conversation_id, message_id,
                classification_provider, classification_reason, rewritten_question,
                retrieval_method, retrieval_stats_json, retrieved_chunks_json,
                conversation_turn_count, latency_ms, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user.get("id", "") if user else "",
                user.get("display_name", "") if user else "",
                user.get("role", "") if user else "",
                question,
                user_type,
                category,
                answer,
                json.dumps(sources, ensure_ascii=False),
                quality_score,
                was_answered,
                missing_info,
                answer_provider,
                conversation_id,
                message_id,
                classification_provider,
                classification_reason,
                rewritten_question,
                retrieval_method,
                json.dumps(retrieval_stats or {}, ensure_ascii=False),
                json.dumps(retrieved_chunks or [], ensure_ascii=False),
                conversation_turn_count,
                latency_ms,
                now_iso(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def row_to_question(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "message_id": row["message_id"],
        "user_id": row["user_id"],
        "user_display_name": row["user_display_name"],
        "user_role": row["user_role"],
        "question": row["question"],
        "user_type": row["user_type"],
        "category": row["category"],
        "answer": row["answer"],
        "sources": json.loads(row["sources_json"]),
        "quality_score": row["quality_score"],
        "was_answered": bool(row["was_answered"]),
        "missing_info": row["missing_info"],
        "answer_provider": row["answer_provider"],
        "classification_provider": row["classification_provider"],
        "classification_reason": row["classification_reason"],
        "rewritten_question": row["rewritten_question"],
        "retrieval_method": row["retrieval_method"],
        "retrieval_stats": json.loads(row["retrieval_stats_json"] or "{}"),
        "retrieved_chunks": json.loads(row["retrieved_chunks_json"] or "[]"),
        "conversation_turn_count": row["conversation_turn_count"],
        "latency_ms": row["latency_ms"],
        "feedback": row["feedback"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


def suggested_questions() -> dict[str, list[str]]:
    return {"questions": random.sample(INITIAL_SUGGESTIONS, 3)}


def suggest_followups(category: str, answer: str, sources: list[dict[str, Any]]) -> list[str]:
    pool = FOLLOWUP_SUGGESTIONS.get(category) or FOLLOWUP_SUGGESTIONS["general"]
    suggestions = list(pool)
    compact = answer.lower()
    if "ai" in compact and "真实" in answer and category != "ai_pm_fit":
        suggestions.append("他的真实 AI 产品经验有哪些？")
    if "工单" in answer:
        suggestions.append("工单 AI 分析项目具体解决了什么问题？")
    if "智能门锁" in answer or "iot" in compact:
        suggestions.append("智能门锁和 IoT 经验怎么证明他的产品能力？")
    if sources:
        suggestions.append("这些结论分别来自职业档案的哪些部分？")
    seen: list[str] = []
    for item in suggestions:
        if item not in seen:
            seen.append(item)
    return seen[:3]


def chunk_text(text: str, size: int = 80) -> list[str]:
    chunks: list[str] = []
    buffer = ""
    for char in text:
        buffer += char
        if len(buffer) >= size or char in "\n。！？":
            chunks.append(buffer)
            buffer = ""
    if buffer:
        chunks.append(buffer)
    return chunks


def generate_answer_for_stream(question: str, category: str, chunks: list[dict[str, Any]]) -> tuple[str, str, int, str]:
    if category == "risk_boundary" or not chunks or chunks[0]["score"] < MIN_SCORE:
        return generate_answer(question, category, chunks)

    provider = selected_llm_provider()
    if provider != "minimax":
        return generate_answer(question, category, chunks)

    prompt = build_llm_user_prompt(question, category, chunks)
    provider_label = f"minimax:{env_value('MINIMAX_MODEL', 'MiniMax-M2.7')}"
    try:
        collected = []
        for delta in stream_minimax(prompt)[1]:
            collected.append(delta)
        answer = strip_thinking("".join(collected))
        if answer:
            return answer, "", 1, provider_label
    except Exception as exc:
        fallback_answer = generate_template_answer(question, category, chunks)
        safe_error = sanitize_error(str(exc))
        return (
            fallback_answer
            + f"\n\n系统备注：LLM 调用失败，已回退到本地资料摘要。错误：{safe_error}",
            f"LLM 调用失败：{safe_error}",
            1,
            "local_template_fallback",
        )

    return generate_template_answer(question, category, chunks), "", 1, "local_template"


def chat(payload: dict[str, Any], user: dict[str, Any] | None = None) -> dict[str, Any]:
    started = time.time()
    question, user_type, payload_conversation_id = validate_chat_payload(payload)
    if user:
        user_type = user.get("role", user_type)
    conversation_id = get_or_create_conversation(user, payload_conversation_id, question)
    history = fetch_conversation_history(conversation_id, user=user)
    classification = classify_question_with_llm(question, history)
    category = classification["category"]
    rewritten_question = classification["rewritten_question"]
    user_message_id = log_message(
        user,
        conversation_id,
        "user",
        question,
        category,
        metadata={"rewritten_question": rewritten_question, "classification": classification},
    )
    if category == "unrelated":
        chunks: list[dict[str, Any]] = []
        retrieval_stats = {"index_count": len(INDEX), "candidate_count": 0, "returned_count": 0, "method": "none"}
    else:
        chunks, retrieval_stats = retrieve_with_stats(rewritten_question, category)
    answer, missing_info, was_answered, answer_provider = generate_answer(question, category, chunks)
    sources = build_sources(chunks, rewritten_question)
    quality_score = evaluate_quality(answer, sources, was_answered, missing_info)
    assistant_message_id = log_message(
        user,
        conversation_id,
        "assistant",
        answer,
        category,
        sources,
        {
            "answer_provider": answer_provider,
            "quality_score": quality_score,
            "missing_info": missing_info,
            "retrieval_stats": retrieval_stats,
        },
    )
    question_id = log_question(
        user,
        question,
        user_type,
        category,
        answer,
        sources,
        quality_score,
        was_answered,
        missing_info,
        answer_provider,
        conversation_id,
        assistant_message_id,
        classification["provider"],
        classification["reason"],
        rewritten_question,
        retrieval_stats.get("method", ""),
        retrieval_stats,
        summarize_retrieved_chunks(chunks),
        len(history) // 2 + 1,
        int((time.time() - started) * 1000),
    )
    return {
        "id": question_id,
        "conversation_id": conversation_id,
        "user_message_id": user_message_id,
        "message_id": assistant_message_id,
        "question": question,
        "category": category,
        "classification_provider": classification["provider"],
        "classification_reason": classification["reason"],
        "rewritten_question": rewritten_question,
        "answer": answer,
        "sources": sources,
        "quality_score": quality_score,
        "was_answered": bool(was_answered),
        "missing_info": missing_info,
        "answer_provider": answer_provider,
        "retrieval_stats": retrieval_stats,
        "retrieved_chunks": summarize_retrieved_chunks(chunks),
    }


def validate_chat_payload(payload: dict[str, Any]) -> tuple[str, str, str]:
    question = str(payload.get("question", "")).strip()
    user_type = str(payload.get("user_type", "public"))[:40]
    conversation_id = str(payload.get("conversation_id", "")).strip()[:80]
    if len(question) < 2 or len(question) > 800:
        raise ValueError("Question length must be between 2 and 800 characters.")
    return question, user_type, conversation_id


def admin_summary() -> dict[str, Any]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM questions WHERE user_type != 'eval' ORDER BY created_at DESC"
        ).fetchall()
        eval_counts = conn.execute("SELECT kind, COUNT(*) AS count FROM eval_cases GROUP BY kind").fetchall()
    total = len(rows)
    answered = sum(1 for row in rows if row["was_answered"])
    avg_quality = round(sum(row["quality_score"] for row in rows) / total, 1) if total else 0
    categories = Counter(row["category"] for row in rows)
    questions = Counter(row["question"] for row in rows)
    retrieval_methods = Counter(row["retrieval_method"] or "unknown" for row in rows)
    classification_providers = Counter(row["classification_provider"] or "unknown" for row in rows)
    avg_latency = round(sum(row["latency_ms"] for row in rows) / total) if total else 0
    missing = [row for row in rows if row["missing_info"] or not row["was_answered"]]
    return {
        "total_questions": total,
        "answered_rate": round(answered / total * 100, 1) if total else 0,
        "avg_quality": avg_quality,
        "missing_count": len(missing),
        "avg_latency_ms": avg_latency,
        "category_counts": categories.most_common(),
        "retrieval_method_counts": retrieval_methods.most_common(),
        "classification_provider_counts": classification_providers.most_common(),
        "eval_case_counts": [(row["kind"], row["count"]) for row in eval_counts],
        "top_questions": questions.most_common(10),
        "index": {"sources": len(load_registry()), "chunks": len(INDEX), "embeddings": indexed_embedding_count()},
        "llm_provider": selected_llm_provider(),
    }


def admin_questions() -> dict[str, Any]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM questions WHERE user_type != 'eval' ORDER BY id DESC LIMIT 200"
        ).fetchall()
    return {"questions": [row_to_question(row) for row in rows]}


def admin_gaps() -> dict[str, Any]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT question, category, missing_info, COUNT(*) AS count, MAX(created_at) AS last_seen
            FROM questions
            WHERE user_type != 'eval' AND (was_answered = 0 OR missing_info != '')
            GROUP BY question, category, missing_info
            ORDER BY count DESC, last_seen DESC
            LIMIT 50
            """
        ).fetchall()
    return {"gaps": [dict(row) for row in rows]}


def admin_evals() -> dict[str, Any]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM eval_cases
            WHERE kind LIKE 'golden_%'
            ORDER BY id ASC
            """
        ).fetchall()
    cases = [eval_case_to_dict(row) for row in rows]
    total = len(cases)
    ran = [case for case in cases if case["last_run_at"]]
    passed = [case for case in ran if case["status"] == "passed"]
    avg_score = round(sum(case["score"] for case in ran) / len(ran), 2) if ran else 0
    return {
        "total": total,
        "ran": len(ran),
        "passed": len(passed),
        "pass_rate": round(len(passed) / len(ran) * 100, 1) if ran else 0,
        "avg_score": avg_score,
        "cases": cases,
    }


def eval_case_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    details = json.loads(row["details_json"] or "{}")
    expected = json.loads(row["expected_answer"] or "{}") if row["expected_answer"] else {}
    return {
        "id": row["id"],
        "kind": row["kind"],
        "case_id": row["notes"],
        "question": row["question"],
        "category": row["category"],
        "status": row["status"],
        "score": row["score"],
        "last_run_at": row["last_run_at"],
        "latency_ms": row["latency_ms"],
        "expected": expected,
        "details": details,
    }


EVAL_ANALYST_SYSTEM_PROMPT = """你是 RAG 问答系统的评测分析员。你的任务是根据一条 golden case 的评测结果，分析失败原因和改进方案。

要求：
- 只能基于用户提供的评测数据分析，不要补充外部事实。
- 不要输出模型隐藏思考链，只输出可执行的诊断结论。
- 不要输出 <think>、思考过程、内心推理或草稿。
- 要区分分类问题、检索问题、生成覆盖问题、引用问题、边界问题、golden set 设计问题。
- 改进方案要具体到：改 query rewrite / chunk / rerank / prompt / golden case / 资料补充。
- 用简洁中文 Markdown 输出。

输出结构：
## 结论
## 主要原因
## 改进方案
## 下一步验证
"""


def eval_ai_analysis_prompt(case: dict[str, Any]) -> str:
    details = case.get("details", {})
    expected = case.get("expected", {})
    payload = {
        "case_id": case.get("case_id"),
        "question": case.get("question"),
        "status": case.get("status"),
        "score": case.get("score"),
        "expected_policy": expected.get("answer_policy"),
        "expected_category": expected.get("expected_category"),
        "must_include": expected.get("must_include", []),
        "must_not_include": expected.get("must_not_include", []),
        "primary_failure": details.get("primary_failure"),
        "diagnosis": details.get("diagnosis"),
        "failures": details.get("failures", []),
        "dimensions": details.get("dimensions", {}),
        "retrieval_stats": details.get("retrieval_stats", {}),
        "actual_answer_excerpt": details.get("actual_answer_excerpt", ""),
        "actual_sources": details.get("actual_sources", []),
        "retrieved_chunks": details.get("retrieved_chunks", []),
    }
    return (
        "请分析下面这条 golden case 的评测结果，判断问题主要出在哪里，并给出改进方案。\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def fallback_eval_ai_analysis(case: dict[str, Any]) -> str:
    details = case.get("details", {})
    dimensions = details.get("dimensions", {})
    coverage = dimensions.get("answer_coverage", {})
    retrieval = dimensions.get("retrieval_relevance", {})
    primary = details.get("primary_failure", "unknown")
    lines = [
        "## 结论",
        details.get("diagnosis") or f"主失败类型是 {primary}。",
        "",
        "## 主要原因",
        f"- 分类分：{dimensions.get('intent_classification', {}).get('score', 0)}",
        f"- 检索分：{retrieval.get('score', 0)}",
        f"- 覆盖分：{coverage.get('score', 0)}",
    ]
    not_retrieved = coverage.get("not_retrieved") or []
    retrieved_not_used = coverage.get("retrieved_not_used") or []
    if not_retrieved:
        lines.append("- 未召回事实：" + "、".join(not_retrieved[:8]))
    if retrieved_not_used:
        lines.append("- 已召回但未写入答案：" + "、".join(retrieved_not_used[:8]))
    lines.extend(
        [
            "",
            "## 改进方案",
            "- 如果缺失事实未召回，先调整 chunk、检索改写和 rerank 权重。",
            "- 如果事实已召回但答案没写，优先加强回答 prompt 的覆盖要求。",
            "- 如果 golden 期望词与资料表达不一致，应该把断言改成可匹配的同义事实组。",
            "",
            "## 下一步验证",
            "- 重跑当前 case，观察检索分和覆盖分是否同时提升。",
        ]
    )
    return "\n".join(lines)


def admin_eval_analysis(eval_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    force = bool(payload.get("force"))
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM eval_cases WHERE id = ?", (eval_id,)).fetchone()
        if not row:
            raise LookupError("Eval case not found")
    case = eval_case_to_dict(row)
    details = case.get("details", {})
    if details.get("ai_analysis") and not force:
        return {"id": eval_id, "ai_analysis": details["ai_analysis"], "cached": True}

    try:
        if selected_llm_provider() == "minimax":
            analysis = strip_thinking(call_minimax(eval_ai_analysis_prompt(case), EVAL_ANALYST_SYSTEM_PROMPT))
            if not analysis:
                analysis = fallback_eval_ai_analysis(case)
        else:
            analysis = fallback_eval_ai_analysis(case)
    except Exception as exc:
        analysis = fallback_eval_ai_analysis(case) + f"\n\n系统备注：AI 分析生成失败，已使用本地规则分析。错误：{sanitize_error(str(exc))}"

    details["ai_analysis"] = analysis
    details["ai_analysis_provider"] = selected_llm_provider()
    details["ai_analysis_updated_at"] = now_iso()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE eval_cases SET details_json = ? WHERE id = ?",
            (json.dumps(details, ensure_ascii=False), eval_id),
        )
        conn.commit()
    return {"id": eval_id, "ai_analysis": analysis, "cached": False}


def run_admin_evals(payload: dict[str, Any]) -> dict[str, Any]:
    limit = int(payload.get("limit", 5))
    if limit <= 0:
        limit = 999
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM eval_cases
            WHERE kind LIKE 'golden_%'
            ORDER BY id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    results = []
    for row in rows:
        results.append(run_single_eval_case(dict(row)))
    passed = sum(1 for item in results if item["status"] == "passed")
    return {
        "ran": len(results),
        "passed": passed,
        "pass_rate": round(passed / len(results) * 100, 1) if results else 0,
        "results": results,
    }


def run_single_eval_case(row: dict[str, Any]) -> dict[str, Any]:
    started = time.time()
    expected = json.loads(row.get("expected_answer") or "{}")
    kind = row.get("kind", "")
    if kind == "golden_multi":
        turns = expected.get("turns") or [part.strip() for part in row["question"].split("/") if part.strip()]
        conversation_id = ""
        final_result: dict[str, Any] = {}
        for turn in turns:
            payload = {"question": turn, "user_type": "eval", "conversation_id": conversation_id}
            final_result = chat(payload)
            conversation_id = final_result.get("conversation_id", conversation_id)
        result = final_result
    else:
        result = chat({"question": row["question"], "user_type": "eval"})
    details = evaluate_answer_against_case(result, expected, json.loads(row.get("expected_sources_json") or "[]"))
    latency_ms = int((time.time() - started) * 1000)
    status = "passed" if details["passed"] else "failed"
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE eval_cases
            SET actual_answer = ?, actual_sources_json = ?, status = ?, score = ?,
                details_json = ?, source_question_id = ?, last_run_at = ?, latency_ms = ?
            WHERE id = ?
            """,
            (
                result.get("answer", ""),
                json.dumps(result.get("sources", []), ensure_ascii=False),
                status,
                details["score"],
                json.dumps(details, ensure_ascii=False),
                result.get("id", 0),
                now_iso(),
                latency_ms,
                row["id"],
            ),
        )
        conn.commit()
    return {
        "id": row["id"],
        "case_id": row.get("notes", ""),
        "question": row["question"],
        "status": status,
        "score": details["score"],
        "failures": details["failures"],
        "diagnosis": details.get("diagnosis", ""),
        "dimensions": details.get("dimensions", {}),
        "latency_ms": latency_ms,
    }


def normalize_for_eval(text: str) -> str:
    return re.sub(r"\s+", "", str(text).lower())


def assertion_present(needle: str, haystack: str) -> bool:
    normalized_needle = normalize_for_eval(needle)
    normalized_haystack = normalize_for_eval(haystack)
    if "/" in needle:
        parts = [part.strip() for part in re.split(r"[/／]", needle) if part.strip()]
        if parts and all(normalize_for_eval(part) in normalized_haystack for part in parts):
            return True
    return normalized_needle in normalized_haystack


def eval_ratio_score(passed: int, total: int) -> float:
    if total <= 0:
        return 100.0
    return round(passed / total * 100, 1)


def eval_term_statuses(terms: list[str], *haystacks: str) -> list[dict[str, Any]]:
    statuses = []
    answer = haystacks[0] if haystacks else ""
    supporting_text = " ".join(haystacks[1:])
    for term in terms:
        in_answer = assertion_present(term, answer)
        in_supporting_text = assertion_present(term, supporting_text)
        statuses.append(
            {
                "term": term,
                "in_answer": in_answer,
                "in_retrieval": in_supporting_text,
                "status": "covered" if in_answer else ("retrieved_not_used" if in_supporting_text else "not_retrieved"),
            }
        )
    return statuses


def eval_source_statuses(expected_sources: list[str], source_text: str) -> list[dict[str, Any]]:
    return [
        {
            "term": term,
            "matched": assertion_present(term, source_text),
        }
        for term in expected_sources
    ]


def diagnose_eval_failure(
    category_ok: bool,
    source_score: float,
    missing_term_statuses: list[dict[str, Any]],
    forbidden_terms: list[str],
    citation_ok: bool,
) -> tuple[str, str]:
    if not category_ok:
        return "intent_classification", "分类错误：先修分类 prompt 或分类兜底规则。"
    if forbidden_terms:
        return "boundary_control", "边界控制错误：回答出现了 golden set 明确禁止的说法。"
    if source_score < 60:
        return "retrieval_relevance", "检索召回不足：预期来源没有进入回答上下文，优先调整 chunk、query rewrite 或召回策略。"
    if missing_term_statuses:
        retrieved_not_used = [item for item in missing_term_statuses if item["status"] == "retrieved_not_used"]
        not_retrieved = [item for item in missing_term_statuses if item["status"] == "not_retrieved"]
        if retrieved_not_used and not not_retrieved:
            return "answer_coverage", "生成覆盖不足：相关依据已召回，但模型没有写进答案，优先调整 system prompt 或答案结构要求。"
        if not_retrieved and not retrieved_not_used:
            return "retrieval_or_golden", "缺失事实未被召回：可能需要优化检索，也可能是 golden 期望词过细或资料表达不一致。"
        return "retrieval_then_generation", "同时存在召回缺口和生成覆盖缺口：先看未召回事实，再看已召回未使用事实。"
    if not citation_ok:
        return "citation_quality", "引用质量不足：答案没有按要求标注 S1/S2 来源。"
    return "passed", "核心评测项通过。"


def evaluate_answer_against_case(
    result: dict[str, Any],
    expected: dict[str, Any],
    expected_sources: list[str],
) -> dict[str, Any]:
    answer = result.get("answer", "")
    sources = result.get("sources", [])
    retrieved_chunks = result.get("retrieved_chunks", [])
    source_text = " ".join(
        f"{source.get('heading', '')} {source.get('excerpt', '')} {source.get('quote', '')}" for source in sources
    )
    retrieved_text = " ".join(
        f"{chunk.get('heading', '')} {chunk.get('excerpt', '')}" for chunk in retrieved_chunks
    )
    supporting_text = f"{source_text} {retrieved_text}"
    failures: list[str] = []
    checks: list[dict[str, Any]] = []

    expected_category = expected.get("expected_category", "")
    category_ok = not expected_category or result.get("category") == expected_category
    checks.append({"name": "category", "passed": category_ok, "expected": expected_category, "actual": result.get("category")})
    if not category_ok:
        failures.append(f"分类应为 {expected_category}，实际为 {result.get('category')}")

    include_statuses = eval_term_statuses(expected.get("must_include", []), answer, supporting_text)
    missing_statuses = [item for item in include_statuses if not item["in_answer"]]
    missing_terms = [item["term"] for item in missing_statuses]
    include_ok = not missing_terms
    checks.append({"name": "must_include", "passed": include_ok, "items": include_statuses, "missing": missing_terms})
    if missing_terms:
        failures.append("缺少关键事实：" + "、".join(missing_terms[:5]))

    forbidden_terms = [term for term in expected.get("must_not_include", []) if assertion_present(term, answer)]
    exclude_ok = not forbidden_terms
    checks.append({"name": "must_not_include", "passed": exclude_ok, "forbidden": forbidden_terms})
    if forbidden_terms:
        failures.append("出现禁止说法：" + "、".join(forbidden_terms[:5]))

    source_statuses = eval_source_statuses(expected_sources, source_text)
    source_hits = [item for item in source_statuses if item["matched"]]
    source_misses = [item["term"] for item in source_statuses if not item["matched"]]
    source_score = eval_ratio_score(len(source_hits), len(source_statuses))
    source_ok = not expected_sources or bool(source_hits)
    checks.append({"name": "expected_sources", "passed": source_ok, "score": source_score, "items": source_statuses, "missing": source_misses})
    if not source_ok:
        failures.append("来源未命中预期小节：" + "、".join(source_misses[:4]))

    citation_ok = bool(re.search(r"\bS\d+\b|\[S\d+\]", answer)) or result.get("category") in {"risk_boundary", "unrelated"}
    checks.append({"name": "citation", "passed": citation_ok})
    if not citation_ok:
        failures.append("回答缺少来源引用")

    coverage_score = eval_ratio_score(
        len([item for item in include_statuses if item["in_answer"]]),
        len(include_statuses),
    )
    boundary_score = 100.0 if exclude_ok else 0.0
    citation_score = 100.0 if citation_ok else 0.0
    category_score = 100.0 if category_ok else 0.0
    dimensions = {
        "intent_classification": {
            "score": category_score,
            "passed": category_ok,
            "expected": expected_category,
            "actual": result.get("category"),
        },
        "retrieval_relevance": {
            "score": source_score,
            "passed": source_ok,
            "expected_sources": expected_sources,
            "matched_sources": [item["term"] for item in source_hits],
            "missing_sources": source_misses,
        },
        "answer_coverage": {
            "score": coverage_score,
            "passed": include_ok,
            "covered_terms": [item["term"] for item in include_statuses if item["in_answer"]],
            "retrieved_not_used": [item["term"] for item in include_statuses if item["status"] == "retrieved_not_used"],
            "not_retrieved": [item["term"] for item in include_statuses if item["status"] == "not_retrieved"],
        },
        "boundary_control": {
            "score": boundary_score,
            "passed": exclude_ok,
            "forbidden_terms": forbidden_terms,
        },
        "citation_quality": {
            "score": citation_score,
            "passed": citation_ok,
            "source_count": len(sources),
        },
    }
    score = round(
        category_score * 0.18
        + source_score * 0.24
        + coverage_score * 0.36
        + boundary_score * 0.12
        + citation_score * 0.10,
        1,
    )
    primary_failure, recommendation = diagnose_eval_failure(
        category_ok,
        source_score,
        missing_statuses,
        forbidden_terms,
        citation_ok,
    )
    return {
        "passed": not failures,
        "score": score,
        "failures": failures,
        "checks": checks,
        "dimensions": dimensions,
        "diagnosis": recommendation,
        "primary_failure": primary_failure,
        "expected_policy": expected.get("answer_policy", ""),
        "actual_category": result.get("category"),
        "answer_provider": result.get("answer_provider"),
        "retrieval_stats": result.get("retrieval_stats", {}),
        "actual_answer_excerpt": excerpt(answer, 520),
        "actual_sources": [
            {
                "heading": source.get("heading", ""),
                "quote": source.get("quote", ""),
                "score": source.get("score", 0),
                "retrieval_method": source.get("retrieval_method", ""),
            }
            for source in sources[:6]
        ],
        "retrieved_chunks": retrieved_chunks[:6],
    }


def conversation_detail(conversation_id: str, user: dict[str, Any]) -> dict[str, Any]:
    conversation_id = conversation_id.strip()[:80]
    if not conversation_id:
        raise LookupError("Conversation not found")
    user_clause = ""
    params: list[Any] = [conversation_id]
    if user.get("role") != "admin":
        user_clause = " AND user_id = ?"
        params.append(user["id"])
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        conversation = conn.execute("SELECT * FROM conversations WHERE id = ?" + user_clause, params).fetchone()
        if not conversation:
            raise LookupError("Conversation not found")
        rows = conn.execute(
            """
            SELECT id, role, content, category, sources_json, metadata_json, created_at
            FROM messages
            WHERE conversation_id = ?""" + user_clause + """
            ORDER BY id ASC
            """,
            params,
        ).fetchall()
    messages = []
    for row in rows:
        messages.append(
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "category": row["category"],
                "sources": json.loads(row["sources_json"] or "[]"),
                "metadata": json.loads(row["metadata_json"] or "{}"),
                "created_at": row["created_at"],
            }
        )
    return {"conversation": dict(conversation), "messages": messages}


def conversations_list(user: dict[str, Any]) -> dict[str, Any]:
    user_clause = ""
    params: list[Any] = []
    if user.get("role") != "admin":
        user_clause = " AND c.user_id = ?"
        params.append(user["id"])
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                c.id,
                c.title,
                c.created_at,
                c.updated_at,
                (
                    SELECT content
                    FROM messages
                    WHERE conversation_id = c.id AND role = 'user'
                    ORDER BY id DESC
                    LIMIT 1
                ) AS last_question,
                (
                    SELECT COUNT(*)
                    FROM messages
                    WHERE conversation_id = c.id
                ) AS message_count
            FROM conversations c
            WHERE EXISTS (
                SELECT 1
                FROM questions q
                WHERE q.conversation_id = c.id AND q.user_type != 'eval'
            )""" + user_clause + """
            ORDER BY c.updated_at DESC
            LIMIT 80
            """,
            params,
        ).fetchall()
    conversations = []
    for row in rows:
        item = dict(row)
        item["title"] = item.get("last_question") or item.get("title") or "新的对话"
        conversations.append(item)
    return {"conversations": conversations}


def update_feedback(question_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    feedback = str(payload.get("feedback", ""))[:40]
    status = str(payload.get("status", "reviewed"))[:40]
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "UPDATE questions SET feedback = ?, status = ? WHERE id = ?",
            (feedback, status, question_id),
        )
        if status in {"bad_case", "needs_fix"} or feedback in {"bad", "incorrect", "low_quality"}:
            row = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
            if row:
                conn.execute(
                    """
                    INSERT INTO eval_cases (
                        kind, question, actual_answer, actual_sources_json, category,
                        status, severity, notes, source_question_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "bad_case",
                        row["question"],
                        row["answer"],
                        row["sources_json"],
                        row["category"],
                        "new",
                        "medium",
                        f"feedback={feedback}; status={status}",
                        question_id,
                        now_iso(),
                    ),
                )
        conn.commit()
        if cursor.rowcount == 0:
            raise LookupError("Question not found")
    return {"ok": True}


def login_admin(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    configured_password = admin_password()
    if not configured_password:
        raise ValueError("管理员密码未配置。")
    if email != admin_email() or password != configured_password:
        raise PermissionError("账号或密码不正确。")
    user = create_or_update_admin_user()
    session_id = create_session(user)
    return {"ok": True, "user": public_user(user)}, session_id


def login_visitor() -> tuple[dict[str, Any], str]:
    user = create_visitor_user()
    session_id = create_session(user)
    return {"ok": True, "user": public_user(user)}, session_id


def admin_users() -> dict[str, Any]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                u.id,
                u.email,
                u.display_name,
                u.role,
                u.created_at,
                u.last_seen_at,
                COUNT(DISTINCT q.id) AS question_count,
                COUNT(DISTINCT c.id) AS conversation_count,
                ROUND(AVG(q.quality_score), 1) AS avg_quality
            FROM users u
            LEFT JOIN questions q ON q.user_id = u.id AND q.user_type != 'eval'
            LEFT JOIN conversations c ON c.user_id = u.id
            GROUP BY u.id
            ORDER BY u.last_seen_at DESC
            LIMIT 200
            """
        ).fetchall()
    return {"users": [dict(row) for row in rows]}


def sources() -> dict[str, Any]:
    return {
        "sources": load_registry(),
        "index": {
            "chunks": len(INDEX),
            "embeddings": indexed_embedding_count(),
            "embedding_ready": EMBEDDING_READY,
            "embedding_model": embedding_model(),
            "embedding_error": EMBEDDING_LAST_ERROR,
        },
    }


def llm_status() -> dict[str, Any]:
    return {
        "provider": selected_llm_provider(),
        "minimax_configured": bool(env_value("MINIMAX_API_KEY")),
        "minimax_model": env_value("MINIMAX_MODEL", "MiniMax-M2.7"),
        "embedding_provider": "siliconflow" if embedding_configured() else "none",
        "embedding_configured": embedding_configured(),
        "embedding_ready": EMBEDDING_READY,
        "embedding_model": embedding_model(),
        "embedding_base_url": embedding_base_url(),
        "embedding_error": EMBEDDING_LAST_ERROR,
        "classification_model": classification_model(),
        "classification_fallback_models": classification_fallback_models(),
    }


def reindex() -> dict[str, int]:
    return refresh_index()


class AvatarRequestHandler(BaseHTTPRequestHandler):
    server_version = "PersonalAvatarAgent/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def route_path(self) -> str:
        return urllib.parse.urlparse(self.path).path

    def cookie_session_id(self) -> str:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel else ""

    def current_user(self) -> dict[str, Any] | None:
        return get_session_user(self.cookie_session_id())

    def require_user(self) -> dict[str, Any]:
        user = self.current_user()
        if not user:
            raise PermissionError("Authentication required")
        return user

    def require_admin(self) -> dict[str, Any]:
        user = self.require_user()
        if user.get("role") != "admin":
            raise PermissionError("Admin access required")
        return user

    def session_cookie_header(self, session_id: str) -> str:
        return (
            f"{SESSION_COOKIE_NAME}={session_id}; Path=/; Max-Age={SESSION_MAX_AGE_SECONDS}; "
            "HttpOnly; SameSite=Lax"
        )

    def clear_session_cookie(self) -> None:
        self.send_header("Set-Cookie", f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self) -> None:
        try:
            path = self.route_path()
            if path == "/":
                if not self.current_user():
                    self.redirect("/login")
                    return
                self.send_file(STATIC_DIR / "index.html")
            elif path == "/login":
                self.send_file(STATIC_DIR / "login.html")
            elif path == "/admin":
                self.require_admin()
                self.send_file(STATIC_DIR / "admin.html")
            elif path.startswith("/static/"):
                requested = path.removeprefix("/static/")
                if requested == "admin.html":
                    self.require_admin()
                self.send_file((STATIC_DIR / requested).resolve(), root=STATIC_DIR)
            elif path == "/api/me":
                self.send_json({"user": public_user(self.current_user())})
            elif path == "/api/suggested-questions":
                self.send_json(suggested_questions())
            elif path == "/api/admin/summary":
                self.require_admin()
                self.send_json(admin_summary())
            elif path == "/api/admin/questions":
                self.require_admin()
                self.send_json(admin_questions())
            elif path == "/api/admin/gaps":
                self.require_admin()
                self.send_json(admin_gaps())
            elif path == "/api/admin/evals":
                self.require_admin()
                self.send_json(admin_evals())
            elif path == "/api/admin/users":
                self.require_admin()
                self.send_json(admin_users())
            elif path == "/api/sources":
                self.send_json(sources())
            elif path == "/api/llm/status":
                self.send_json(llm_status())
            elif path == "/api/conversations":
                self.send_json(conversations_list(self.require_user()))
            elif match := re.match(r"^/api/conversations/([A-Za-z0-9_-]+)$", path):
                self.send_json(conversation_detail(match.group(1), self.require_user()))
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
        except PermissionError as exc:
            if self.route_path() == "/admin":
                self.redirect("/login?next=/admin")
            else:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, str(exc))
        except Exception as exc:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:
        try:
            path = self.route_path()
            payload = self.read_json()
            if path == "/api/auth/admin":
                self.send_auth_response(login_admin(payload))
            elif path == "/api/auth/visitor":
                self.send_auth_response(login_visitor())
            elif path == "/api/auth/logout":
                delete_session(self.cookie_session_id())
                self.send_logout_response()
            elif path == "/api/chat":
                self.send_json(chat(payload, self.require_user()))
            elif path == "/api/chat/stream":
                self.send_chat_stream(payload)
            elif path == "/api/reindex":
                self.require_admin()
                self.send_json(reindex())
            elif path == "/api/admin/evals/run":
                self.require_admin()
                self.send_json(run_admin_evals(payload))
            elif match := re.match(r"^/api/admin/evals/(\d+)/analysis$", path):
                self.require_admin()
                self.send_json(admin_eval_analysis(int(match.group(1)), payload))
            elif match := re.match(r"^/api/admin/questions/(\d+)/feedback$", path):
                self.require_admin()
                self.send_json(update_feedback(int(match.group(1)), payload))
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
        except PermissionError as exc:
            self.send_error_json(HTTPStatus.UNAUTHORIZED, str(exc))
        except ValueError as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except LookupError as exc:
            self.send_error_json(HTTPStatus.NOT_FOUND, str(exc))
        except Exception as exc:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def send_auth_response(self, result: tuple[dict[str, Any], str]) -> None:
        data, session_id = result
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", self.session_cookie_header(session_id))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_logout_response(self) -> None:
        body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.clear_session_cookie()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.send_json({"detail": message}, status)

    def send_stream_event(self, event: dict[str, Any]) -> None:
        body = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        self.wfile.write(body)
        self.wfile.flush()

    def send_progress(self, message: str, items: list[str] | None = None) -> None:
        event: dict[str, Any] = {"type": "progress", "message": message}
        if items:
            event["items"] = items
        self.send_stream_event(event)

    def send_chat_stream(self, payload: dict[str, Any]) -> None:
        started = time.time()
        user = self.require_user()
        question, user_type, payload_conversation_id = validate_chat_payload(payload)
        user_type = user.get("role", user_type)
        conversation_id = get_or_create_conversation(user, payload_conversation_id, question)
        history = fetch_conversation_history(conversation_id, user=user)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        self.send_progress("理解问题")
        self.send_progress("识别意图")
        classification = classify_question_with_llm(question, history)
        category = classification["category"]
        rewritten_question = classification["rewritten_question"]
        user_message_id = log_message(
            user,
            conversation_id,
            "user",
            question,
            category,
            metadata={"rewritten_question": rewritten_question, "classification": classification},
        )
        self.send_progress("确定回答范围")

        if category == "unrelated":
            answer, missing_info, was_answered, answer_provider = generate_answer(question, category, [])
            sources: list[dict[str, Any]] = []
            retrieval_stats = {"index_count": len(INDEX), "candidate_count": 0, "returned_count": 0, "method": "none"}
            self.send_progress("生成边界说明")
            for part in chunk_text(answer):
                self.send_stream_event({"type": "delta", "text": part})
            quality_score = evaluate_quality(answer, sources, was_answered, missing_info)
            assistant_message_id = log_message(
                user,
                conversation_id,
                "assistant",
                answer,
                category,
                sources,
                {"answer_provider": answer_provider, "quality_score": quality_score, "missing_info": missing_info},
            )
            question_id = log_question(
                user,
                question,
                user_type,
                category,
                answer,
                sources,
                quality_score,
                was_answered,
                missing_info,
                answer_provider,
                conversation_id,
                assistant_message_id,
                classification["provider"],
                classification["reason"],
                rewritten_question,
                "none",
                retrieval_stats,
                [],
                len(history) // 2 + 1,
                int((time.time() - started) * 1000),
            )
            self.send_stream_event(
                {
                    "type": "done",
                    "id": question_id,
                    "conversation_id": conversation_id,
                    "user_message_id": user_message_id,
                    "message_id": assistant_message_id,
                    "category": category,
                    "classification_provider": classification["provider"],
                    "classification_reason": classification["reason"],
                    "rewritten_question": rewritten_question,
                    "answer_provider": answer_provider,
                    "quality_score": quality_score,
                    "was_answered": bool(was_answered),
                    "missing_info": missing_info,
                    "sources": sources,
                    "retrieval_stats": retrieval_stats,
                    "suggestions": suggest_followups(category, answer, sources),
                }
            )
            return

        if EMBEDDING_READY:
            self.send_progress("检索向量资料库")
        else:
            self.send_progress("检索职业资料")
        chunks, retrieval_stats = retrieve_with_stats(rewritten_question, category)
        sources = build_sources(chunks, rewritten_question)
        if sources:
            self.send_progress("筛选相关依据")
            self.send_stream_event({"type": "sources", "sources": sources})
        else:
            self.send_progress("确认资料边界")

        answer = ""
        missing_info = ""
        was_answered = 1
        answer_provider = selected_llm_provider()

        if category == "risk_boundary" or not chunks or (chunks and chunks[0]["score"] < MIN_SCORE):
            answer, missing_info, was_answered, answer_provider = generate_answer(question, category, chunks)
            self.send_progress("生成边界说明")
            for part in chunk_text(answer):
                self.send_stream_event({"type": "delta", "text": part})
        else:
            try:
                provider = selected_llm_provider()
                if provider == "minimax":
                    prompt = build_llm_user_prompt(question, category, chunks, history, rewritten_question)
                    self.send_progress("组织回答")
                    answer_provider, deltas = stream_minimax(prompt)
                    raw_answer = ""
                    sent_length = 0
                    for delta in deltas:
                        raw_answer += delta
                        visible = remove_thinking_for_stream(raw_answer)
                        new_text = visible[sent_length:]
                        if new_text:
                            self.send_stream_event({"type": "delta", "text": new_text})
                            sent_length = len(visible)
                    answer = strip_thinking(raw_answer)
                else:
                    answer, missing_info, was_answered, answer_provider = generate_answer(question, category, chunks)
                    self.send_progress("组织回答")
                    for part in chunk_text(answer):
                        self.send_stream_event({"type": "delta", "text": part})
                if not answer:
                    raise RuntimeError("LLM returned empty answer")
            except Exception as exc:
                safe_error = sanitize_error(str(exc))
                missing_info = f"LLM 调用失败：{safe_error}"
                answer_provider = "local_template_fallback"
                answer = generate_template_answer(question, category, chunks) + f"\n\n系统备注：LLM 调用失败，已回退到本地资料摘要。错误：{safe_error}"
                self.send_progress("切换备用回答方式")
                for part in chunk_text(answer):
                    self.send_stream_event({"type": "delta", "text": part})

        quality_score = evaluate_quality(answer, sources, was_answered, missing_info)
        assistant_message_id = log_message(
            user,
            conversation_id,
            "assistant",
            answer,
            category,
            sources,
            {
                "answer_provider": answer_provider,
                "quality_score": quality_score,
                "missing_info": missing_info,
                "retrieval_stats": retrieval_stats,
            },
        )
        question_id = log_question(
            user,
            question,
            user_type,
            category,
            answer,
            sources,
            quality_score,
            was_answered,
            missing_info,
            answer_provider,
            conversation_id,
            assistant_message_id,
            classification["provider"],
            classification["reason"],
            rewritten_question,
            retrieval_stats.get("method", ""),
            retrieval_stats,
            summarize_retrieved_chunks(chunks),
            len(history) // 2 + 1,
            int((time.time() - started) * 1000),
        )
        self.send_progress("整理来源")
        self.send_stream_event(
            {
                "type": "done",
                "id": question_id,
                "conversation_id": conversation_id,
                "user_message_id": user_message_id,
                "message_id": assistant_message_id,
                "category": category,
                "classification_provider": classification["provider"],
                "classification_reason": classification["reason"],
                "rewritten_question": rewritten_question,
                "answer_provider": answer_provider,
                "quality_score": quality_score,
                "was_answered": bool(was_answered),
                "missing_info": missing_info,
                "sources": sources,
                "retrieval_stats": retrieval_stats,
                "suggestions": suggest_followups(category, answer, sources),
            }
        )

    def send_file(self, path: Path, root: Path | None = None) -> None:
        resolved = path.resolve()
        if root is not None and root.resolve() not in [resolved, *resolved.parents]:
            self.send_error_json(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        if not resolved.exists() or not resolved.is_file():
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
            return
        body = resolved.read_bytes()
        content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        if resolved.suffix == ".js":
            content_type = "text/javascript"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server(host: str, port: int) -> None:
    load_local_env()
    init_db()
    stats = refresh_index()
    server = ThreadingHTTPServer((host, port), AvatarRequestHandler)
    print(f"Personal Avatar Agent running at http://{host}:{port}")
    print(f"Indexed {stats['chunks']} chunks from {stats['sources']} sources.")
    print(f"LLM provider: {selected_llm_provider()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Personal Avatar Agent MVP.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    run_server(args.host, args.port)
