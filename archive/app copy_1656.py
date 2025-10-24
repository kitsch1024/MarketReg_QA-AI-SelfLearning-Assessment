"""
App: 电力知识自适应学习 Demo

概述
- 单页 Streamlit 应用，加载本地 JSONL 题库，提供自适应学习体验。
- 特点：
  1) 支持按领域/类型/难度/标准筛选
  2) 题型支持：单选、多选、判断、填空、开放题（理解/案例等）
  3) 判分与锁定：提交后锁定，不可再修改；顶部实时展示正确率与能力
  4) 自适应：基于难度与相似度（Qdrant 向量检索）对后续题目动态重排；
     做对复杂题→相似简单题抑制；做错题→相似题增强
  5) 去重与稳定：使用 UID 去重，确保题目不重复、数量不减少
  6) 轮次总结：本轮总览、难度分布、错题回顾（题干/你的作答/参考答案/解析）

重要状态（st.session_state）
- items: 当前轮次所有题（按最终顺序）
- item_idx: 当前题索引
- answered_items / answers_by_item: 已提交记录（用于锁定与总结）
- correct_count / answered_count / ability: 顶部指标
- recent_correct_complex_ids: 用于"做对→相似简单抑制"的近邻参考
- show_summary: 是否显示本轮总结页

注意
- 不更改业务逻辑的前提下，尽量通过 docstring 与注释说明关键流程与设计意图。
"""
import os
import json
import io
import re
import time
from typing import Any, Dict, Generator, Iterable, List, Optional, Set, Tuple, Union

import streamlit as st
from typing import cast

# Adaptive imports
try:
    from adaptive.config import QdrantConfig, AdaptiveParams
    from adaptive.scorer import Scorer
    from adaptive.selector import Selector
    from adaptive.scheduler import Scheduler
    from qdrant_client import QdrantClient
    from qdrant_client.http import models as qmodels
except Exception:
    QdrantConfig = None  # type: ignore
    AdaptiveParams = None  # type: ignore
    Scorer = None  # type: ignore
    Selector = None  # type: ignore
    Scheduler = None  # type: ignore
    QdrantClient = None  # type: ignore
    qmodels = None  # type: ignore


# -------- JSON helpers (fast parser fallback) --------
try:
    import orjson  # type: ignore

    def json_loads(line: str) -> Any:
        return orjson.loads(line)
except Exception:  # pragma: no cover
    def json_loads(line: str) -> Any:
        return json.loads(line)


Item = Dict[str, Any]


def stream_jsonl(file_path: str) -> Generator[Item, None, None]:
    """Stream a JSONL file line by line as dicts."""
    with open(file_path, "r", encoding="utf-8") as f:
        for raw in f:
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                yield json_loads(stripped)
            except Exception:
                # Skip malformed lines in a best-effort demo
                continue


def collect_unique_values(file_path: str, show_progress: bool = True) -> Dict[str, Set[str]]:
    fields: Dict[str, Set[str]] = {
        "field": set(),
        "type": set(),
        "difficulty": set(),
        "doc": set(),
    }
    # Progress UI
    progress = st.progress(0.0) if show_progress else None
    total_bytes = os.path.getsize(file_path)
    processed_bytes = 0

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            processed_bytes += len(line.encode("utf-8"))
            try:
                raw_item = json_loads(line)
            except Exception:
                continue
            item = normalize_marketreg_item(raw_item)
            for key in fields.keys():
                if key in item and isinstance(item[key], str):
                    if len(fields[key]) < 5000:  # avoid huge memory for unique docs
                        fields[key].add(item[key])

            if progress is not None:
                progress.progress(min(0.999, processed_bytes / max(1, total_bytes)))

    if progress is not None:
        progress.progress(1.0)

    return fields


def get_type_display_name(type_key: str) -> str:
    """Convert internal type keys to English display names."""
    type_mapping = {
        "single_choice": "Single Choice",
        "multiple_choice": "Multiple Choice",
        "true_false": "True/False",
        "fill_blank": "Fill in the Blank",
        "case_analysis": "Case Analysis",
        "comprehension": "Comprehension",
        "short_answer": "Short Answer",
        "calculation": "Calculation",
        "essay": "Essay",
    }
    return type_mapping.get(str(type_key).strip().lower(), str(type_key))


def normalize_text(text: str) -> str:
    # Lowercase, remove common punctuation, spaces normalization for simple matching
    text = text.lower().strip()
    text = re.sub(r"[\s\u3000]+", " ", text)
    text = re.sub(r'[，,。；;、.！!？?：:"]', "", text)
    return text


def _strip_option_prefix(text: str) -> str:
    s = str(text).strip()
    m = re.match(r"^[A-Ha-h][\).。、】\]\s]*", s)
    if m:
        return s[m.end() :].strip()
    return s


def _is_true_false_choices(choices: Optional[List[str]]) -> bool:
    if not choices or len(choices) != 2:
        return False
    tf_true = {"true", "t", "正确", "对", "是", "√", "✓"}
    tf_false = {"false", "f", "错误", "错", "否", "×", "✗"}
    a = _strip_option_prefix(choices[0]).lower()
    b = _strip_option_prefix(choices[1]).lower()
    return (a in tf_true and b in tf_false) or (a in tf_false and b in tf_true)


def _infer_internal_type(raw_type: str, choices: Optional[List[str]]) -> str:
    t = str(raw_type).strip()
    # Direct pass-through for known internal types
    t_l = t.lower()
    if t_l in {"single_choice", "multiple_choice", "true_false", "fill_blank", "short_answer", "case_analysis", "comprehension", "calculation", "essay"}:
        return t_l

    # Map common Chinese labels with heuristics on choices
    if t in {"单选题", "选择题"}:
        return "single_choice"
    if t == "多选题":
        return "multiple_choice"
    if t == "判断题":
        # Many datasets label TF as 判断题, but some use A/B/C/D. Detect TF by options content.
        return "true_false" if _is_true_false_choices(choices) else "single_choice"
    if t == "填空题":
        # Some items marked 填空题 still provide A-D choices; treat as single_choice when choices exist
        return "single_choice" if (choices and len(choices) >= 2) else "fill_blank"
    if t in {"简答题"}:
        return "short_answer"

    # Fallback: use lower-cased raw type
    return t_l or "single_choice"


def normalize_marketreg_item(item: Item) -> Item:
    """Normalize MarketReg-style item to internal schema while preserving originals.

    - Map Chinese type labels to internal type keys
    - Promote `choices` -> `options`
    - Promote `context` -> `analysis`
    - Promote `category` -> `field`
    - Derive `difficulty` from `score` if absent (1-5 as string)
    """
    try:
        # Shallow copy to avoid mutating caller state
        out: Item = dict(item)
        # options
        if not out.get("options") and isinstance(out.get("choices"), list):
            out["options"] = [str(x) for x in out.get("choices", [])]
        # analysis
        if out.get("analysis") is None and out.get("context") is not None:
            out["analysis"] = out.get("context")
        # field/category
        if not out.get("field") and out.get("category"):
            out["field"] = str(out.get("category"))
        # type mapping with heuristics
        choices_list = out.get("options") if isinstance(out.get("options"), list) else None
        out["type"] = _infer_internal_type(str(out.get("type", "")), choices_list)
        # difficulty from score when missing; map to 1..5 digits as string
        if not out.get("difficulty"):
            score_val = out.get("score")
            try:
                if isinstance(score_val, (int, float)):
                    d = max(1, min(5, int(round(float(score_val)))))
                    out["difficulty"] = str(d)
            except Exception:
                pass
        # Ensure id is string-like
        if out.get("id") is not None:
            out["id"] = str(out.get("id"))
        return out
    except Exception:
        # Best-effort: return original item on any failure
        return item


def flatten_answers(answer: Any) -> List[str]:
    result: List[str] = []

    def _walk(val: Any) -> None:
        if val is None:
            return
        if isinstance(val, str):
            s = val.strip()
            if s:
                result.append(s)
            return
        if isinstance(val, (list, tuple)):
            for v in val:
                _walk(v)
            return
        # Ignore other types in demo

    _walk(answer)
    # Deduplicate while preserving order
    seen: Set[str] = set()
    deduped: List[str] = []
    for s in result:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


def parse_options_from_question(question: Union[str, List[str]]) -> Optional[List[str]]:
    # Some items embed options in the question text, like ["A. ...", "B. ..."]
    text = None
    if isinstance(question, str):
        text = question
    elif isinstance(question, list):
        # join parts
        try:
            text = "\n".join([str(x) for x in question])
        except Exception:
            text = "".join([str(x) for x in question])

    if not text:
        return None

    # Look for a Python-like list literal ... ['A. ...', 'B. ...']
    m = re.search(r"\[(?:\s*'[^']*'\s*,?\s*)+\]", text)
    if m:
        literal = m.group(0)
        try:
            import ast

            val = ast.literal_eval(literal)
            if isinstance(val, list) and all(isinstance(x, str) for x in val):
                return val
        except Exception:
            pass

    # Fallback: split by lines that look like options
    lines = [ln.strip() for ln in text.splitlines()]
    opts = [ln for ln in lines if re.match(r"^[A-Ha-h][\).。]\s*", ln)]
    if opts:
        return opts
    return None


def _badge(label: str, value: str) -> str:
    return (
        "<span class=\"badge-pill\" style=\"display:inline-block;padding:4px 10px;margin:2px 8px 8px 0;"
        "border-radius:999px;background:rgba(0,229,255,.06);color:#E6F7FF;font-size:12px;line-height:18px;"
        "box-shadow: inset 0 0 0 1px rgba(0,229,255,.25), 0 0 12px rgba(0,229,255,.08);\">"
        f"<span class='lbl'>{label}</span> {value}</span>"
    )


def inject_neon_theme() -> None:
    """Inject global dark-gradient + neon glow theme. Purely visual; no logic change."""
    st.markdown(
        """
        <style>
        :root{
          --bg-a:#e7e9eb; --bg-b:#dde4e6; --bg-c:#d1dddf;
          --primary:#00b4c8; --warning:#ffa726; --ok:#4caf50; --error:#ef5350; --text:#2d3436;
          --panel: rgba(225,233,235,.55);
        }
        html, body, .stApp{
          background: linear-gradient(135deg, var(--bg-a) 0%, var(--bg-b) 50%, var(--bg-c) 100%) fixed !important;
          color: var(--text) !important;
        }
        /* Top holographic header */
        .holo-header{ position: sticky; top: 0; z-index: 999; margin: -8px -16px 16px -16px; padding: 8px 16px; 
          background: linear-gradient(180deg, rgba(255,255,255,.6), rgba(255,255,255,.25));
          backdrop-filter: saturate(1.2) blur(6px);
          box-shadow: inset 0 0 0 1px rgba(0,180,200,.15), 0 6px 12px rgba(0,0,0,.06);
        }
        .holo-grid{ display:flex; gap:16px; align-items:center; }
        .holo-tile{ flex:1; border-radius:12px; padding:8px 12px; 
          background: rgba(255,255,255,.75); box-shadow: inset 0 0 0 1px rgba(0,180,200,.15);
        }
        .holo-label{ font-size:12px; color: rgba(45,52,54,.7); margin-bottom:2px }
        .holo-led{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, "Liberation Mono", monospace; 
          font-weight:700; font-size:22px; letter-spacing:1px; color:#2d3436; text-shadow: 0 0 0 rgba(0,0,0,0); }
        .holo-led.cyan{ color: var(--primary) }
        .holo-led.green{ color: var(--ok) }
        /* Main content container as a soft card */
        .block-container{
          border-radius:16px;
          background: linear-gradient(180deg, rgba(255,255,255,.25), rgba(255,255,255,0) 10%), var(--panel);
          box-shadow: inset 0 0 0 1px rgba(0,180,200,.15), 0 18px 48px rgba(0,0,0,.08);
          backdrop-filter: saturate(1.08) blur(6px);
        }
        /* Sidebar */
        /* Move sidebar to the right */
        [data-testid="stAppViewContainer"]{ flex-direction: row-reverse !important }
        [data-testid="stSidebar"] {
          background: linear-gradient(180deg, rgba(255,255,255,.35), rgba(255,255,255,0) 12%), var(--panel) !important;
          box-shadow: inset 0 0 0 1px rgba(0,180,200,.15);
          width:56px !important; min-width:56px !important; transition: width .25s ease, min-width .25s ease; overflow:hidden;
        }
        /* Keep expanded while interacting: hover, focus within, combobox open, or any BaseWeb popover menu */
        [data-testid="stSidebar"]:hover,
        [data-testid="stSidebar"]:focus-within,
        [data-testid="stSidebar"]:has([role="combobox"][aria-expanded="true"]),
        body:has([data-baseweb="popover"] [data-baseweb="menu"]) [data-testid="stSidebar"]{
          width:320px !important; min-width:320px !important;
        }
        /* Collapsed state: tidy text to avoid clutter, show a simple icon */
        [data-testid="stSidebar"]::before{ content:"☰"; display:block; color: var(--text); font-size:18px; padding:10px 8px; opacity:.85 }
        [data-testid="stSidebar"]:not(:hover):not(:focus-within):not(:has([role="combobox"][aria-expanded="true"])):not(:has([data-baseweb="popover"] [data-baseweb="menu"])) label,
        [data-testid="stSidebar"]:not(:hover):not(:focus-within):not(:has([role="combobox"][aria-expanded="true"])):not(:has([data-baseweb="popover"] [data-baseweb="menu"])) .stMarkdown,
        [data-testid="stSidebar"]:not(:hover):not(:focus-within):not(:has([role="combobox"][aria-expanded="true"])):not(:has([data-baseweb="popover"] [data-baseweb="menu"])) [data-testid="stTextInputRoot"],
        [data-testid="stSidebar"]:not(:hover):not(:focus-within):not(:has([role="combobox"][aria-expanded="true"])):not(:has([data-baseweb="popover"] [data-baseweb="menu"])) [data-baseweb="select"],
        [data-testid="stSidebar"]:not(:hover):not(:focus-within):not(:has([role="combobox"][aria-expanded="true"])):not(:has([data-baseweb="popover"] [data-baseweb="menu"])) [data-testid="stSlider"]{
          display:none !important;
        }
        [data-testid="stSidebar"]:not(:hover):not(:focus-within) *{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis }

        /* Select/Multiselect: selected items/chips and active options use light blue, not red */
        [data-baseweb="tag"]{ background: rgba(0,180,200,.12) !important; border: 1px solid rgba(0,180,200,.25) !important }
        [data-baseweb="menu"] div[role="option"][aria-selected="true"],
        [data-baseweb="menu"] div[role="option"][aria-checked="true"]{
          background: rgba(0,180,200,.15) !important; color: var(--text) !important;
        }
        [data-testid="stSidebar"]:before{ content:"☰"; display:block; color:#2d3436; font-size:18px; padding:10px 8px; }
        [data-testid="stSidebar"] * { color: var(--text) !important; }

        /* Headings */
        h1,h2,h3,h4,h5,h6{ color: var(--text) !important; letter-spacing:.2px }

        /* Buttons */
        .stButton>button{
          background: linear-gradient(90deg, rgba(0,180,200,.18), rgba(255,167,38,.18));
          color: var(--text); border: none; border-radius:12px; padding:0.5rem 0.9rem;
          box-shadow: inset 0 0 0 1px rgba(0,180,200,.15), 0 2px 8px rgba(0,0,0,.06);
          transition: all .15s ease;
        }
        .stButton>button:hover{ transform: translateY(-1px); box-shadow: inset 0 0 0 1px rgba(0,180,200,.22), 0 4px 12px rgba(0,0,0,.10) }
        .stButton>button:focus{ outline: none; box-shadow: 0 0 0 2px rgba(0,229,255,.3) inset }
        /* Beam-style bottom action bar */
        .beam-bar{ position: sticky; bottom: 8px; z-index: 9; padding: 8px 0; margin-top: 8px }
        .beam-bar .stButton>button{ 
          background: linear-gradient(90deg, rgba(0,180,200,.35), rgba(0,180,200,.15));
          box-shadow: inset 0 0 0 1px rgba(0,180,200,.20), 0 0 0 2px rgba(0,0,0,0), 0 2px 8px rgba(0,0,0,.06);
        }
        .beam-bar .stButton>button:active{ box-shadow: inset 0 0 0 1px rgba(0,180,200,.3), 0 0 16px rgba(0,180,200,.25) }

        /* Inputs - light style */
        input, textarea, select{ color: var(--text) !important }
        .stTextInput>div>div, .stTextArea>div>div, .stSelectbox>div>div, .stMultiSelect>div>div{
          background: rgba(255,255,255,.75) !important;
          border: none !important; border-radius:12px !important;
          box-shadow: inset 0 0 0 1px rgba(0,180,200,.15) !important;
        }
        .stTextInput>div>div>input, .stTextArea textarea{
          background: transparent !important; color: var(--text) !important;
        }
        /* Sidebar Dataset Path: make its text black for better path readability */
        [data-testid="stSidebar"] .stTextInput:first-of-type input{
          color:#000000 !important; caret-color:#000000 !important;
        }
        [data-testid="stSidebar"] .stTextInput:first-of-type input::placeholder{ color: rgba(0,0,0,.6) !important }
        /* BaseWeb select internals (for sidebar and main) */
        [data-baseweb="select"]>div, [data-baseweb="select"] [role="combobox"]{
          background: rgba(255,255,255,.75) !important; color: var(--text) !important;
          border-radius:12px !important; box-shadow: inset 0 0 0 1px rgba(0,180,200,.15) !important;
        }
        [data-baseweb="select"] svg, [data-baseweb="select"] svg path{ color: var(--text) !important; fill: var(--text) !important }
        /* Select dropdown menu */
        [data-baseweb="menu"], [data-baseweb="popover"] [data-baseweb="menu"]{
          background: rgba(255,255,255,.98) !important; color: var(--text) !important;
          box-shadow: inset 0 0 0 1px rgba(0,180,200,.15), 0 8px 22px rgba(0,0,0,.10) !important;
        }
        [data-baseweb="menu"] div[role="option"]{ color: var(--text) !important }
        .stSlider>div>div>div>div{ background: rgba(0,0,0,.08) }
        .stSlider [data-baseweb="slider"]>div>div{ background: linear-gradient(90deg, var(--primary), var(--ok)) !important }

        /* Radio/Checkbox highlight */
        .stRadio>div [data-baseweb], .stCheckbox>label>div:first-child{
          box-shadow: inset 0 0 0 1px rgba(0,180,200,.25);
        }
        /* Cinema center layout for main learning area */
        .cinema-center{ width:60vw; margin: 0 auto; }
        /* Pseudo-slide: one option per viewport height (scroll to switch) */
        .stRadio [role="radiogroup"], .stMultiSelect>div>div{ max-height:62vh; overflow-y:auto; scroll-snap-type: y mandatory }
        .stRadio label{ display:flex; align-items:center; justify-content:flex-start; min-height:58vh; scroll-snap-align:start; 
          border-radius:12px; margin-bottom:10px; padding:16px; background: rgba(255,255,255,.85); box-shadow: inset 0 0 0 1px rgba(0,180,200,.12) }
        /* Light theme option text */
        .stRadio label, .stRadio span, .stRadio p{ color: var(--text) !important }
        .stMultiSelect [data-baseweb="tag"] div{ color: var(--text) !important }
        .stMultiSelect input{ color: var(--text) !important }

        /* Expander */
        details>summary{ background: rgba(255,255,255,.04); border-radius: 12px; padding:.6rem .9rem;
          box-shadow: inset 0 0 0 1px rgba(0,229,255,.18) }
        details[open]{ box-shadow: inset 0 0 0 1px rgba(0,229,255,.22), 0 0 16px rgba(0,229,255,.12) }

        /* Progress (built-in) */
        [data-testid="stProgressBar"]>div>div{ background: linear-gradient(90deg, var(--primary), var(--ok)) !important; border-radius: 6px }

        /* Metrics */
        [data-testid="stMetricValue"]{ color: var(--text) !important }
        [data-testid="stMetricLabel"]{ color: rgba(45,52,54,.6) !important }

        /* Alerts */
        .stAlert{ color: var(--text) !important; background: rgba(255,255,255,.75) !important;
          border-radius: 12px !important; box-shadow: inset 0 0 0 1px rgba(0,180,200,.15) !important }

        /* Tables & Dataframes */
        .stTable, .stDataFrame{ filter: drop-shadow(0 2px 8px rgba(0,0,0,.06)) }
        .stDataFrame [data-testid="stTable"][role="table"]{ color: var(--text) }
        .stDataFrame thead tr th{ background: rgba(255,255,255,.85) }

        /* Badges */
        .badge-pill{ display:inline-block; padding:4px 10px; border-radius:999px; color:var(--text);
          background: rgba(0,180,200,.08); box-shadow: inset 0 0 0 1px rgba(0,180,200,.15) }
        .badge-pill .lbl{ color:var(--primary); font-weight:700; margin-right:6px }

        /* Hide duplicate top metrics; keep metrics visible inside summary cards */
        [data-testid="stMetric"]{ display:none !important }
        .sumCard [data-testid="stMetric"]{ display:flex !important }

        /* App Title & Subtitle */
        .appTitle{ font-size:42px; font-weight:800; color: var(--primary); letter-spacing:.5px; margin:6px 0 4px }
        .appSubtitle{ color: rgba(45,52,54,.82); font-size:14px; line-height:1.7; margin:0 0 14px }

        /* Top CTA four-square full row (robust selectors for columns right after .top-cta) */
        .top-cta ~ [data-testid="column"],
        .top-cta ~ div [data-testid="column"],
        .top-cta ~ div div[data-testid="column"]{ padding: 0 8px }
        .top-cta ~ [data-testid="column"] .stButton>button,
        .top-cta ~ div [data-testid="column"] .stButton>button,
        .top-cta ~ div div[data-testid="column"] .stButton>button{
          display:flex; align-items:center; justify-content:center; flex-direction:column;
          width:100% !important; aspect-ratio:1 / 1; border-radius:14px; font-size:18px;
          white-space: pre-line; text-align:center;
          background: linear-gradient(180deg, rgba(0,180,200,.22), rgba(0,180,200,.08));
          box-shadow: inset 0 0 0 1px rgba(0,180,200,.22), 0 6px 14px rgba(0,0,0,.08);
        }
        .top-cta ~ [data-testid="column"] .stButton>button:hover,
        .top-cta ~ div [data-testid="column"] .stButton>button:hover,
        .top-cta ~ div div[data-testid="column"] .stButton>button:hover{ transform: translateY(-1px); box-shadow: inset 0 0 0 1px rgba(0,180,200,.28), 0 10px 18px rgba(0,0,0,.12) }
        .top-cta ~ [data-testid="column"] .stButton>button:active,
        .top-cta ~ div [data-testid="column"] .stButton>button:active,
        .top-cta ~ div div[data-testid="column"] .stButton>button:active{ box-shadow: inset 0 0 0 1px rgba(0,180,200,.35), 0 0 18px rgba(0,180,200,.25) }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_item_badges(item: Item) -> None:
    type_key = str(item.get("type", "-"))
    type_display = get_type_display_name(type_key) if type_key != "-" else "-"
    badges = [
        _badge("field", str(item.get("field", "-"))),
        _badge("type", type_display),
        _badge("difficulty", str(item.get("difficulty", "-"))),
        _badge("Source of regulatory documents", str(item.get("doc", "-"))),
    ]
    st.markdown(" ".join(badges), unsafe_allow_html=True)


# ---------------- Adaptive helpers (non-intrusive; use if available) ----------------
def get_item_uid(item: Item) -> str:
    # Prefer dataset id if present and non-empty
    raw = str(item.get("id", "")).strip()
    if raw:
        return raw
    # Stable hash from essential fields
    import hashlib
    parts = []
    q = item.get("question")
    # 归一化 question 为字符串
    if isinstance(q, list):
        _qparts = []
        for x in q:
            if isinstance(x, dict):
                try:
                    _qparts.append(json.dumps(x, ensure_ascii=False))
                except Exception:
                    _qparts.append(str(x))
            else:
                _qparts.append(str(x))
        q_text_norm = "\n".join(_qparts)
    elif isinstance(q, dict):
        try:
            q_text_norm = json.dumps(q, ensure_ascii=False)
        except Exception:
            q_text_norm = str(q)
    else:
        q_text_norm = str(q) if q is not None else ""
    parts.append(q_text_norm)
    parts.append(str(item.get("doc", "")))
    parts.append(str(item.get("field", "")))
    # 'standard' removed from filters; keep backward compatibility in UID generation if present
    parts.append(str(item.get("standard", "")))
    h = hashlib.md5("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"uid_{h}"


def _now_ms() -> int:
    """当前毫秒时间戳。用于记录学习行为与作答时长。"""
    import time
    return int(time.time() * 1000)


def _history_dir() -> str:
    """历史数据目录（本地持久化）：项目目录/data/history。

    - 轮次汇总：rounds.jsonl（一行一轮，摘要信息）
    - 轮次明细：rounds/{ts}_detail.json（题目与作答详情）
    """
    base = os.path.join(os.path.dirname(__file__), "data", "history")
    try:
        os.makedirs(os.path.join(base, "rounds"), exist_ok=True)
    except Exception:
        pass
    return base


def _start_new_round_from_filters() -> None:
    """基于当前会话中的 dataset_path + filters 开启新一轮题目（与统计页顶部逻辑一致）。"""
    dataset_path = st.session_state.get("dataset_path") or ""
    filters = st.session_state.get("filters", {}) or {}
    if not dataset_path or not os.path.exists(dataset_path):
        st.warning(" Invalid dataset path")
        return
    selected_fields = set(filters.get("field", []))
    selected_types = set(filters.get("type", []))
    selected_difficulties = set(filters.get("difficulty", []))
    selected_docs = set(filters.get("doc", []))
    max_items = len(st.session_state.get("round_uids", [])) or 50

    # 重置轮次标志
    st.session_state.show_summary = False
    st.session_state.round_persisted = False

    with st.spinner(" Loading next round ..."):
        items = load_filtered_items(
            dataset_path,
            selected_fields,
            selected_types,
            selected_difficulties,
            selected_docs,
            max_items,
        )
    if not items:
        st.warning(" No items matched filters")
        return

    selector = _ensure_selector()
    ability = float(st.session_state.get("ability", 1.0))
    if selector is not None:
        # 去重
        seen_uids = set()
        unique_items = []
        for it in items:
            uid = get_item_uid(it)
            if uid in seen_uids:
                continue
            seen_uids.add(uid)
            unique_items.append(it)
        items = unique_items
        metas = []
        for it in items:
            metas.append({
                "id": get_item_uid(it),
                "field": it.get("field"),
                "type": it.get("type"),
                "difficulty_str": it.get("difficulty"),
                "difficulty_num": _parse_difficulty_num(it.get("difficulty")),
                "standard": it.get("standard"),
                "doc": it.get("doc"),
                "knowledge_points": it.get("knowledge_points", []) if isinstance(it.get("knowledge_points"), list) else [it.get("knowledge_points")] if isinstance(it.get("knowledge_points"), str) else [],
            })
        class _M:
            def __init__(self, d):
                self.__dict__.update(d)
        class _S:
            def __init__(self, ability):
                self.ability = ability
                self.review_schedule = {}
                self.kp_mastery = {}
                self.answers_by_item = {}
        state_adapter = _S(ability)
        def get_neighbors_fn(src_id: str):
            params = st.session_state.get("_adaptive_params")
            if params is None:
                return None
            return _get_neighbors_recommend(src_id, cast(Any, params).topk_neighbors)
        def get_complex_difficulty_fn(src_id: str) -> int:
            for it in items:
                if get_item_uid(it) == src_id:
                    return _parse_difficulty_num(it.get("difficulty"))
            return 3
        picks = selector.choose(
            [_M(m) for m in metas],
            state_adapter,
            cast(List[str], st.session_state.get("recent_correct_complex_ids", [])),
            get_neighbors_fn,
            get_complex_difficulty_fn,
            k=len(items)
        )
        id_to_item = {get_item_uid(it): it for it in items}
        items_sorted = []
        seen_pick = set()
        for p in picks:
            if p.id in seen_pick:
                continue
            seen_pick.add(p.id)
            it = id_to_item.get(p.id)
            if it is not None:
                items_sorted.append(it)
        if len(items_sorted) < len(items):
            picked_uids = {get_item_uid(it) for it in items_sorted}
            for it in items:
                uid = get_item_uid(it)
                if uid not in picked_uids:
                    items_sorted.append(it)
            items_sorted = items_sorted[: len(items)]
        if not items_sorted:
            items_sorted = sort_items_for_adaptive(items, ability)
    else:
        # 仅按难度排序
        seen_uids = set()
        dedup_items = []
        for it in items:
            uid = get_item_uid(it)
            if uid in seen_uids:
                continue
            seen_uids.add(uid)
            dedup_items.append(it)
        items_sorted = sort_items_for_adaptive(dedup_items, ability)

    # 刷新本轮会话状态
    st.session_state.items = items_sorted
    st.session_state.item_idx = 0
    st.session_state.correct_count = 0
    st.session_state.answered_count = 0
    st.session_state.answered_items = set()
    st.session_state.answers_by_item = {}
    st.session_state.recent_correct_complex_ids = []
    st.session_state.round_uids = [get_item_uid(it) for it in items_sorted]
    st.session_state.round_start_ms = _now_ms()
    st.success(f" Loaded {len(items_sorted)} items")
    st.rerun()

def _persist_round_to_disk() -> None:
    """在本轮结束时将摘要与明细写入本地文件，避免程序重启后记录丢失。

    仅在尚未持久化过（round_persisted=False）时执行一次。
    """
    if st.session_state.get("round_persisted"):
        return
    items: Optional[List[Item]] = st.session_state.get("items")
    if not items:
        return
    answers: Dict[str, Any] = st.session_state.get("answers_by_item", {}) or {}
    round_uids: List[str] = st.session_state.get("round_uids", []) or [get_item_uid(it) for it in items]
    r_start = int(st.session_state.get("round_start_ms", _now_ms()))
    r_end = int(st.session_state.get("round_end_ms", _now_ms()))
    duration_ms = max(0, r_end - r_start)
    total = len(round_uids)
    answered_ids = [uid for uid in round_uids if uid in answers]
    answered = len(answered_ids)
    correct = sum(1 for uid in answered_ids if isinstance(answers.get(uid), dict) and answers.get(uid).get("is_correct") is True)
    acc = (correct / answered) if answered else 0.0
    # 简要过滤条件快照
    filters_snapshot = st.session_state.get("filters", {})

    # 汇总行
    summary = {
        "ts_ms": r_end,
        "duration_ms": duration_ms,
        "total": total,
        "answered": answered,
        "correct": correct,
        "accuracy": acc,
        "filters": filters_snapshot,
    }
    # 明细：每题UID与作答记录（若有）、题目元信息（文档/难度/类型）
    uid_to_item = {get_item_uid(it): it for it in items}
    details = []
    for uid in round_uids:
        it = uid_to_item.get(uid)
        rec = answers.get(uid)
        if not it:
            continue
        # 题干与选项/参考答案提取
        q = it.get("question")
        # question 可能为 list[str|dict] / dict / str / None，需安全归一化为字符串
        if isinstance(q, list):
            parts = []
            for x in q:
                if isinstance(x, dict):
                    try:
                        parts.append(json.dumps(x, ensure_ascii=False))
                    except Exception:
                        parts.append(str(x))
                else:
                    parts.append(str(x))
            q_text = "\n".join(parts)
        elif isinstance(q, dict):
            try:
                q_text = json.dumps(q, ensure_ascii=False)
            except Exception:
                q_text = str(q)
        else:
            q_text = str(q) if q is not None else ""
        options = it.get("options")
        if isinstance(options, list):
            options_disp = "; ".join([str(x) for x in options])
        else:
            # 回退：从题干解析选项
            try:
                parsed_opts = parse_options_from_question(q_text)
                options_disp = "; ".join(parsed_opts) if parsed_opts else None
            except Exception:
                options_disp = None
        ans_field = it.get("answer")
        if isinstance(ans_field, list):
            correct_disp = "; ".join([str(x) for x in ans_field])
        elif ans_field is None:
            correct_disp = None
        else:
            correct_disp = str(ans_field)
        # 用户作答展示
        user_disp = None
        if isinstance(rec, dict):
            if "display_choice" in rec and rec.get("display_choice") is not None:
                user_disp = str(rec.get("display_choice"))
            elif "choice" in rec and rec.get("choice") is not None:
                # 单选（字母）
                user_disp = str(rec.get("choice"))
            elif "choice_text" in rec and rec.get("choice_text"):
                user_disp = str(rec.get("choice_text"))
            elif "choices" in rec and isinstance(rec.get("choices"), list):
                user_disp = "; ".join([str(x) for x in rec.get("choices")])
            elif "text" in rec and rec.get("text") is not None:
                user_disp = str(rec.get("text"))
        details.append({
            "uid": uid,
            "doc": it.get("doc"),
            "difficulty": it.get("difficulty"),
            "type": it.get("type"),
            "field": it.get("field"),
            "is_correct": rec.get("is_correct") if isinstance(rec, dict) else None,
            # 记录该题是否已提交（便于历史中区分未作答 vs 作答错/对）
            "answered": bool(isinstance(rec, dict)),
            # 新增：题干/选项/参考答案/用户作答，用于历史页复盘
            "question": q_text,
            "options": options_disp,
            "correct_answer": correct_disp,
            "user_answer": user_disp,
        })

    # 写入磁盘
    base = _history_dir()
    rounds_path = os.path.join(base, "rounds.jsonl")
    detail_path = os.path.join(base, "rounds", f"{r_end}_detail.json")
    try:
        with open(detail_path, "w", encoding="utf-8") as f:
            json.dump({
                "ts_ms": r_end,
                "duration_ms": duration_ms,
                "items": details,
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    # 在汇总里记录明细文件相对路径
    summary["detail_file"] = os.path.join("rounds", f"{r_end}_detail.json")
    try:
        with open(rounds_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
        st.session_state.round_persisted = True
    except Exception:
        pass


def _load_recent_rounds(limit: int = 10) -> List[Dict[str, Any]]:
    """读取最近 N 轮记录（从 rounds.jsonl 末尾向前取）。"""
    path = os.path.join(_history_dir(), "rounds.jsonl")
    if not os.path.exists(path):
        return []
    rounds: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rounds.append(json.loads(line))
            except Exception:
                continue
            if len(rounds) >= limit:
                break
        rounds.reverse()
    except Exception:
        return []
    return rounds


def render_learning_records() -> None:
    """学习记录页面：展示最近 N 轮的摘要指标与趋势（美化版）。"""
    # 局部样式
    st.markdown(
        """
        <style>
        .stCard{
          padding:12px 16px; border-radius:14px; margin-bottom:14px;
          background: linear-gradient(180deg, rgba(255,255,255,.25), rgba(255,255,255,0) 10%), rgba(225,233,235,.55);
          box-shadow: inset 0 0 0 1px rgba(0,180,200,.15), 0 8px 22px rgba(0,0,0,.08);
        }
        .badge{display:inline-block;background:rgba(0,180,200,.10);color:#2d3436;padding:2px 10px;border-radius:999px;font-size:12px;margin-right:6px;margin-bottom:6px;box-shadow: inset 0 0 0 1px rgba(0,180,200,.15)}
        .kpi{font-size:13px;color:rgba(45,52,54,.7)}
        .kpi-strong{font-size:18px;font-weight:700;color:#00b4c8}
        .section-title{font-weight:800;font-size:18px;margin:4px 0 10px;color:#2d3436}
        .softDivider{height:1px;margin:14px 0;background:linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(0,180,200,.25) 20%, rgba(0,180,200,.25) 80%, rgba(0,0,0,0) 100%)}
        .small{color:rgba(45,52,54,.6);font-size:12px}
        .progress{background:rgba(0,0,0,.08);border-radius:6px;height:8px;overflow:hidden}
        .progress > div{height:100%;background:linear-gradient(90deg, #00b4c8, #4caf50)}
        /* Dataframe light theme */
        .stDataFrame [data-testid="stTable"][role="table"]{ color:#2d3436 }
        .stDataFrame thead tr th{ background: rgba(255,255,255,.9) !important; color:#2d3436 }
        .stDataFrame tbody tr td{ background: rgba(255,255,255,.85) !important; color:#2d3436 }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # 顶部标题（缩小级别）
    st.markdown("**Learning Records** · Recent rounds")

    # 选择显示最近多少轮（统一使用字符串避免类型混用导致状态错乱）
    col_sel, _ = st.columns([2, 5])
    with col_sel:
        show_opts_labels = ["5", "10", "20", "50", "100", "All"]
        # 记住上次选择，默认"10"
        default_label = st.session_state.get("history_limit_label", "10")
        try:
            default_index = show_opts_labels.index(default_label)
        except Exception:
            default_index = 1
        sel_label = st.selectbox("Show last", show_opts_labels, index=default_index, key="history_limit_select")
        st.session_state["history_limit_label"] = sel_label
        if sel_label == "All":
            limit = 1000
        else:
            try:
                limit = int(sel_label)
            except Exception:
                limit = 10

    data = _load_recent_rounds(limit=limit)
    if not data:
        st.info(" No history yet")
        return
    # 确保按时间降序（最近在前）显示
    try:
        data_sorted = sorted(data, key=lambda r: int(r.get("ts_ms", 0)), reverse=True)
    except Exception:
        data_sorted = data
    # 顶部 KPI
    try:
        avg_acc = sum([float(r.get("accuracy", 0.0)) for r in data_sorted]) / max(1, len(data_sorted))
        avg_answered = sum([int(r.get("answered", 0)) for r in data_sorted]) / max(1, len(data_sorted))
        # 最近一次的时间应取排序后的第一个
        last_ts = int(data_sorted[0].get("ts_ms", 0)) if data_sorted else 0
        last_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last_ts/1000)) if last_ts else "--"
        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown("Recent Rounds")
            st.markdown(f"<div class='kpi-strong'>{len(data)}</div>", unsafe_allow_html=True)
            st.markdown("<div class='small'>Count</div>", unsafe_allow_html=True)
        with c2:
            st.markdown("Avg. Accuracy")
            st.markdown(f"<div class='kpi-strong'>{avg_acc:.0%}</div>", unsafe_allow_html=True)
            st.markdown("<div class='small'>Per round</div>", unsafe_allow_html=True)
        with c3:
            st.markdown("Avg. Completed")
            st.markdown(f"<div class='kpi-strong'>{avg_answered:.1f}</div>", unsafe_allow_html=True)
            st.markdown(f"<div class='small'>Last: {last_when}</div>", unsafe_allow_html=True)
    except Exception:
        pass

    # 趋势图：使用 Altair 更稳健地渲染折线+柱图
    import pandas as pd
    try:
        rows = []
        for r in data_sorted:
            ts = int(r.get("ts_ms", 0))
            rows.append({
                "time": time.strftime("%m-%d %H:%M", time.localtime(ts / 1000)) if ts else "--",
                "accuracy_pct": float(r.get("accuracy", 0.0)) * 100.0,
                "answered": int(r.get("answered", 0)),
            })
        df = pd.DataFrame(rows)
        st.subheader("Trends")
        try:
            import altair as alt
            # Light theme colors (match current palette)
            primary = '#00b4c8'
            amber = '#ffa726'
            text_col = '#2d3436'
            x_axis = alt.Axis(title='Time', labelAngle=0, labelOverlap=True, labelPadding=8, tickSize=3,
                              labelColor=text_col, titleColor=text_col)
            # Bar for answered count
            bar = alt.Chart(df, background='transparent').mark_bar(opacity=0.55, color=amber).encode(
                x=alt.X('time:N', axis=x_axis),
                y=alt.Y('answered:Q', title='Completed', axis=alt.Axis(labelColor=text_col, titleColor=text_col)),
                tooltip=['time','answered']
            )
            # Line for accuracy
            line = alt.Chart(df, background='transparent').mark_line(point=True, color=primary, strokeWidth=2).encode(
                x=alt.X('time:N', axis=x_axis),
                y=alt.Y('accuracy_pct:Q', title='Accuracy (%)', axis=alt.Axis(orient='right', labelColor=text_col, titleColor=text_col)),
                tooltip=['time','accuracy_pct']
            )
            chart = alt.layer(bar, line).resolve_scale(y='independent').configure_axis(
                grid=False
            ).configure_view(stroke=None)
            st.altair_chart(chart, use_container_width=True)
        except Exception:
            # Fallback to simple charts
            st.line_chart(df.set_index('time')[['accuracy_pct']])
            st.bar_chart(df.set_index('time')[['answered']])
    except Exception:
        st.warning("Failed to render trends")
    st.markdown("<div class='section-title'>Recent Rounds</div>", unsafe_allow_html=True)
    for r in data_sorted:
        ts = int(r.get("ts_ms", 0))
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts / 1000)) if ts else "--"
        answered = int(r.get('answered',0))
        total = int(r.get('total',0))
        acc = float(r.get('accuracy',0.0))
        dur_s = int(r.get('duration_ms',0))/1000
        ratio = (answered / total) if total else 0.0
        # 卡片头部信息
        st.markdown("<div class='stCard'>", unsafe_allow_html=True)
        top_cols = st.columns([3, 2, 2, 2, 2])
        top_cols[0].markdown(f"**{when}**")
        top_cols[1].markdown(f"Completed: {answered}/{total}")
        top_cols[2].markdown(f"Correct: {int(r.get('correct',0))}")
        top_cols[3].markdown(f"Accuracy: {acc:.0%}")
        top_cols[4].markdown(f"Duration: {dur_s:.1f}s")
        # 进度条
        st.markdown(
            f"<div class='progress'><div style='width:{ratio*100:.0f}%;'></div></div>",
            unsafe_allow_html=True,
        )
        # 过滤快照（默认展示）
        filt = r.get("filters", {}) or {}
        if isinstance(filt, dict):
            try:
                fields = filt.get("field") or []
                types = filt.get("type") or []
                diffs = filt.get("difficulty") or []
                stds = filt.get("doc") or []
                types_cn = [get_type_display_name(t) for t in types]
                badges = []
                if fields:
                    badges.append(_badge("field", ", ".join(fields)))
                if types_cn:
                    badges.append(_badge("type", ", ".join(types_cn)))
                if diffs:
                    badges.append(_badge("difficulty", ", ".join(diffs)))
                if stds:
                    _docs_text = ", ".join(stds)
                    if len(_docs_text) > 100:
                        _docs_text = _docs_text[:100] + "…"
                    badges.append(_badge("Source of regulatory documents", _docs_text))
                if badges:
                    st.markdown(" ".join(badges), unsafe_allow_html=True)
                st.caption("Filters")
                try:
                    import pandas as pd
                    rows = [
                        {"Category":"Field","Value":", ".join(fields) or "(All)"},
                        {"Category":"Type","Value":", ".join(types_cn) or "(All)"},
                        {"Category":"Difficulty","Value":", ".join(diffs) or "(All)"},
                        {"Category":"Source of regulatory documents","Value":", ".join(stds) or "(All)"},
                    ]
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                except Exception:
                    st.table([
                        ["field", ", ".join(fields) or "(All)"],
                        ["type", ", ".join(types_cn) or "(All)"],
                        ["difficulty", ", ".join(diffs) or "(All)"],
                        ["Source of regulatory documents", ", ".join(stds) or "(All)"],
                    ])
            except Exception:
                st.json(filt)
        else:
            st.json(filt)

        # 题目明细（需展开）
        with st.expander("Items"):
            detail_file = r.get("detail_file")
            if detail_file:
                st.caption(f"Detailed file: {os.path.join(_history_dir(), detail_file)}")
                try:
                    with open(os.path.join(_history_dir(), detail_file), "r", encoding="utf-8") as df:
                        detail_json = json.load(df)
                    items = detail_json.get("items", [])
                    preview = []
                    for it in items[:50]:
                        preview.append({
                            "uid": it.get("uid"),
                            "doc": it.get("doc"),
                            "type": get_type_display_name(str(it.get("type"))),
                            "difficulty": str(it.get("difficulty")),
                            "answered": bool(it.get("answered")),
                            "is_correct": it.get("is_correct"),
                            "question": (it.get("question") or "")[:200],
                            "user_answer": it.get("user_answer"),
                            "correct_answer": it.get("correct_answer"),
                            "options": it.get("options"),
                        })
                    display_name_map = {
                        "uid": "UID",
                        "doc": "Document",
                        "type": "Question Type",
                        "difficulty": "Difficulty",
                        "answered": "Answered",
                        "is_correct": "Is Correct",
                        "question": "Question Stem",
                        "user_answer": "Your Answer",
                        "correct_answer": "Reference Answer",
                        "options": "Options"
                    }
                    cols_order = [
                        "uid","doc","type","difficulty","answered","is_correct","question","user_answer","correct_answer","options"
                    ]
                    try:
                        import pandas as pd
                        df_prev = pd.DataFrame(preview)
                        for col in cols_order:
                            if col not in df_prev.columns:
                                df_prev[col] = ""
                        df_prev = df_prev.reindex(columns=cols_order)
                        def _to_str(x):
                            if x is None:
                                return ""
                            if isinstance(x, bool):
                                return "True" if x else "False"
                            return str(x)
                        for c in ["answered","is_correct","question","user_answer","correct_answer","options"]:
                            df_prev[c] = df_prev[c].map(_to_str)
                        df_prev = df_prev.rename(columns={c: display_name_map.get(c, c) for c in df_prev.columns})
                        st.dataframe(df_prev, use_container_width=True)
                    except Exception:
                        pretty_rows = []
                        for row in preview:
                            pretty_rows.append({display_name_map.get(k, k): v for k, v in row.items()})
                        st.table(pretty_rows)
                except Exception:
                    pass
        st.markdown("</div>", unsafe_allow_html=True)


def render_round_summary() -> None:
    """本轮总结页：展示关键指标与错题回顾。

    - 学习用时：本轮时长（若有 round_start_ms/round_end_ms）与会话累计时长
    - 学习行为：本轮学习顺序（UID 顺序）、跳过/回退统计（近似估计：非单调前后移动）
    - 练习与测试：总体正确率、难度分布、错题详情（题干/你的作答/标准答案/解析）
    """
    items: Optional[List[Item]] = st.session_state.get("items")
    if not items:
        st.info(" No items in current round")
        return
    round_uids: List[str] = st.session_state.get("round_uids", []) or [get_item_uid(it) for it in items]
    answers: Dict[str, Any] = st.session_state.get("answers_by_item", {}) or {}

    total = len(round_uids)
    answered_ids = [uid for uid in round_uids if uid in answers]
    answered = len(answered_ids)
    correct = sum(1 for uid in answered_ids if isinstance(answers.get(uid), dict) and answers.get(uid).get("is_correct") is True)
    acc = (correct / answered) if answered else 0.0

    # 局部样式（简洁卡片+分隔）
    st.markdown(
        """
        <style>
        .sumCard{
          padding:14px 18px; border-radius:14px; margin:14px 0;
          background: linear-gradient(180deg, rgba(255,255,255,.02), rgba(255,255,255,0) 12%), rgba(20,22,40,.52);
          box-shadow: inset 0 0 0 1px rgba(0,229,255,.15), 0 12px 32px rgba(0,0,0,.35), 0 0 18px rgba(0,229,255,.06);
        }
        .sectionTitle{font-weight:800;font-size:16px;margin:0 0 10px;color:#E6F7FF}
        .softDivider{height:1px;margin:14px 0;background:linear-gradient(90deg, rgba(0,0,0,0) 0%, rgba(0,229,255,.25) 20%, rgba(0,229,255,.25) 80%, rgba(0,0,0,0) 100%)}
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.subheader(" Round Summary")
    st.markdown("<div class='sumCard'>", unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(" Total", total)
    c2.metric(" Answered", answered)
    c3.metric(" Correct", correct)
    c4.metric(" Accuracy", f"{acc:.0%}")
    st.markdown("</div>", unsafe_allow_html=True)

    # 学习用时：本轮与会话累计
    try:
        r_start = int(st.session_state.get("round_start_ms", 0))
        r_end = int(st.session_state.get("round_end_ms", _now_ms()))
        r_dur = max(0, (r_end - r_start) / 1000)
        s_start = int(st.session_state.get("session_start_ms", r_start))
        s_dur = max(0, (_now_ms() - s_start) / 1000)
        st.caption(f"Round time：{r_dur:.1f}s；Session total：{s_dur:.1f}s")
    except Exception:
        pass

    # 难度分布
    from collections import Counter
    def _dnum(it: Item) -> int:
        return _parse_difficulty_num(it.get("difficulty"))
    d_counter = Counter(_dnum(it) for it in items)
    # 简洁展示：使用徽章标签展示各难度数量
    st.markdown("<div class='sumCard'>", unsafe_allow_html=True)
    st.markdown("<div class='sectionTitle'> Difficulty</div>", unsafe_allow_html=True)
    if d_counter:
        total_cnt = sum(int(v) for v in d_counter.values()) or 1
        color_map = {1:("#e8f5e9","#2e7d32"),2:("#fff8e1","#b26a00"),3:("#ffebee","#c62828"),4:("#e3f2fd","#1565c0"),5:("#ede7f6","#4527a0")}
        levels_html = []
        for k in sorted(d_counter.keys()):
            cnt = int(d_counter.get(k, 0))
            pct = int(round(cnt * 100 / total_cnt))
            bg, fg = color_map.get(int(k), ("#f1f5f9", "#334155"))
            levels_html.append(
                f"<span style='display:inline-block;padding:4px 10px;margin:2px 8px 8px 0;border-radius:999px;background:{bg};color:{fg};font-size:12px;'><strong>L{k}</strong>：{cnt}Questions（{pct}%）</span>"
            )
        st.markdown(" ".join(levels_html), unsafe_allow_html=True)
    else:
        st.caption("(No Data)")
    st.markdown("</div>", unsafe_allow_html=True)

    # 学习路径（UID顺序）与跳过/回退估计（简单示例）
    st.markdown("<div class='sumCard'>", unsafe_allow_html=True)
    st.markdown("<div class='sectionTitle'> Learning Path</div>", unsafe_allow_html=True)
    try:
        path_preview = ", ".join(round_uids[:20]) + (" ..." if len(round_uids) > 20 else "")
        st.write(path_preview)
    except Exception:
        pass
    st.markdown("</div>", unsafe_allow_html=True)

    # 错题列表（更详细：题干、你的作答、参考答案、解析）
    uid_to_item = {get_item_uid(it): it for it in items}
    wrong_uids = [uid for uid in answered_ids if answers.get(uid, {}).get("is_correct") is False]
    if wrong_uids:
        st.markdown("<div class='sumCard'>", unsafe_allow_html=True)
        st.markdown("<div class='sectionTitle'> Wrong Answers</div>", unsafe_allow_html=True)
        for uid in wrong_uids[:100]:  # 显示前100条避免过长
            it = uid_to_item.get(uid)
            if not it:
                continue
            q = it.get("question")
            if isinstance(q, list):
                _qparts = []
                for x in q:
                    if isinstance(x, dict):
                        try:
                            _qparts.append(json.dumps(x, ensure_ascii=False))
                        except Exception:
                            _qparts.append(str(x))
                    else:
                        _qparts.append(str(x))
                q_text = "\n".join(_qparts)
            elif isinstance(q, dict):
                try:
                    q_text = json.dumps(q, ensure_ascii=False)
                except Exception:
                    q_text = str(q)
            else:
                q_text = str(q) if q is not None else ""
            q_preview = (q_text or "").strip().replace("\n", " ")[:120]
            meta_badges = " ".join([
                _badge("type", get_type_display_name(str(it.get("type","-")))),
                _badge("difficulty", str(it.get("difficulty","-"))),
                _badge("doc", str(it.get("doc","-"))),
            ])
            st.markdown(meta_badges, unsafe_allow_html=True)
            with st.expander(f"{q_preview or '[No Question Stem]'}"):
                # 题干全文
                if q_text:
                    st.markdown(f"**Question Stem**\n\n{q_text}")
                # 选项（若有）
                opts = it.get("options") or parse_options_from_question(q_text)
                if opts:
                    st.markdown("**options**")
                    for op in opts:
                        st.write("- "+str(op))
                # 你的作答
                rec = answers.get(uid, {})
                st.markdown("** Your Answer**")
                if "display_choice" in rec:
                    st.write(str(rec.get("display_choice")))
                elif "choice" in rec or "choice_text" in rec:
                    choice = rec.get("choice") or rec.get("choice_text")
                    st.write(str(choice))
                elif "choices" in rec:
                    st.write(", ".join([str(x) for x in (rec.get("choices") or [])]))
                elif "text" in rec:
                    st.write(str(rec.get("text")))
                else:
                    st.write("(Answer details not saved)")
                # 参考答案
                std_ans = flatten_answers(it.get("answer"))
                st.markdown("** Reference**")
                if std_ans:
                    for a in std_ans[:10]:  # 最多展示10条
                        st.write("- "+str(a))
                else:
                    st.write("(No standard answer)")
                # 解析
                analysis = it.get("analysis")
                if analysis:
                    st.markdown("** Analysis**")
                    if isinstance(analysis, list):
                        for a in analysis[:5]:
                            st.write("- "+str(a))
                    else:
                        st.write(str(analysis))
                kps = it.get("knowledge_points")
                if kps:
                    st.markdown("**Knowledge Points:**")
                    if isinstance(kps, list):
                        st.write(", ".join([str(x) for x in kps]))
                    else:
                        st.write(str(kps))
        st.markdown("</div>", unsafe_allow_html=True)

    # 底部操作按钮行
    st.markdown("<div class='sumCard'>", unsafe_allow_html=True)
    c_wrong, c_another, c_review = st.columns([2, 2, 2])
    # 仅错题再来一轮
    if c_wrong.button(" Retry Wrong Only", key="summary_retry_wrong_inline"):
        wrong_items = [uid_to_item[uid] for uid in wrong_uids if uid in uid_to_item]
        if wrong_items:
            st.session_state.items = wrong_items
            st.session_state.item_idx = 0
            st.session_state.correct_count = 0
            st.session_state.answered_count = 0
            st.session_state.ability = 1.0
            st.session_state.answered_items = set()
            st.session_state.answers_by_item = {}
            st.session_state.round_uids = [get_item_uid(it) for it in wrong_items]
            st.session_state.show_summary = False
            st.rerun()
        else:
            st.info(" No wrong items to review")
    # 再做一轮新题（沿用当前筛选条件）
    if c_another.button(" Start Another Round", key="summary_start_another_inline"):
        _start_new_round_from_filters()
    # 复习本轮题目：返回题目界面，从第一题开始
    if c_review.button(" Review This Round", key="summary_review_round_inline"):
        st.session_state.show_summary = False
        st.session_state.item_idx = 0
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)
def _parse_difficulty_num(val: Any) -> int:
    if val is None:
        return 3
    s = str(val).upper()
    if s.startswith("L"):
        try:
            return max(1, min(5, int(s[1:])))
        except Exception:
            return 3
    if s.isdigit():
        try:
            return max(1, min(5, int(s)))
        except Exception:
            return 3
    return 3


def _ensure_qdrant_client() -> Optional["QdrantClient"]:
    if QdrantClient is None or QdrantConfig is None:
        return None
    if "_qdrant_client" in st.session_state:
        return cast(QdrantClient, st.session_state["_qdrant_client"])  # type: ignore
    cfg = QdrantConfig()
    try:
        client = QdrantClient(host=cfg.host, port=cfg.port, prefer_grpc=cfg.prefer_grpc, timeout=cfg.timeout)
        st.session_state["_qdrant_client"] = client
        st.session_state["_qdrant_collection"] = cfg.collection
        return client
    except Exception:
        return None


def _get_neighbors_recommend(source_id: str, top_k: int) -> Optional[Tuple[List[str], List[float]]]:
    client = _ensure_qdrant_client()
    if client is None or qmodels is None:
        return None
    collection = st.session_state.get("_qdrant_collection")
    try:
        flt = qmodels.Filter(must=[qmodels.FieldCondition(key="source_id", match=qmodels.MatchValue(value=source_id))])
        recs, _ = client.scroll(collection_name=collection, scroll_filter=flt, with_payload=False, limit=1)
        if not recs:
            return None
        pid = recs[0].id
        rs = client.recommend(collection_name=collection, positive=[pid], limit=top_k, with_payload=True)
        nn_ids: List[str] = []
        nn_sims: List[float] = []
        for r in rs:
            pl = r.payload or {}
            sid = pl.get("source_id")
            if not sid:
                continue
            nn_ids.append(str(sid))
            nn_sims.append(float(r.score))
        return (nn_ids, nn_sims)
    except Exception:
        return None


def _ensure_selector() -> Optional["Selector"]:
    if Selector is None or Scorer is None or AdaptiveParams is None:
        return None
    if "_selector" in st.session_state:
        return cast(Selector, st.session_state["_selector"])  # type: ignore
    params = AdaptiveParams()
    scorer = Scorer(params)
    selector = Selector(scorer, temp=params.softmax_temperature)
    st.session_state["_selector"] = selector
    st.session_state["_adaptive_params"] = params
    return selector


def item_matches_filters(item: Item, selected_fields: Set[str], selected_types: Set[str], selected_difficulties: Set[str], selected_docs: Set[str]) -> bool:
    def ok(key: str, selected: Set[str]) -> bool:
        if not selected:
            return True
        val = item.get(key)
        return isinstance(val, str) and val in selected

    return (
        ok("field", selected_fields)
        and ok("type", selected_types)
        and ok("difficulty", selected_difficulties)
        and ok("doc", selected_docs)
    )


def load_filtered_items(
    file_path: str,
    selected_fields: Set[str],
    selected_types: Set[str],
    selected_difficulties: Set[str],
    selected_docs: Set[str],
    max_items: int,
) -> List[Item]:
    items: List[Item] = []
    progress = st.progress(0.0)
    total_bytes = os.path.getsize(file_path)
    processed_bytes = 0
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            processed_bytes += len(line.encode("utf-8"))
            try:
                raw_item = json_loads(line)
            except Exception:
                continue
            item = normalize_marketreg_item(raw_item)
            if item_matches_filters(item, selected_fields, selected_types, selected_difficulties, selected_docs):
                items.append(item)
                if len(items) >= max_items:
                    break
            progress.progress(min(0.999, processed_bytes / max(1, total_bytes)))
    progress.progress(1.0)
    return items


def get_item_difficulty_score(item: Item) -> int:
    # Map like L1..L5 -> 1..5. Default medium (3)
    raw = str(item.get("difficulty", "")).strip().upper()
    m = re.match(r"L(\d+)", raw)
    if m:
        try:
            return max(1, min(5, int(m.group(1))))
        except Exception:
            return 3
    # Try digits alone
    if raw.isdigit():
        try:
            return max(1, min(5, int(raw)))
        except Exception:
            return 3
    return 3


def sort_items_for_adaptive(items: List[Item], ability: float) -> List[Item]:
    def key_fn(it: Item) -> Tuple[float, str]:
        d = get_item_difficulty_score(it)
        return (abs(d - ability), str(it.get("id", "")))

    return sorted(items, key=key_fn)


def evaluate_single_choice(selected: Optional[str], correct: str) -> Optional[bool]:
    if selected is None:
        return None
    # If options like "A. ..." are used, compare the leading letter
    m_sel = re.match(r"^([A-Ha-h])[\).。]?", selected.strip())
    sel_letter = m_sel.group(1).upper() if m_sel else selected.strip().upper()
    return sel_letter == correct.strip().upper()


def evaluate_fill_blank(user_text: str, answers: List[str]) -> Optional[bool]:
    if not user_text:
        return None
    norm_user = normalize_text(user_text)
    normalized_answers = [normalize_text(a) for a in answers if a.strip()]
    # Consider correct if any acceptable answer snippet is contained
    for a in normalized_answers:
        if a and a in norm_user:
            return True
    # If nothing matched, mark incorrect for demo
    return False


def evaluate_true_false(selected: Optional[str], correct: str) -> Optional[bool]:
    if selected is None:
        return None
    # Normalize both selected and correct answers
    selected_norm = selected.strip().lower()
    correct_norm = correct.strip().lower()
    
    # Handle various formats of True/False answers
    true_variants = ["true", "正确", "对", "是", "t", "√", "✓"]
    false_variants = ["false", "错误", "错", "否", "f", "×", "✗"]
    
    selected_is_true = selected_norm in true_variants
    selected_is_false = selected_norm in false_variants
    correct_is_true = correct_norm in true_variants
    correct_is_false = correct_norm in false_variants
    
    if selected_is_true and correct_is_true:
        return True
    elif selected_is_false and correct_is_false:
        return True
    elif (selected_is_true and correct_is_false) or (selected_is_false and correct_is_true):
        return False
    else:
        # Fallback: direct string comparison
        return selected_norm == correct_norm


def _letters_from_string(s: str) -> List[str]:
    # Extract distinct option letters A-H from a string like "A", "A,C", "AC", "A; C" etc.
    letters = []
    # First, grab all individual uppercase letters A-H
    for ch in s.upper():
        if "A" <= ch <= "H":
            letters.append(ch)
    # Deduplicate preserving order
    seen = set()
    dedup = []
    for l in letters:
        if l not in seen:
            seen.add(l)
            dedup.append(l)
    return dedup


def evaluate_multiple_choice(selected_options: Optional[List[str]], answers: List[str], options: Optional[List[str]]) -> Optional[bool]:
    if not selected_options:
        return None

    # Case 1: compare by option letters when possible
    # Selected letters from selected option display strings like "A. ..."
    selected_letters: List[str] = []
    for s in selected_options:
        m = re.match(r"^([A-Ha-h])[\).。]?", s.strip())
        if m:
            selected_letters.append(m.group(1).upper())

    correct_letters: List[str] = []
    for a in answers:
        # Try parse list literal like "['A','C']"
        parsed = None
        try:
            import ast

            parsed = ast.literal_eval(a)
        except Exception:
            parsed = None
        if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
            for x in parsed:
                correct_letters.extend(_letters_from_string(x))
        else:
            correct_letters.extend(_letters_from_string(a))

    if selected_letters and correct_letters:
        return set(selected_letters) == set(correct_letters)

    # Case 2: fallback to comparing normalized option texts
    if not options:
        # Cannot grade without options reference
        return None

    def strip_prefix(text: str) -> str:
        # Remove leading "A. ", "B)" etc and normalize
        text = re.sub(r"^([A-Ha-h])[\).。]\s*", "", text).strip()
        return normalize_text(text)

    selected_norm = {strip_prefix(s) for s in selected_options}
    answers_norm = {normalize_text(a) for a in answers if a and isinstance(a, str)}
    # If answers are provided as full option strings
    if answers_norm:
        return selected_norm == answers_norm
    return None


def show_item_view(item: Item) -> Tuple[Optional[bool], Dict[str, Any]]:
    """Render the UI for a single item and return (is_correct, user_inputs)."""
    render_item_badges(item)

    question = item.get("question")
    if isinstance(question, list):
        for idx, q in enumerate(question):
            st.markdown(f"**Q{idx + 1}.** {q}")
    elif isinstance(question, str):
        st.markdown(f"** Question**: {question}")
    else:
        st.info(" No valid question field")

    q_type = str(item.get("type", "")).strip().lower()
    answers = flatten_answers(item.get("answer"))
    user_inputs: Dict[str, Any] = {}
    is_correct: Optional[bool] = None

    # Get unique item UID for component keys and prior answers to lock UI
    item_id = get_item_uid(item)
    answers_by_item: Dict[str, Any] = st.session_state.get("answers_by_item", {}) or {}
    prior = answers_by_item.get(item_id)
    is_locked = prior is not None
    
    if q_type == "single_choice":
        options = item.get("options")
        if not options:
            options = parse_options_from_question(question)
        if options:
            # Set default index based on prior answer to truly lock the widget selection
            default_index = None
            if is_locked and isinstance(prior, dict) and prior.get("choice") in options:
                try:
                    default_index = options.index(prior.get("choice"))
                except Exception:
                    default_index = None
            choice = st.radio(
                " Choose one",
                options,
                index=default_index,
                key=f"sc_radio_{item_id}",
                disabled=is_locked,
            )
            user_inputs["choice"] = choice
            # Correct answer often like 'C'
            correct_letter = answers[0] if answers else ""
            is_correct = evaluate_single_choice(choice, correct_letter)
        else:
            st.warning(" No options detected; cannot auto-grade")
            # If locked, show prior choice_text and disable editing
            text_default = str(prior.get("choice_text", "")) if (is_locked and isinstance(prior, dict)) else ""
            text = st.text_input(
                "Your answer  eg. A/B/C/D",
                key=f"sc_text_{item_id}",
                value=text_default if is_locked else None,
                disabled=is_locked,
            )
            user_inputs["choice_text"] = text
            if text:
                is_correct = evaluate_single_choice(text, answers[0] if answers else "")

    elif q_type == "fill_blank":
        # If locked, prefill and disable
        text_default = str(prior.get("text", "")) if (is_locked and isinstance(prior, dict)) else ""
        text = st.text_area(
            " Your answer",
            height=100,
            key=f"fb_text_{item_id}",
            value=text_default if is_locked else None,
            disabled=is_locked,
        )
        user_inputs["text"] = text
        if answers:
            is_correct = evaluate_fill_blank(text, answers)
        else:
            # 无标准答案时，提供自评选择（持久化）
            if not is_locked:
                eval_choice = st.radio(
                    "Your judgement",
                    ["Not selected", "I got it right", "I got it wrong"],
                    key=f"fb_eval_{item_id}",
                    horizontal=True,
                )
                if eval_choice == "I got it right":
                    is_correct = True
                elif eval_choice == "I got it wrong":
                    is_correct = False
                else:
                    is_correct = None
            else:
                if isinstance(prior, dict) and prior.get("is_correct") is not None:
                    is_correct = bool(prior.get("is_correct"))
                else:
                    is_correct = None

    elif q_type == "multiple_choice":
        # Multiple choice with multiselect
        options = item.get("options")
        if not options:
            options = parse_options_from_question(question) or []
        default_selection = []
        if is_locked and isinstance(prior, dict) and isinstance(prior.get("choices"), list):
            default_selection = [opt for opt in options if opt in prior.get("choices")]
        selected = st.multiselect(
            " Choose multiple",
            options,
            default=default_selection if is_locked else [],
            key=f"mc_multi_{item_id}",
            disabled=is_locked,
        )
        user_inputs["choices"] = selected
        if selected:
            is_correct = evaluate_multiple_choice(selected, answers, options)
        else:
            is_correct = None

    elif q_type == "true_false":
        # True/False question with radio buttons
        tf_options = ["True", "False"]
        # If locked, set default index to previous selection to lock widget value
        default_index = None
        if is_locked and isinstance(prior, dict) and prior.get("display_choice") in tf_options:
            try:
                default_index = tf_options.index(prior.get("display_choice"))
            except Exception:
                default_index = None
        choice = st.radio(
            " Please choose:",
            tf_options,
            index=default_index,
            key=f"tf_radio_{item_id}",
            disabled=is_locked,
        )
        user_inputs["choice"] = choice
        
        if choice and answers:
            # Extract True/False from the choice
            selected_value = "True" if "True" in choice else "False"
            correct_answer = answers[0] if answers else ""
            is_correct = evaluate_true_false(selected_value, correct_answer)
        else:
            is_correct = None

    else:
        # Open-ended types: case_analysis, comprehension, etc.
        text_default = str(prior.get("text", "")) if (is_locked and isinstance(prior, dict)) else ""
        text = st.text_area(
            " Your response",
            height=200,
            key=f"open_text_{item_id}",
            value=text_default if is_locked else None,
            disabled=is_locked,
        )
        user_inputs["text"] = text
        # Self-assessment (持久化为单选，不用瞬时按钮)
        if not is_locked:
            eval_choice = st.radio(
                " Your judgement",
                ["Not selected", "I got it right", "I got it wrong"],
                key=f"open_eval_{item_id}",
                horizontal=True,
            )
            if eval_choice == "I got it right":
                is_correct = True
            elif eval_choice == "I got it wrong":
                is_correct = False
            else:
                is_correct = None
        else:
            if isinstance(prior, dict) and prior.get("is_correct") is not None:
                is_correct = bool(prior.get("is_correct"))

    # 做错或查看已锁定错题时自动展开解析
    expand_analysis = bool(is_correct is False)
    with st.expander(" Show Answer & Analysis", expanded=expand_analysis):
        if answers:
            st.markdown("** Answer(s):**")
            for a in answers:
                st.write("- " + a)
        else:
            st.write("( No standardized answer)")
        analysis = item.get("analysis")
        if analysis:
            st.markdown("** Analysis:**")
            if isinstance(analysis, list):
                for a in analysis:
                    st.write("- " + str(a))
            else:
                st.write(str(analysis))
        kps = item.get("knowledge_points")
        if kps:
            st.markdown("** Knowledge Points:**")
            if isinstance(kps, list):
                st.write(", ".join([str(x) for x in kps]))
            else:
                st.write(str(kps))

    if is_correct is True:
        st.success(" Correct ✅")
    elif is_correct is False:
        st.error(" Incorrect ❌")

    return is_correct, user_inputs


def save_progress(path: str, state: Dict[str, Any]) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        st.warning(f" Failed to save progress: {e}")


def load_progress(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def main() -> None:
    st.set_page_config(page_title="Adaptive Power Knowledge Learning (JSONL QA)", layout="wide")
    # 处理回到首页的 query 参数（支持 HTML 固定按钮跳转）
    try:
        qp = getattr(st, "query_params", None)
        qs_has_home = False
        if qp is not None:
            try:
                qs_has_home = bool(qp.get("home"))
            except Exception:
                qs_has_home = False
        else:
            from urllib.parse import parse_qs, urlparse
            import os as _os
            qs = _os.environ.get("QUERY_STRING", "")
            qs_has_home = "home=" in qs
        if qs_has_home:
            st.session_state["show_records"] = False
            st.session_state["show_summary"] = False
            st.session_state["items"] = None
            st.session_state["item_idx"] = 0
            try:
                st.query_params.clear()  # type: ignore[attr-defined]
            except Exception:
                try:
                    st.experimental_set_query_params()
                except Exception:
                    pass
            st.rerun()
    except Exception:
        pass
    # Inject global neon theme (visual only)
    # 左上角固定“回到首页”按钮（所有页面可见）
    st.markdown(
        """
        <style>
        .back-home-fab{ position: fixed !important; top: 12px; left: 12px; z-index: 2147483647; text-decoration:none; 
          width:44px; height:44px; display:flex; align-items:center; justify-content:center; border-radius:10px;
          background: rgba(0,180,200,.22); box-shadow: inset 0 0 0 1px rgba(0,180,200,.30), 0 4px 10px rgba(0,0,0,.10);
          color:#2d3436; font-size:22px; pointer-events:auto; cursor:pointer; border: 1px solid rgba(0,180,200,.35) }
        .back-home-fab:hover{ background: rgba(0,180,200,.30) }
        </style>
        <a class=\"back-home-fab\" href=\"?home=1\" title=\"Back to Home\">🏠</a>
        """,
        unsafe_allow_html=True,
    )

    try:
        inject_neon_theme()
    except Exception:
        pass
    # 若需要强制展开右侧悬浮功能栏（由顶部 Start Session 触发）
    if st.session_state.get("force_sidebar_open"):
        st.markdown(
            """
            <style>
            /* Force open sidebar just like hover/focus states */
            [data-testid="stSidebar"]{ width:320px !important; min-width:320px !important }
            /* Show full content when forced open - reverse collapsed selectors */
            [data-testid="stSidebar"]::before{ display:none !important }
            [data-testid="stSidebar"]:not(:hover):not(:focus-within):not(:has([role="combobox"][aria-expanded="true"])):not(:has([data-baseweb="popover"] [data-baseweb="menu"])) label,
            [data-testid="stSidebar"]:not(:hover):not(:focus-within):not(:has([role="combobox"][aria-expanded="true"])):not(:has([data-baseweb="popover"] [data-baseweb="menu"])) .stMarkdown,
            [data-testid="stSidebar"]:not(:hover):not(:focus-within):not(:has([role="combobox"][aria-expanded="true"])):not(:has([data-baseweb="popover"] [data-baseweb="menu"])) [data-testid="stTextInputRoot"],
            [data-testid="stSidebar"]:not(:hover):not(:focus-within):not(:has([role="combobox"][aria-expanded="true"])):not(:has([data-baseweb="popover"] [data-baseweb="menu"])) [data-baseweb="select"],
            [data-testid="stSidebar"]:not(:hover):not(:focus-within):not(:has([role="combobox"][aria-expanded="true"])):not(:has([data-baseweb="popover"] [data-baseweb="menu"])) [data-testid="stSlider"]{
              display: block !important; visibility: visible !important; opacity: 1 !important;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )
        # make force-open transient so it collapses after mouse leaves naturally
        try:
            st.session_state["force_sidebar_open"] = False
        except Exception:
            pass
    # 顶部全息仪表盘（仅在做题过程中显示）；初始页显示顶端操作按钮组
    items_list_hdr = st.session_state.get("items")
    has_items_hdr = bool(items_list_hdr and len(items_list_hdr) > 0)
    top_start_btn = top_view_btn = top_load_btn = top_save_btn = False
    # 若已进入答题/历史/总结视图，隐藏首页标题与四大按钮（即便本次渲染早先已输出）
    if has_items_hdr or bool(st.session_state.get("show_records")) or bool(st.session_state.get("show_summary")):
        try:
            st.markdown(
                """
                <style>
                .appTitle, .appSubtitle{ display:none !important }
                .top-cta, .top-cta ~ [data-testid="column"], .top-cta ~ div [data-testid="column"], .top-cta ~ div div[data-testid="column"]{
                  display:none !important;
                }
                </style>
                """,
                unsafe_allow_html=True,
            )
        except Exception:
            pass
    # 首页主标题与副标题：仅在初始首页显示（不在答题界面/历史页/总结页显示）
    if (not has_items_hdr) and (not st.session_state.get("show_records")) and (not st.session_state.get("show_summary")):
        st.markdown("<div class='appTitle'>MarketReg-Based Adaptive Learning System</div>", unsafe_allow_html=True)
        st.markdown(
            """
            <div class='appSubtitle'>
            This system is an adaptive learning platform focused on market regulation knowledge. It efficiently loads and filters data from local JSONL question banks, covering various question types such as single choice, multiple choice, true/false, fill-in-the-blank, and open-ended questions.
            Through difficulty-leveling, de-duplication stability mechanisms, and Qdrant-based neighbor retrieval (when available), the platform dynamically reorders questions: answering a complex question correctly suppresses similar easier ones, while mistakes increase the recurrence of related items. Combined with interval-based review scheduling, the learning curve adapts to each learner’s actual mastery level.
            After each submission, the system automatically locks answers and scores responses in real time. The ability index and accuracy rate at the top update instantly, while the history page presents session summaries and error reviews through cards, trend charts, and tables, helping users continuously refine their study strategy. Learners can also save and load their progress to seamlessly continue their study experience across different devices.
            """,
            unsafe_allow_html=True,
        )
    #            本系统是一套面向市场监管法规知识的自适应学习平台，支持从本地 JSONL 题库高效加载与筛选，覆盖单选、多选、判断、填空与开放题等多种题型。它通过难度分级、去重稳定机制与 Qdrant 邻居检索（如可用）实现动态重排：做对复杂题会抑制相似简单题，做错则增强相似题复现频率；配合基于间隔的复习调度，使学习曲线更贴合个体掌握度。同时，系统在作答后即时锁定并自动判分，顶部能力值与正确率实时刷新，历史页面以卡片、趋势图与表格形式展现轮次总结与错题回顾，便于持续优化备考策略。你还可以保存/加载当前进度，随时在不同设备延续学习体验。</div>

    if has_items_hdr:
        try:
            total_items_hdr = int(len(items_list_hdr))
        except Exception:
            total_items_hdr = 0
        current_idx_hdr = int(st.session_state.get("item_idx", 0)) + (1 if total_items_hdr else 0)
        answered_hdr = int(st.session_state.get("answered_count", 0))
        correct_hdr = int(st.session_state.get("correct_count", 0))
        acc_hdr = (correct_hdr / answered_hdr) if answered_hdr else 0.0
        ability_hdr = float(st.session_state.get("ability", 0.0))
        st.markdown(
            f"""
            <div class='holo-header'>
              <div class='holo-grid'>
                <div class='holo-tile'>
                  <div class='holo-label'>Current</div>
                  <div class='holo-led'>{current_idx_hdr}/{total_items_hdr}</div>
                </div>
                <div class='holo-tile'>
                  <div class='holo-label'>Accuracy</div>
                  <div class='holo-led cyan'>{acc_hdr:.0%}</div>
                </div>
                <div class='holo-tile'>
                  <div class='holo-label'>Ability</div>
                  <div class='holo-led green'>{ability_hdr:.2f}</div>
                </div>
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        # 初始页：顶端四按钮（与右侧悬浮栏相同功能）
        # 首页按钮统一方形样式（仅首页注入，避免影响答题/历史/总结页）
        st.markdown(
            """
            <style>
            .block-container .stButton>button{
              display:flex; align-items:center; justify-content:center; flex-direction:column;
              width:66% !important; aspect-ratio:1 / 1; border-radius:14px; font-size:22px; font-weight:700;
              margin:10px auto; white-space: pre-line; text-align:center; line-height:1.25;
              background: linear-gradient(180deg, rgba(0,180,200,.22), rgba(0,180,200,.08));
              box-shadow: inset 0 0 0 1px rgba(0,180,200,.22), 0 6px 14px rgba(0,0,0,.08);
            }
            .block-container .stButton>button:hover{ transform: translateY(-1px); box-shadow: inset 0 0 0 1px rgba(0,180,200,.28), 0 10px 18px rgba(0,0,0,.12) }
            .block-container .stButton>button:active{ box-shadow: inset 0 0 0 1px rgba(0,180,200,.35), 0 0 18px rgba(0,180,200,.25) }
            </style>
            """,
            unsafe_allow_html=True,
        )
        if (not has_items_hdr) and (not st.session_state.get("show_records")) and (not st.session_state.get("show_summary")):
            st.markdown("<div class='top-cta'></div>", unsafe_allow_html=True)
            # 使用四等分列，按钮用 CSS 设为正方形占满列宽
            cA, cB, cC, cD = st.columns([1,1,1,1])
            with cA:
                top_start_btn = st.button("🚀\nStart\nSession", key="top_start_session")
            with cB:
                top_view_btn = st.button("📊\nView\nRecords", key="top_view_records")
            with cC:
                top_load_btn = st.button("📥\nLoad\nProgress", key="top_load_progress")
            with cD:
                top_save_btn = st.button("💾\nSave\nProgress", key="top_save_progress")
            # 顶部 Start 仅用于展开右侧悬浮栏，不直接开启会话
            if top_start_btn:
                st.session_state["force_sidebar_open"] = True

    # 侧边栏：数据集路径与扫描（悬浮折叠舱样式在 CSS 已处理）
    default_path = os.path.join(os.path.dirname(__file__), "MarketReg_QA.jsonl")
    dataset_path = st.sidebar.text_input("Dataset Path", value=st.session_state.get("dataset_path", default_path))
    st.session_state["dataset_path"] = dataset_path
    scan_btn = st.sidebar.button("Scan Dataset")

    if "unique_values" not in st.session_state:
        st.session_state.unique_values = None
    if "scanned_path" not in st.session_state:
        st.session_state.scanned_path = None

    # 默认自动扫描当前数据集（仅第一次或首次进入时），避免每次手动点击
    if (st.session_state.unique_values is None) and os.path.exists(dataset_path):
        st.info("Auto scanning default dataset ...")
        st.session_state.unique_values = collect_unique_values(dataset_path, show_progress=True)
        st.session_state.scanned_path = dataset_path
        st.success("Auto scan finished")

    if scan_btn and os.path.exists(dataset_path):
        st.info("Scanning unique values ...")
        st.session_state.unique_values = collect_unique_values(dataset_path, show_progress=True)
        st.session_state.scanned_path = dataset_path
        st.success("Scan done")
    elif scan_btn and not os.path.exists(dataset_path):
        st.error("Path not found")

    # 学习会话启动/结束时长跟踪（简单版）：记录本页面打开时刻
    if "session_start_ms" not in st.session_state:
        st.session_state.session_start_ms = _now_ms()

    values = st.session_state.unique_values
    if values:
        st.sidebar.markdown("---")
        st.sidebar.markdown("**Filters**")
        sel_field = st.sidebar.multiselect("Field", sorted(values["field"]))
        # Type 显示名映射
        type_options = sorted(values["type"])
        type_display_options = [get_type_display_name(t) for t in type_options]
        sel_type_display = st.sidebar.multiselect("Type", type_display_options)
        display_to_key = {get_type_display_name(t): t for t in type_options}
        sel_type = [display_to_key[d] for d in sel_type_display]
        sel_diff = st.sidebar.multiselect("Difficulty", sorted(values["difficulty"]))
        sel_doc = st.sidebar.multiselect("Source of regulatory documents", sorted(values["doc"]))
    else:
        sel_field = sel_type = sel_diff = sel_doc = []
    st.sidebar.markdown("---")
    max_items = st.sidebar.slider("Items to load", min_value=5, max_value=200, value=50, step=5)
    st.sidebar.markdown("**Review Frequency**")
    review_mode = st.sidebar.selectbox("Mode", ["Standard", "Light", "Intensive", "Custom"], index=0)
    custom_intervals = None
    if review_mode == "Custom":
        c1, c2 = st.sidebar.columns(2)
        d1 = c1.number_input("Level 1 (Day)", min_value=0.0, value=1.0, step=0.5)
        d2 = c2.number_input("Level 2 (Days)", min_value=0.0, value=3.0, step=0.5)
        d3 = c1.number_input("Level 3 (Days)", min_value=0.0, value=7.0, step=0.5)
        d4 = c2.number_input("Level 4 (Days)", min_value=0.0, value=21.0, step=0.5)
        custom_intervals = (int(d1), int(d2), int(d3), int(d4))
    st.sidebar.caption("Review frequency affects scheduling of due/overdue items. After submission, bucket and next review time are updated.")
    start_btn = st.sidebar.button("Start Session")
    view_records_btn = st.sidebar.button("View Records")
    progress_file = os.path.join(os.path.dirname(dataset_path or "."), "adaptive_progress.json")
    load_prog_btn = st.sidebar.button("Load Progress")
    save_prog_btn = st.sidebar.button("Save Progress")

    # 顶端按钮与侧边栏按钮合并：Start 不与顶部合并（顶部仅展开侧栏）
    start_btn = bool(start_btn)
    view_records_btn = bool(view_records_btn or top_view_btn)
    load_prog_btn = bool(load_prog_btn or top_load_btn)
    save_prog_btn = bool(save_prog_btn or top_save_btn)

    if load_prog_btn:
        prog = load_progress(progress_file)
        if prog:
            # Convert answered_items list back to set
            if "answered_items" in prog and isinstance(prog["answered_items"], list):
                prog["answered_items"] = set(prog["answered_items"])
            st.session_state.update(prog)
            st.success("Progress loaded")
        else:
            st.warning("No progress file")

    # 学习记录入口：不影响当前会话状态，仅打开历史页面
    if view_records_btn:
        st.session_state["show_records"] = True
        st.rerun()

    if start_btn:
        if not os.path.exists(dataset_path):
            st.error("Path not found")
        else:
            # 开启新一轮前，重置轮次持久化与总结标志，确保每轮都会写入历史
            st.session_state.show_summary = False
            st.session_state.round_persisted = False
            # If user didn't choose any filters, default to 'select all' for that category
            if values:
                if not sel_field:
                    sel_field = sorted(values["field"])
                if not sel_type:
                    sel_type = sorted(values["type"])
                if not sel_diff:
                    sel_diff = sorted(values["difficulty"])
                if not sel_doc:
                    sel_doc = sorted(values["doc"])

            selected_fields = set(sel_field)
            selected_types = set(sel_type)
            selected_difficulties = set(sel_diff)
            selected_docs = set(sel_doc)

            # 依据温习强度建立调度器（仅在会话中保存一次）
            intervals_map = {
                "Light": (1, 3, 7, 21),
                "Standard": (1, 3, 7, 21),
                "Intensive": (0, 1, 3, 7),
            }
            if review_mode == "Custom" and custom_intervals:
                intervals = custom_intervals
            else:
                intervals = intervals_map.get(review_mode, (1, 3, 7, 21))
            if Scheduler is not None:
                st.session_state["_scheduler_intervals"] = intervals

            with st.spinner("Loading items ..."):
                items = load_filtered_items(
                    dataset_path,
                    selected_fields,
                    selected_types,
                    selected_difficulties,
                    selected_docs,
                    max_items,
                )

            if not items:
                st.warning("No items matched filters")
            else:
                # Adaptive selection: if Qdrant is available, use selector; else fallback to difficulty sort
                selector = _ensure_selector()
                # 初始能力从低档开始，优先出低难度题
                ability = 1.0
                if selector is not None:
                    # Dedupe items by UID first
                    seen_uids: Set[str] = set()
                    unique_items: List[Item] = []
                    for it in items:
                        uid = get_item_uid(it)
                        if uid in seen_uids:
                            continue
                        seen_uids.add(uid)
                        unique_items.append(it)
                    items = unique_items
                    # Build ItemMeta list for selector (use UID)
                    metas: List[Dict[str, Any]] = []
                    for it in items:
                        metas.append({
                            "id": get_item_uid(it),
                            "field": it.get("field"),
                            "type": it.get("type"),
                            "difficulty_str": it.get("difficulty"),
                            "difficulty_num": _parse_difficulty_num(it.get("difficulty")),
                            "standard": it.get("standard"),
                            "doc": it.get("doc"),
                            "knowledge_points": it.get("knowledge_points", []) if isinstance(it.get("knowledge_points"), list) else [it.get("knowledge_points")] if isinstance(it.get("knowledge_points"), str) else [],
                        })
                    # Seed session state for recent complex tracking
                    st.session_state.setdefault("recent_correct_complex_ids", [])
                    # Use neighbors via Qdrant recommend if available
                    def get_neighbors_fn(src_id: str):
                        params = st.session_state.get("_adaptive_params")
                        if params is None:
                            return None
                        return _get_neighbors_recommend(src_id, cast(Any, params).topk_neighbors)
                    def get_complex_difficulty_fn(src_id: str) -> int:
                        # Look up in current items first
                        for it in items:
                            if str(it.get("id")) == src_id:
                                return _parse_difficulty_num(it.get("difficulty"))
                        return 4
                    # Minimal ItemMeta classless adaptation
                    class _M:  # lightweight adapter
                        def __init__(self, d):
                            self.__dict__.update(d)
                    candidate_metas = [_M(m) for m in metas]
                    # Minimal SessionState adapter
                    class _S:
                        def __init__(self, ability):
                            self.ability = ability
                            self.review_schedule = {}
                            self.kp_mastery = {}
                            # 传递最近答题记录用于错题增强：此处简单传空，上线可接入真实记录
                            self.answers_by_item = {}
                    state_adapter = _S(ability)
                    picks = selector.choose(
                        candidate_metas,
                        state_adapter,
                        cast(List[str], st.session_state.get("recent_correct_complex_ids", [])),
                        get_neighbors_fn,
                        get_complex_difficulty_fn,
                        k=len(items)
                    )
                    # Reorder items by picks sequence id order, ensuring no duplicates
                    id_to_item = {get_item_uid(it): it for it in items}
                    items_sorted: List[Item] = []
                    seen_pick: Set[str] = set()
                    for p in picks:
                        if p.id in seen_pick:
                            continue
                        seen_pick.add(p.id)
                        it = id_to_item.get(p.id)
                        if it is not None:
                            items_sorted.append(it)
                    # Fill missing to keep count stable (append remaining items not picked)
                    if len(items_sorted) < len(items):
                        picked_uids = {get_item_uid(it) for it in items_sorted}
                        for it in items:
                            uid = get_item_uid(it)
                            if uid not in picked_uids:
                                items_sorted.append(it)
                        # ensure no overshoot (shouldn't happen)
                        items_sorted = items_sorted[: len(items)]
                    if not items_sorted:
                        items_sorted = sort_items_for_adaptive(items, ability)
                else:
                    # Fallback with dedupe by UID
                    seen_uids: Set[str] = set()
                    dedup_items: List[Item] = []
                    for it in items:
                        uid = get_item_uid(it)
                        if uid in seen_uids:
                            continue
                        seen_uids.add(uid)
                        dedup_items.append(it)
                    items_sorted = sort_items_for_adaptive(dedup_items, ability)
                st.session_state.items = items_sorted
                st.session_state.item_idx = 0
                st.session_state.correct_count = 0
                st.session_state.answered_count = 0
                st.session_state.ability = ability
                # Reset per-session states to avoid遗留提交影响新会话
                st.session_state.answered_items = set()
                st.session_state.answers_by_item = {}
                st.session_state.recent_correct_complex_ids = []
                # 记录当前轮次的 UID 序列与起止时间，用于统计
                st.session_state.round_uids = [get_item_uid(it) for it in items_sorted]
                st.session_state.round_start_ms = _now_ms()
                st.session_state.dataset_path = dataset_path
                st.session_state.filters = {
                    "field": list(selected_fields),
                    "type": list(selected_types),
                    "difficulty": list(selected_difficulties),
                    "doc": list(selected_docs),
                }
                st.success(f"Loaded {len(items_sorted)} items")
                # Ensure a clean rerun so homepage header/CTA are not rendered in quiz view
                st.rerun()

    if save_prog_btn:
        state = {
            key: st.session_state.get(key)
            for key in [
                "items",
                "item_idx",
                "correct_count",
                "answered_count",
                "ability",
                "answered_items",
                "dataset_path",
                "filters",
            ]
        }
        # Convert set to list for JSON serialization
        if "answered_items" in state and isinstance(state["answered_items"], set):
            state["answered_items"] = list(state["answered_items"])
        save_progress(progress_file, state)
        st.sidebar.success("Progress saved")

    # 学习记录页（持久化为独立视图，避免交互后返回主界面）
    # 若点击开始学习，则强制退出学习记录视图
    if start_btn:
        st.session_state["show_records"] = False

    if st.session_state.get("show_records"):
        render_learning_records()
        # 提供返回学习按钮
        if st.sidebar.button("Back"):
            st.session_state["show_records"] = False
            st.rerun()
        return

    # Main learning UI or Summary
    if st.session_state.get("show_summary"):
        _persist_round_to_disk()
        render_round_summary()
        return

    items: Optional[List[Item]] = st.session_state.get("items")
    if items:
        # Hide home title and four top-square buttons when in quiz view (even if rendered earlier this run)
        try:
            st.markdown(
                """
                <style>
                .appTitle, .appSubtitle{ display:none !important }
                .top-cta, .top-cta ~ [data-testid="column"], .top-cta ~ div [data-testid="column"], .top-cta ~ div div[data-testid="column"]{
                  display:none !important;
                }
                </style>
                """,
                unsafe_allow_html=True,
            )
        except Exception:
            pass
        # Adjust radio group to compact size and horizontal options (override pseudo-slide styles)
        try:
            st.markdown(
                """
                <style>
                .stRadio [role="radiogroup"]{
                  max-height:none !important; overflow:visible !important; scroll-snap-type:none !important;
                  display:flex !important; flex-wrap: wrap !important; gap:12px !important;
                }
                .stRadio label{ min-height: initial !important; scroll-snap-align: unset !important; background: transparent !important; padding: 0 !important; margin: 0 !important }
                .stRadio [role="radio"]{
                  background:#fff !important; border:1px solid #E5E5E5 !important; border-radius:8px !important;
                  min-height:56px !important; display:flex !important; align-items:center !important; padding:10px 12px !important;
                  width: calc(50% - 6px) !important; box-sizing: border-box !important;
                }
                .stRadio [role="radio"][aria-checked="true"]{ border-width:2px !important; border-color:#0052D9 !important; background:#F0F7FF !important }
                </style>
                """,
                unsafe_allow_html=True,
            )
        except Exception:
            pass
        n = len(items)
        idx = int(st.session_state.get("item_idx", 0))
        idx = max(0, min(idx, n - 1))
        st.session_state.item_idx = idx

        col_a, col_b, col_c = st.columns([2, 5, 2])
        with col_a:
            st.metric("Current", f"{idx + 1}/{n}")
        with col_b:
            answered = int(st.session_state.get("answered_count", 0))
            correct = int(st.session_state.get("correct_count", 0))
            acc = (correct / answered) if answered else 0.0
            st.metric("Accuracy", f"{acc:.0%}")
        with col_c:
            st.metric("Ability", f"{st.session_state.get('ability', 3.0):.2f}")

        item = items[idx]
        is_correct, _inputs = show_item_view(item)

        # Navigation and grading controls
        col1, col2, col3 = st.columns(3)
        
        # Track if current question has been answered (must be submitted, not仅输入)
        current_item_id = get_item_uid(item)
        answered_items = st.session_state.get("answered_items", set())
        answers_by_item_dict = st.session_state.get("answers_by_item", {}) or {}

        # Determine whether the user has provided any input for this item (counts as "done")
        q_type_for_gate = str(item.get("type", "")).strip().lower()
        has_input = False
        if q_type_for_gate == "true_false":
            has_input = st.session_state.get(f"tf_radio_{current_item_id}") is not None
        elif q_type_for_gate == "single_choice":
            sc_choice = st.session_state.get(f"sc_radio_{current_item_id}")
            sc_text = st.session_state.get(f"sc_text_{current_item_id}")
            has_input = bool(sc_choice) or (sc_text is not None and str(sc_text).strip() != "")
        elif q_type_for_gate == "multiple_choice":
            mc_choices = st.session_state.get(f"mc_multi_{current_item_id}")
            # mc_choices is typically a list; treat non-empty as having input
            has_input = isinstance(mc_choices, (list, tuple)) and len(mc_choices) > 0
        elif q_type_for_gate == "fill_blank":
            fb_text = st.session_state.get(f"fb_text_{current_item_id}")
            has_input = fb_text is not None and str(fb_text).strip() != ""
        else:
            open_text = st.session_state.get(f"open_text_{current_item_id}")
            has_input = open_text is not None and str(open_text).strip() != ""

        # 仅当已经提交过（记录在 answers_by_item 或 answered_items）才视为已答
        is_current_answered = (current_item_id in answered_items) or (current_item_id in answers_by_item_dict)
        
        # (Debug removed)
        
        with col1:
            prev_clicked = st.button("Previous", key=f"prev_btn_{idx}")
        with col2:
            # Always render Next button; gate on click to show warning only when attempted
            next_clicked = st.button("Next", key=f"next_btn_{idx}")
        with col3:
            final_submit_btn = st.button("Submit & Grade", key=f"final_submit_btn_{idx}")

        # Handle navigation
        if prev_clicked:
            st.session_state.item_idx = max(0, idx - 1)
            st.rerun()

        # Determine lock status in main scope based on persisted answers/answered set
        is_locked_main = (
            (current_item_id in st.session_state.get("answered_items", set()))
            or (current_item_id in answers_by_item_dict)
        )


        if next_clicked:
            # 若未提交但已可判分（is_correct 非 None），则自动提交再进入下一题
            if (not is_current_answered) and (is_correct is not None):
                if "answered_items" not in st.session_state:
                    st.session_state.answered_items = set()
                st.session_state.answered_items.add(current_item_id)
                if "answers_by_item" not in st.session_state:
                    st.session_state.answers_by_item = {}
                record: Dict[str, Any] = {"is_correct": bool(is_correct)}
                if q_type_for_gate == "true_false":
                    record["display_choice"] = st.session_state.get(f"tf_radio_{current_item_id}")
                elif q_type_for_gate == "single_choice":
                    record["choice"] = st.session_state.get(f"sc_radio_{current_item_id}")
                    record["choice_text"] = st.session_state.get(f"sc_text_{current_item_id}")
                elif q_type_for_gate == "fill_blank":
                    record["text"] = st.session_state.get(f"fb_text_{current_item_id}")
                elif q_type_for_gate == "multiple_choice":
                    record["choices"] = st.session_state.get(f"mc_multi_{current_item_id}")
                else:
                    record["text"] = st.session_state.get(f"open_text_{current_item_id}")
                st.session_state.answers_by_item[current_item_id] = record

                # 更新统计（并更新复习调度：正确升桶、错误降桶）
                st.session_state.answered_count = int(st.session_state.get("answered_count", 0)) + 1
                if is_correct:
                    st.session_state.correct_count = int(st.session_state.get("correct_count", 0)) + 1
                    st.session_state.ability = float(st.session_state.get("ability", 3.0)) + 0.15
                    try:
                        if _parse_difficulty_num(item.get("difficulty")) >= 3:
                            arr = cast(List[str], st.session_state.get("recent_correct_complex_ids", []))
                            arr = ([str(item.get("id"))] + [x for x in arr if x != str(item.get("id"))])[:10]
                            st.session_state["recent_correct_complex_ids"] = arr
                    except Exception:
                        pass
                else:
                    st.session_state.ability = float(st.session_state.get("ability", 3.0)) - 0.15

                # 复习调度：根据温习强度更新该题的 next_ts
                try:
                    intervals = st.session_state.get("_scheduler_intervals", (1, 3, 7, 21))
                    if Scheduler is not None:
                        from adaptive.scheduler import ReviewEntry
                        sched = Scheduler(intervals)
                        # 取旧条目
                        rs = st.session_state.get("review_schedule", {}) or {}
                        old = rs.get(current_item_id)
                        if old:
                            old_entry = ReviewEntry(bucket=int(old.get("bucket", 0)), next_ts_ms=int(old.get("next_ts_ms", 0)))
                        else:
                            old_entry = None
                        new_entry = sched.on_result(old_entry, bool(is_correct))
                        rs[current_item_id] = {"bucket": new_entry.bucket, "next_ts_ms": new_entry.next_ts_ms}
                        st.session_state["review_schedule"] = rs
                except Exception:
                    pass

                # Clamp ability
                st.session_state.ability = max(1.0, min(5.0, float(st.session_state.ability)))

                # 重排剩余题（在打分中对已到期/逾期的题会有更高权重）
                if idx + 1 < n:
                    remaining = items[idx + 1 :]
                    selector = _ensure_selector()
                    if selector is not None:
                        # Dedupe remaining by UID
                        seen_uids_r: Set[str] = set()
                        uniq_remaining: List[Item] = []
                        for it in remaining:
                            uid = get_item_uid(it)
                            if uid in seen_uids_r:
                                continue
                            seen_uids_r.add(uid)
                            uniq_remaining.append(it)
                        metas = []
                        for it in uniq_remaining:
                            metas.append({
                                "id": get_item_uid(it),
                                "field": it.get("field"),
                                "type": it.get("type"),
                                "difficulty_str": it.get("difficulty"),
                                "difficulty_num": _parse_difficulty_num(it.get("difficulty")),
                                "standard": it.get("standard"),
                                "doc": it.get("doc"),
                                "knowledge_points": it.get("knowledge_points", []) if isinstance(it.get("knowledge_points"), list) else [it.get("knowledge_points")] if isinstance(it.get("knowledge_points"), str) else [],
                            })
                        class _M:
                            def __init__(self, d):
                                self.__dict__.update(d)
                        class _S:
                            def __init__(self, ability):
                                self.ability = ability
                                self.review_schedule = {}
                                self.kp_mastery = {}
                        state_adapter = _S(st.session_state.ability)
                        def get_neighbors_fn(src_id: str):
                            params = st.session_state.get("_adaptive_params")
                            if params is None:
                                return None
                            return _get_neighbors_recommend(src_id, cast(Any, params).topk_neighbors)
                        def get_complex_difficulty_fn(src_id: str) -> int:
                            for it in remaining:
                                if get_item_uid(it) == src_id:
                                    return _parse_difficulty_num(it.get("difficulty"))
                            return 3
                        picks = selector.choose(
                            [_M(m) for m in metas],
                            state_adapter,
                            cast(List[str], st.session_state.get("recent_correct_complex_ids", [])),
                            get_neighbors_fn,
                            get_complex_difficulty_fn,
                            k=len(uniq_remaining)
                        )
                        id_to_item = {get_item_uid(it): it for it in uniq_remaining}
                        resorted: List[Item] = []
                        seen_pick_r: Set[str] = set()
                        for p in picks:
                            if p.id in seen_pick_r:
                                continue
                            seen_pick_r.add(p.id)
                            it = id_to_item.get(p.id)
                            if it is not None:
                                resorted.append(it)
                        # Fill missing to keep remaining length stable
                        if len(resorted) < len(uniq_remaining):
                            picked_uids_r = {get_item_uid(it) for it in resorted}
                            for it in uniq_remaining:
                                uid = get_item_uid(it)
                                if uid not in picked_uids_r:
                                    resorted.append(it)
                            resorted = resorted[: len(uniq_remaining)]
                        if not resorted:
                            resorted = sort_items_for_adaptive(uniq_remaining, st.session_state.ability)
                    else:
                        resorted = sort_items_for_adaptive(remaining, st.session_state.ability)
                    st.session_state.items = items[: idx + 1] + resorted

                # 不自动跳到总结页；如非最后一题则前进一题
                if idx + 1 < n:
                    st.session_state.item_idx = min(n - 1, idx + 1)
                # 刷新以更新 UI
                st.rerun()

            # 已经提交过，直接进入下一题
            elif is_current_answered:
                if idx + 1 < n:
                    st.session_state.item_idx = min(n - 1, idx + 1)
                st.rerun()
            else:
                st.warning(" Please answer the current question before proceeding to the next one.")

        # 整轮提交：必须全部题目提交后才进入总结
        if final_submit_btn:
            answers_by_item_dict = st.session_state.get("answers_by_item", {}) or {}
            total_items = len(items)
            answered_num = len(answers_by_item_dict)
            if answered_num < total_items:
                st.warning(" Please finish all questions before final submission.")
            else:
                st.session_state.round_end_ms = _now_ms()
                st.session_state.show_summary = True
                st.rerun()

    else:
        st.info(" Enter dataset path at right, scan, then start session. Click the 'Start Session' button again.")


if __name__ == "__main__":
    main()
