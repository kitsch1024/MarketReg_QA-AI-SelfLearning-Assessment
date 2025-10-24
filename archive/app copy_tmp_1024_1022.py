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
import random
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
    """Minimal, elegant badge design."""
    return (
        "<span style=\"display:inline-flex;align-items:center;padding:4px 10px;margin:0 6px 6px 0;"
        "border-radius:6px;background:#f1f3f4;color:#5f6368;font-size:12px;font-weight:500;\">"
        f"<span style='opacity:0.7;margin-right:4px;'>{label}</span>"
        f"<span style='color:#202124;font-weight:600;'>{value}</span>"
        "</span>"
    )


def inject_neon_theme() -> None:
    """Inject global dark-gradient + neon glow theme. Purely visual; no logic change."""
    st.markdown(
        """
        <style>
        :root{
          --bg-primary:#f8f9fa;
          --primary:#00b4c8; --warning:#ffa726; --ok:#4caf50; --error:#ef5350; --text:#2d3436;
          --panel: rgba(255,255,255,.85);
          /* Unified spacing system - 8px based */
          --spacing-xs: 4px;
          --spacing-sm: 8px;
          --spacing-md: 16px;
          --spacing-lg: 24px;
          --spacing-xl: 32px;
          --spacing-2xl: 48px;
        }
        html, body, .stApp{
          background: var(--bg-primary) !important;
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
        /* Sidebar - Modern collapsible design */
        /* Move sidebar to the right */
        [data-testid="stAppViewContainer"]{ flex-direction: row-reverse !important }
        [data-testid="stSidebar"] {
          background: white !important;
          box-shadow: -2px 0 8px rgba(0,0,0,0.08);
          border-left: 1px solid #e8eaed;
          width:64px !important; 
          min-width:64px !important; 
          transition: all .3s cubic-bezier(0.4, 0, 0.2, 1); 
          overflow:hidden;
        }
        /* Keep expanded while interacting: hover, focus within, combobox open, or any BaseWeb popover menu */
        [data-testid="stSidebar"]:hover,
        [data-testid="stSidebar"]:focus-within,
        [data-testid="stSidebar"]:has([role="combobox"][aria-expanded="true"]),
        body:has([data-baseweb="popover"] [data-baseweb="menu"]) [data-testid="stSidebar"]{
          width:300px !important; min-width:300px !important;
          box-shadow: -4px 0 16px rgba(0,0,0,0.12);
        }
        /* Collapsed state: show elegant icon with animation */
        [data-testid="stSidebar"]::before{ 
          content:"☰ Filters"; 
          display:block; 
          color: var(--primary); 
          font-size:16px; 
          font-weight: 600;
          padding:12px 10px; 
          opacity:.9;
          text-align: center;
          animation: pulse 2s ease-in-out infinite;
        }
        @keyframes pulse {
          0%, 100% { opacity: 0.9; }
          50% { opacity: 0.5; }
        }
        [data-testid="stSidebar"]:hover::before,
        [data-testid="stSidebar"]:focus-within::before {
          animation: none;
          opacity: 1;
        }
        /* Hide content when collapsed */
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
        
        /* Beautiful Sidebar Typography */
        /* Section headers (Filters, Items to Load, Review Frequency) */
        [data-testid="stSidebar"] .stMarkdown strong,
        [data-testid="stSidebar"] .stMarkdown b {
          color: #202124 !important;
          font-size: 13px !important;
          font-weight: 700 !important;
          text-transform: uppercase !important;
          letter-spacing: 1px !important;
          display: block !important;
          margin-bottom: 12px !important;
          padding-bottom: 8px !important;
          border-bottom: 2px solid var(--primary) !important;
        }
        
        /* Labels for inputs */
        [data-testid="stSidebar"] label {
          color: #5f6368 !important;
          font-size: 13px !important;
          font-weight: 600 !important;
          letter-spacing: 0.3px !important;
          margin-bottom: 8px !important;
        }
        
        /* Multiselect placeholder and input text */
        [data-testid="stSidebar"] .stMultiSelect label,
        [data-testid="stSidebar"] .stSelectbox label {
          color: #202124 !important;
          font-size: 14px !important;
          font-weight: 600 !important;
        }
        
        /* Selected items in multiselect */
        [data-testid="stSidebar"] [data-baseweb="tag"] {
          font-size: 12px !important;
          font-weight: 500 !important;
        }
        
        /* Slider label */
        [data-testid="stSidebar"] .stSlider label {
          font-size: 13px !important;
          font-weight: 600 !important;
          color: #202124 !important;
        }
        
        /* Default text color for other elements */
        [data-testid="stSidebar"] * { 
          color: var(--text) !important; 
        }

        /* Headings */
        h1,h2,h3,h4,h5,h6{ color: var(--text) !important; letter-spacing:.2px }

        /* Unified Button System */
        .stButton>button{
          border-radius: 8px;
          padding: 10px 20px;
          font-size: 14px;
          font-weight: 500;
          transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
          border: none;
          cursor: pointer;
        }
        
        /* Secondary Button (default) */
        .stButton>button:not([kind="primary"]){
          background: white;
          color: #202124;
          border: 1.5px solid #dadce0;
          box-shadow: 0 1px 2px rgba(0,0,0,0.05);
        }
        .stButton>button:not([kind="primary"]):hover{
          border-color: var(--primary);
          background: #f8f9fa;
          box-shadow: 0 2px 4px rgba(0,0,0,0.08);
          transform: translateY(-1px);
        }
        
        /* Primary Button */
        .stButton>button[kind="primary"],
        .beam-bar .stButton>button{ 
          background: linear-gradient(135deg, var(--primary) 0%, #0099b3 100%);
          color: white;
          box-shadow: 0 2px 4px rgba(0,180,200,0.3);
          border: none;
        }
        .stButton>button[kind="primary"]:hover,
        .beam-bar .stButton>button:hover{
          background: linear-gradient(135deg, #00c4d8 0%, #00a9c3 100%);
          box-shadow: 0 4px 12px rgba(0,180,200,0.4);
          transform: translateY(-2px);
        }
        
        /* Focus state for accessibility */
        .stButton>button:focus{
          outline: none;
          box-shadow: 0 0 0 3px rgba(0,180,200,0.2);
        }
        
        /* Active state */
        .stButton>button:active{
          transform: translateY(0);
        }
        
        /* Beam-style bottom action bar */
        .beam-bar{ 
          position: sticky; 
          bottom: 8px; 
          z-index: 9; 
          padding: var(--spacing-sm) 0; 
          margin-top: var(--spacing-sm);
        }

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
        /* Beautiful modern slider styling */
        .stSlider {
            padding: 8px 0 !important;
        }
        /* Slider track background - elegant light gray */
        .stSlider>div>div>div>div{ 
            background: #e8eaed !important;
            height: 4px !important;
            border-radius: 2px !important;
        }
        /* Slider filled track - beautiful gradient */
        .stSlider [data-baseweb="slider"]>div>div{ 
            background: linear-gradient(90deg, #00b4c8 0%, #00d4e8 100%) !important;
            height: 4px !important;
            border-radius: 2px !important;
            box-shadow: 0 1px 3px rgba(0,180,200,0.3) !important;
        }
        /* Slider thumb - modern circular handle */
        .stSlider [data-baseweb="slider"] [role="slider"] {
            width: 20px !important;
            height: 20px !important;
            background: white !important;
            border: 3px solid #00b4c8 !important;
            border-radius: 50% !important;
            box-shadow: 0 2px 6px rgba(0,180,200,0.4) !important;
            transition: all 0.2s ease !important;
        }
        .stSlider [data-baseweb="slider"] [role="slider"]:hover {
            transform: scale(1.15) !important;
            box-shadow: 0 3px 8px rgba(0,180,200,0.5) !important;
        }
        .stSlider [data-baseweb="slider"] [role="slider"]:active {
            transform: scale(1.1) !important;
            box-shadow: 0 2px 6px rgba(0,180,200,0.6) !important;
        }
        /* Min/Max value labels - clean typography */
        .stSlider [data-testid="stTickBarMin"],
        .stSlider [data-testid="stTickBarMax"] {
            background: transparent !important;
            color: #5f6368 !important;
            font-size: 12px !important;
            font-weight: 500 !important;
            padding: 4px 0 !important;
        }
        /* Remove all backgrounds from tick labels */
        .stSlider [data-testid="stTickBar"] *,
        .stSlider [data-testid="stTickBarMin"] *,
        .stSlider [data-testid="stTickBarMax"] * {
            background: transparent !important;
            background-color: transparent !important;
        }
        .stSlider [data-testid="stTickBar"],
        .stSlider [data-testid="stTickBarMin"],
        .stSlider [data-testid="stTickBarMax"] {
            background: transparent !important;
            background-color: transparent !important;
        }
        /* Current value display (if visible) */
        .stSlider > div > div > div:last-child {
            color: #00b4c8 !important;
            font-weight: 600 !important;
            font-size: 14px !important;
        }

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

        /* Modern Expander Styling */
        details>summary {
          background: white;
          border: 1.5px solid #e8eaed;
          border-radius: 8px;
          padding: 12px 16px;
          cursor: pointer;
          transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
          font-weight: 500;
          color: #202124;
          user-select: none;
        }
        details>summary:hover {
          border-color: var(--primary);
          background: #f8f9fa;
          box-shadow: 0 2px 4px rgba(0,0,0,0.08);
        }
        details[open]>summary {
          border-color: var(--primary);
          background: #e6f4f7;
          margin-bottom: var(--spacing-md);
          box-shadow: 0 2px 4px rgba(0,180,200,0.15);
        }
        details[open] {
          border: 1.5px solid #e8eaed;
          border-radius: 8px;
          padding: var(--spacing-sm);
          background: white;
          box-shadow: 0 1px 3px rgba(0,0,0,0.08);
        }
        /* Smooth expand animation */
        details[open] > *:not(summary) {
          animation: expandContent 0.3s ease;
        }
        @keyframes expandContent {
          from { opacity: 0; transform: translateY(-10px); }
          to { opacity: 1; transform: translateY(0); }
        }

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

        /* Top CTA four-square full row - Beautiful Modern Design */
        .top-cta ~ [data-testid="column"],
        .top-cta ~ div [data-testid="column"],
        .top-cta ~ div div[data-testid="column"]{ padding: 0 10px }
        .top-cta ~ [data-testid="column"] .stButton>button,
        .top-cta ~ div [data-testid="column"] .stButton>button,
        .top-cta ~ div div[data-testid="column"] .stButton>button{
          display:flex; 
          align-items:center; 
          justify-content:center; 
          flex-direction:column;
          gap: 8px;
          width:100% !important; 
          aspect-ratio:1 / 1; 
          border-radius:16px; 
          font-size:20px;
          font-weight: 600;
          letter-spacing: 0.5px;
          line-height: 1.3;
          white-space: pre-line; 
          text-align:center;
          background: linear-gradient(135deg, rgba(0,180,200,.18) 0%, rgba(0,180,200,.08) 100%);
          box-shadow: 0 2px 8px rgba(0,0,0,0.1), inset 0 0 0 1px rgba(0,180,200,.25);
          transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
          color: #202124;
        }
        /* Emoji styling in top buttons */
        .top-cta ~ [data-testid="column"] .stButton>button::first-line,
        .top-cta ~ div [data-testid="column"] .stButton>button::first-line,
        .top-cta ~ div div[data-testid="column"] .stButton>button::first-line {
          font-size: 32px;
          line-height: 1;
        }
        .top-cta ~ [data-testid="column"] .stButton>button:hover,
        .top-cta ~ div [data-testid="column"] .stButton>button:hover,
        .top-cta ~ div div[data-testid="column"] .stButton>button:hover{ 
          transform: translateY(-3px) scale(1.02); 
          box-shadow: 0 6px 16px rgba(0,180,200,0.25), inset 0 0 0 1px rgba(0,180,200,.35);
          background: linear-gradient(135deg, rgba(0,180,200,.25) 0%, rgba(0,180,200,.12) 100%);
        }
        .top-cta ~ [data-testid="column"] .stButton>button:active,
        .top-cta ~ div [data-testid="column"] .stButton>button:active,
        .top-cta ~ div div[data-testid="column"] .stButton>button:active{ 
          transform: translateY(-1px) scale(0.98);
          box-shadow: 0 3px 10px rgba(0,180,200,0.3), inset 0 0 0 2px rgba(0,180,200,.4);
        }
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
                # 🆕 新算法字段
                self.ability_variance = st.session_state.get("ability_variance", 1.0)
                self.q_values = {}
                self.item_selection_counts = st.session_state.get("item_selection_counts", {})
                self.total_selections = st.session_state.get("total_selections", 0)
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
    """现代化的学习记录页面：美观的统计展示与趋势分析。"""
    # Modern, elegant styling
    st.markdown("""
        <style>
        /* Learning Records Page Styling */
        .records-header {
            background: linear-gradient(135deg, #00b4c8 0%, #0099b3 100%);
            border-radius: 16px;
            padding: 28px 32px;
            margin-bottom: 24px;
            box-shadow: 0 4px 16px rgba(0,180,200,0.25);
            color: white;
        }
        .records-title {
            font-size: 26px;
            font-weight: 700;
            margin-bottom: 6px;
            color: white;
        }
        .records-subtitle {
            font-size: 13px;
            color: rgba(255,255,255,0.8);
        }
        .kpi-card {
            background: white;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.12);
            text-align: center;
        }
        .kpi-label {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: #5f6368;
            font-weight: 600;
            margin-bottom: 8px;
        }
        .kpi-value {
            font-size: 32px;
            font-weight: 700;
            color: #00b4c8;
            font-family: 'SF Mono', monospace;
            margin-bottom: 4px;
        }
        .kpi-subtext {
            font-size: 12px;
            color: #5f6368;
        }
        .record-card {
            background: white;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 12px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.12);
            border-left: 4px solid #00b4c8;
        }
        .record-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }
        .record-time {
            font-size: 15px;
            font-weight: 600;
            color: #202124;
        }
        .record-stats {
            display: flex;
            gap: 16px;
            font-size: 13px;
            color: #5f6368;
        }
        .record-progress {
            background: #e8eaed;
            border-radius: 8px;
            height: 10px;
            overflow: hidden;
            margin-bottom: 12px;
        }
        .record-progress-bar {
            height: 100%;
            background: linear-gradient(90deg, #00b4c8, #34a853);
            transition: width 0.3s ease;
        }
        .chart-container {
            background: white;
            border-radius: 12px;
            padding: 16px 20px 12px 20px;
            margin-top: 8px;
            margin-bottom: 20px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.12);
        }
        .chart-title {
            font-size: 17px;
            font-weight: 600;
            color: #202124;
            margin-bottom: 8px;
        }
        </style>
    """, unsafe_allow_html=True)

    # Beautiful header
    st.markdown("""
        <div class="records-header">
            <div class="records-title">📊 Learning Records</div>
            <div class="records-subtitle">Track your progress and performance over time</div>
        </div>
    """, unsafe_allow_html=True)

    # Filter selector (compact)
    st.markdown('<div style="margin-bottom: 16px;">', unsafe_allow_html=True)
    col_sel, _ = st.columns([1, 4])
    with col_sel:
        show_opts_labels = ["5", "10", "20", "50", "100", "All"]
        default_label = st.session_state.get("history_limit_label", "10")
        try:
            default_index = show_opts_labels.index(default_label)
        except Exception:
            default_index = 1
        sel_label = st.selectbox("📅 Show last", show_opts_labels, index=default_index, key="history_limit_select")
        st.session_state["history_limit_label"] = sel_label
        if sel_label == "All":
            limit = 1000
        else:
            try:
                limit = int(sel_label)
            except Exception:
                limit = 10
    st.markdown('</div>', unsafe_allow_html=True)

    data = _load_recent_rounds(limit=limit)
    if not data:
        st.info("📭 No history yet. Start your first round to see statistics!")
        return
    
    # Sort by time (recent first)
    try:
        data_sorted = sorted(data, key=lambda r: int(r.get("ts_ms", 0)), reverse=True)
    except Exception:
        data_sorted = data
    
    # Beautiful KPI cards (no extra spacing)
    try:
        avg_acc = sum([float(r.get("accuracy", 0.0)) for r in data_sorted]) / max(1, len(data_sorted))
        avg_answered = sum([int(r.get("answered", 0)) for r in data_sorted]) / max(1, len(data_sorted))
        total_questions = sum([int(r.get("answered", 0)) for r in data_sorted])
        last_ts = int(data_sorted[0].get("ts_ms", 0)) if data_sorted else 0
        last_when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last_ts/1000)) if last_ts else "--"
        
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.markdown(f"""
                <div class="kpi-card">
                    <div class="kpi-label">Total Rounds</div>
                    <div class="kpi-value">{len(data)}</div>
                    <div class="kpi-subtext">completed</div>
                </div>
            """, unsafe_allow_html=True)
        with c2:
            st.markdown(f"""
                <div class="kpi-card">
                    <div class="kpi-label">Avg. Accuracy</div>
                    <div class="kpi-value">{avg_acc:.0%}</div>
                    <div class="kpi-subtext">per round</div>
                </div>
            """, unsafe_allow_html=True)
        with c3:
            st.markdown(f"""
                <div class="kpi-card">
                    <div class="kpi-label">Avg. Completed</div>
                    <div class="kpi-value">{avg_answered:.1f}</div>
                    <div class="kpi-subtext">questions/round</div>
                </div>
            """, unsafe_allow_html=True)
        with c4:
            st.markdown(f"""
                <div class="kpi-card">
                    <div class="kpi-label">Total Questions</div>
                    <div class="kpi-value">{total_questions}</div>
                    <div class="kpi-subtext">answered</div>
                </div>
            """, unsafe_allow_html=True)
    except Exception:
        pass

    # Beautiful combined chart with dual axes
    import pandas as pd
    try:
        rows = []
        # Reverse sort to show oldest to newest in chart (left to right)
        for r in reversed(data_sorted):
            ts = int(r.get("ts_ms", 0))
            rows.append({
                "time": time.strftime("%m-%d %H:%M", time.localtime(ts / 1000)) if ts else "--",
                "accuracy_pct": float(r.get("accuracy", 0.0)) * 100.0,
                "answered": int(r.get("answered", 0)),
            })
        df = pd.DataFrame(rows)
        
        st.markdown('<div class="chart-container">', unsafe_allow_html=True)
        st.markdown('<div class="chart-title">📈 Performance Trends</div>', unsafe_allow_html=True)
        
        try:
            import altair as alt
            
            # Elegant color scheme
            bar_color = '#FFA726'  # Warm orange
            line_color = '#00B4C8'  # Teal
            text_color = '#5f6368'
            grid_color = '#e8eaed'
            
            # Calculate smart Y axis ranges
            max_accuracy = df['accuracy_pct'].max() if not df.empty else 100
            min_accuracy = df['accuracy_pct'].min() if not df.empty else 0
            max_answered = df['answered'].max() if not df.empty else 10
            
            # Ultra-compact scaling for minimal top whitespace
            accuracy_range = max_accuracy - min_accuracy if max_accuracy > min_accuracy else 10
            # Very minimal padding - just enough to not clip the points
            accuracy_padding = max(2, accuracy_range * 0.05)  # 2% min or 5% of range
            
            answered_padding = max(0.3, max_answered * 0.05) if max_answered > 0 else 0.5  # Minimal padding
            
            # Base chart with clean styling
            base = alt.Chart(df).encode(
                x=alt.X('time:N', 
                       axis=alt.Axis(
                           title='',
                           labelAngle=-45,
                           labelColor=text_color,
                           labelFontSize=11,
                           labelPadding=8,
                           tickSize=0,
                           domainWidth=1,
                           domainColor=grid_color
                       ))
            )
            
            # Beautiful bar chart - Questions Completed
            bar = base.mark_bar(
                opacity=0.85,
                color=bar_color,
                cornerRadiusTopLeft=6,
                cornerRadiusTopRight=6,
                width={'band': 0.6}
            ).encode(
                y=alt.Y('answered:Q',
                       title='Questions',
                       scale=alt.Scale(
                           domain=[0, max_answered + answered_padding],
                           nice=False,  # Disable auto-rounding for tighter fit
                           clamp=True
                       ),
                       axis=alt.Axis(
                           labelColor=text_color,
                           titleColor=bar_color,
                           titleFontWeight=600,
                           titleFontSize=13,
                           labelFontSize=11,
                           grid=True,
                           gridColor=grid_color,
                           gridOpacity=0.5,
                           tickSize=0,
                           domainWidth=0
                       )),
                tooltip=[
                    alt.Tooltip('time:N', title='Date'),
                    alt.Tooltip('answered:Q', title='Questions')
                ]
            )
            
            # Elegant line chart - Accuracy
            line = base.mark_line(
                point={
                    'filled': True,
                    'size': 80,
                    'fill': line_color,
                    'stroke': 'white',
                    'strokeWidth': 2
                },
                color=line_color,
                strokeWidth=3,
                interpolate='monotone'
            ).encode(
                y=alt.Y('accuracy_pct:Q',
                       title='Accuracy %',
                       scale=alt.Scale(
                           domain=[max(0, min_accuracy - accuracy_padding), 
                                   min(100, max_accuracy + accuracy_padding)],
                           nice=False,  # Disable auto-rounding for tighter fit
                           clamp=True
                       ),
                       axis=alt.Axis(
                           orient='right',
                           labelColor=text_color,
                           titleColor=line_color,
                           titleFontWeight=600,
                           titleFontSize=13,
                           labelFontSize=11,
                           grid=False,
                           tickSize=0,
                           domainWidth=0
                       )),
                tooltip=[
                    alt.Tooltip('time:N', title='Date'),
                    alt.Tooltip('accuracy_pct:Q', title='Accuracy', format='.1f')
                ]
            )
            
            # Combine with professional styling - ultra-compact
            chart = alt.layer(bar, line).resolve_scale(
                y='independent'
            ).properties(
                height=350,
                padding=0  # No internal padding for maximum space usage
            ).configure_view(
                strokeWidth=0,
                fill='#ffffff',
                continuousHeight=350,
                continuousWidth=800
            ).configure_legend(
                labelFontSize=12,
                titleFontSize=13,
                padding=10
            ).configure_axis(
                labelLimit=200
            )
            
            st.altair_chart(chart, use_container_width=True)
            
        except Exception as e:
            # Fallback to simpler visualization
            st.line_chart(df.set_index('time')[['accuracy_pct', 'answered']])
        
        st.markdown('</div>', unsafe_allow_html=True)
    except Exception:
        st.warning("⚠️ Failed to render trends chart")
    
    # Recent rounds section
    st.markdown('<div style="margin-top: 24px; font-size: 18px; font-weight: 600; color: #202124; margin-bottom: 16px;">📋 Recent Rounds</div>', unsafe_allow_html=True)
    
    for r in data_sorted:
        ts = int(r.get("ts_ms", 0))
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts / 1000)) if ts else "--"
        answered = int(r.get('answered',0))
        total = int(r.get('total',0))
        correct = int(r.get('correct',0))
        acc = float(r.get('accuracy',0.0))
        dur_s = int(r.get('duration_ms',0))/1000
        ratio = (answered / total) if total else 0.0
        
        # Modern record card
        st.markdown('<div class="record-card">', unsafe_allow_html=True)
        
        # Header with time and key stats
        st.markdown(f"""
            <div class="record-header">
                <div class="record-time">🕐 {when}</div>
                <div class="record-stats">
                    <span>✅ {correct}/{answered}</span>
                    <span>📊 {acc:.0%}</span>
                    <span>⏱️ {dur_s/60:.1f}min</span>
                </div>
            </div>
        """, unsafe_allow_html=True)
        
        # Progress bar
        st.markdown(f"""
            <div class="record-progress">
                <div class="record-progress-bar" style="width:{ratio*100:.0f}%;"></div>
            </div>
            <div style="font-size: 12px; color: #5f6368; margin-bottom: 12px;">
                Completed: {answered}/{total} questions ({ratio*100:.0f}%)
            </div>
        """, unsafe_allow_html=True)
        # Filters snapshot with badges
        filt = r.get("filters", {}) or {}
        if isinstance(filt, dict):
            try:
                fields = filt.get("field") or []
                types = filt.get("type") or []
                diffs = filt.get("difficulty") or []
                stds = filt.get("doc") or []
                types_cn = [get_type_display_name(t) for t in types]
                
                # Display badges
                badges = []
                if fields:
                    badges.append(_badge("Field", ", ".join(fields)))
                if types_cn:
                    badges.append(_badge("Type", ", ".join(types_cn)))
                if diffs:
                    badges.append(_badge("Difficulty", ", ".join(diffs)))
                
                if badges:
                    st.markdown(" ".join(badges), unsafe_allow_html=True)
            except Exception:
                pass

        # Item details (expandable)
        with st.expander("📝 View Details", expanded=False):
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
    """现代化的本轮总结页：美观的统计展示与错题回顾。"""
    items: Optional[List[Item]] = st.session_state.get("items")
    if not items:
        st.info("No items in current round")
        return
    round_uids: List[str] = st.session_state.get("round_uids", []) or [get_item_uid(it) for it in items]
    answers: Dict[str, Any] = st.session_state.get("answers_by_item", {}) or {}

    total = len(round_uids)
    answered_ids = [uid for uid in round_uids if uid in answers]
    answered = len(answered_ids)
    correct = sum(1 for uid in answered_ids if isinstance(answers.get(uid), dict) and answers.get(uid).get("is_correct") is True)
    wrong = answered - correct
    acc = (correct / answered) if answered else 0.0

    # Modern, clean styling
    st.markdown("""
        <style>
        /* Summary page styling */
        .summary-header {
            background: linear-gradient(135deg, #00b4c8 0%, #0099b3 100%);
            border-radius: 16px;
            padding: 32px;
            margin-bottom: 24px;
            box-shadow: 0 4px 16px rgba(0,180,200,0.25);
            color: white;
        }
        .summary-title {
            font-size: 28px;
            font-weight: 700;
            margin-bottom: 8px;
            color: white;
        }
        .summary-subtitle {
            font-size: 14px;
            color: rgba(255,255,255,0.8);
            margin-bottom: 24px;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
            gap: 16px;
            margin-top: 20px;
        }
        .stat-card {
            background: rgba(255,255,255,0.15);
            border-radius: 12px;
            padding: 16px;
            text-align: center;
            backdrop-filter: blur(10px);
        }
        .stat-label {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: rgba(255,255,255,0.8);
            margin-bottom: 8px;
            font-weight: 600;
        }
        .stat-value {
            font-size: 32px;
            font-weight: 700;
            color: white;
            font-family: 'SF Mono', monospace;
        }
        .stat-subtext {
            font-size: 12px;
            color: rgba(255,255,255,0.7);
            margin-top: 4px;
        }
        .section-card {
            background: white;
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 16px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.12);
        }
        .section-header {
            font-size: 18px;
            font-weight: 600;
            color: #202124;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .section-header::before {
            content: "";
            width: 4px;
            height: 20px;
            background: #00b4c8;
            border-radius: 2px;
        }
        </style>
    """, unsafe_allow_html=True)
    
    # Beautiful header with stats
    st.markdown(f"""
        <div class="summary-header">
            <div class="summary-title">🎉 Round Complete!</div>
            <div class="summary-subtitle">Here's how you performed in this round</div>
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-label">Total</div>
                    <div class="stat-value">{total}</div>
                    <div class="stat-subtext">questions</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Answered</div>
                    <div class="stat-value">{answered}</div>
                    <div class="stat-subtext">{answered/total*100:.0f}% completed</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Correct</div>
                    <div class="stat-value" style="color: #4ade80;">{correct}</div>
                    <div class="stat-subtext">answers</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Wrong</div>
                    <div class="stat-value" style="color: #f87171;">{wrong}</div>
                    <div class="stat-subtext">answers</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Accuracy</div>
                    <div class="stat-value">{acc:.0%}</div>
                    <div class="stat-subtext">success rate</div>
                </div>
            </div>
        </div>
    """, unsafe_allow_html=True)

    # Time stats in clean cards
    try:
        r_start = int(st.session_state.get("round_start_ms", 0))
        r_end = int(st.session_state.get("round_end_ms", _now_ms()))
        r_dur = max(0, (r_end - r_start) / 1000)
        s_start = int(st.session_state.get("session_start_ms", r_start))
        s_dur = max(0, (_now_ms() - s_start) / 1000)
        
        st.markdown(f"""
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px;">
                <div style="background: white; border-radius: 12px; padding: 20px;
                            box-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.12);">
                    <div style="font-size: 12px; color: #5f6368; font-weight: 600; 
                                text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px;">
                        ⏱️ Round Time
                    </div>
                    <div style="font-size: 32px; font-weight: 700; color: #00b4c8; font-family: 'SF Mono', monospace;">
                        {r_dur/60:.1f}
                    </div>
                    <div style="font-size: 13px; color: #5f6368; margin-top: 4px;">minutes</div>
                </div>
                <div style="background: white; border-radius: 12px; padding: 20px;
                            box-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.12);">
                    <div style="font-size: 12px; color: #5f6368; font-weight: 600; 
                                text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px;">
                        ⏰ Session Total
                    </div>
                    <div style="font-size: 32px; font-weight: 700; color: #00b4c8; font-family: 'SF Mono', monospace;">
                        {s_dur/60:.1f}
                    </div>
                    <div style="font-size: 13px; color: #5f6368; margin-top: 4px;">minutes</div>
                </div>
            </div>
        """, unsafe_allow_html=True)
    except Exception:
        pass

    # Difficulty distribution with visual bars
    from collections import Counter
    def _dnum(it: Item) -> int:
        return _parse_difficulty_num(it.get("difficulty"))
    d_counter = Counter(_dnum(it) for it in items)
    
    st.markdown('<div class="section-card">', unsafe_allow_html=True)
    st.markdown('<div class="section-header">📊 Difficulty Distribution</div>', unsafe_allow_html=True)
    
    if d_counter:
        total_cnt = sum(int(v) for v in d_counter.values()) or 1
        color_map = {
            1: ("#d1f4e0", "#0d7538", "Easy"),
            2: ("#d4f4dd", "#15803d", "Normal"),
            3: ("#fef3c7", "#b45309", "Medium"),
            4: ("#fed7d7", "#c53030", "Hard"),
            5: ("#e9d5ff", "#7c3aed", "Expert")
        }
        
        for k in sorted(d_counter.keys()):
            cnt = int(d_counter.get(k, 0))
            pct = cnt * 100 / total_cnt
            bg, fg, label = color_map.get(int(k), ("#f1f3f4", "#5f6368", f"Level {k}"))
            
            st.markdown(f"""
                <div style="margin-bottom: 12px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                        <span style="font-size: 14px; font-weight: 600; color: #202124;">
                            Level {k} - {label}
                        </span>
                        <span style="font-size: 13px; color: #5f6368;">{cnt} questions ({pct:.1f}%)</span>
                    </div>
                    <div style="background: #f1f3f4; border-radius: 8px; height: 8px; overflow: hidden;">
                        <div style="background: {fg}; width: {pct}%; height: 100%; border-radius: 8px; 
                                    transition: width 0.3s ease;"></div>
                    </div>
                </div>
            """, unsafe_allow_html=True)
    else:
        st.info("No difficulty data available")
    
    st.markdown("</div>", unsafe_allow_html=True)

    # Wrong answers review - modern card design
    uid_to_item = {get_item_uid(it): it for it in items}
    wrong_uids = [uid for uid in answered_ids if answers.get(uid, {}).get("is_correct") is False]
    
    if wrong_uids:
        st.markdown('<div class="section-card">', unsafe_allow_html=True)
        st.markdown(f'<div class="section-header">❌ Wrong Answers Review ({len(wrong_uids)} questions)</div>', unsafe_allow_html=True)
        
        st.markdown("""
            <style>
            .wrong-item {
                background: #fef2f2;
                border: 1.5px solid #fecaca;
                border-radius: 8px;
                padding: 16px;
                margin-bottom: 12px;
            }
            .wrong-item-header {
                display: flex;
                justify-content: space-between;
                align-items: flex-start;
                margin-bottom: 12px;
            }
            .wrong-item-question {
                font-size: 14px;
                color: #202124;
                line-height: 1.6;
                flex: 1;
            }
            .expanderHeader-custom {
                cursor: pointer;
                user-select: none;
            }
            </style>
        """, unsafe_allow_html=True)
        
        for uid in wrong_uids[:100]:
            it = uid_to_item.get(uid)
            if not it:
                continue
                
            # Get question text
            q = it.get("question")
            if isinstance(q, list):
                q_text = "\n".join([str(x) for x in q])
            else:
                q_text = str(q) if q is not None else ""
            q_preview = (q_text or "").strip().replace("\n", " ")[:100]
            
            # Badges
            meta_badges = " ".join([
                _badge("type", get_type_display_name(str(it.get("type","-")))),
                _badge("difficulty", str(it.get("difficulty","-"))),
            ])
            
            st.markdown(meta_badges, unsafe_allow_html=True)
            
            with st.expander(f"📄 {q_preview or '[No Question]'}..."):
                # Question
                if q_text:
                    st.markdown(f"""
                        <div style="background: white; padding: 12px; border-radius: 6px; margin-bottom: 12px;">
                            <div style="font-size: 12px; color: #5f6368; font-weight: 600; margin-bottom: 6px;">QUESTION</div>
                            <div style="font-size: 14px; color: #202124; line-height: 1.6;">{q_text}</div>
                        </div>
                    """, unsafe_allow_html=True)
                
                # Options
                opts = it.get("options") or parse_options_from_question(q_text)
                if opts:
                    st.markdown('<div style="background: white; padding: 12px; border-radius: 6px; margin-bottom: 12px;">', unsafe_allow_html=True)
                    st.markdown('<div style="font-size: 12px; color: #5f6368; font-weight: 600; margin-bottom: 6px;">OPTIONS</div>', unsafe_allow_html=True)
                    for op in opts:
                        st.markdown(f'<div style="font-size: 14px; color: #202124; margin: 4px 0;">• {str(op)}</div>', unsafe_allow_html=True)
                    st.markdown('</div>', unsafe_allow_html=True)
                
                # Your answer
                rec = answers.get(uid, {})
                your_answer = ""
                if "display_choice" in rec:
                    your_answer = str(rec.get("display_choice"))
                elif "choice" in rec or "choice_text" in rec:
                    your_answer = str(rec.get("choice") or rec.get("choice_text"))
                elif "choices" in rec:
                    your_answer = ", ".join([str(x) for x in (rec.get("choices") or [])])
                elif "text" in rec:
                    your_answer = str(rec.get("text"))
                else:
                    your_answer = "(Not saved)"
                
                st.markdown(f"""
                    <div style="background: #fef2f2; padding: 12px; border-radius: 6px; margin-bottom: 12px; border-left: 3px solid #ef4444;">
                        <div style="font-size: 12px; color: #ef4444; font-weight: 600; margin-bottom: 6px;">YOUR ANSWER (WRONG)</div>
                        <div style="font-size: 14px; color: #991b1b;">{your_answer}</div>
                    </div>
                """, unsafe_allow_html=True)
                
                # Correct answer
                std_ans = flatten_answers(it.get("answer"))
                if std_ans:
                    ans_text = "<br>".join([f"• {str(a)}" for a in std_ans[:10]])
                    st.markdown(f"""
                        <div style="background: #f0fdf4; padding: 12px; border-radius: 6px; margin-bottom: 12px; border-left: 3px solid #22c55e;">
                            <div style="font-size: 12px; color: #16a34a; font-weight: 600; margin-bottom: 6px;">CORRECT ANSWER</div>
                            <div style="font-size: 14px; color: #166534;">{ans_text}</div>
                        </div>
                    """, unsafe_allow_html=True)
                
                # Analysis
                analysis = it.get("analysis")
                if analysis:
                    if isinstance(analysis, list):
                        analysis_text = "<br>".join([f"• {str(a)}" for a in analysis[:5]])
                    else:
                        analysis_text = str(analysis)
                    st.markdown(f"""
                        <div style="background: #f8f9fa; padding: 12px; border-radius: 6px;">
                            <div style="font-size: 12px; color: #5f6368; font-weight: 600; margin-bottom: 6px;">EXPLANATION</div>
                            <div style="font-size: 14px; color: #202124; line-height: 1.6;">{analysis_text}</div>
                        </div>
                    """, unsafe_allow_html=True)
        
        st.markdown("</div>", unsafe_allow_html=True)

    # Action buttons with modern styling
    st.markdown("""
        <style>
        .action-buttons-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 12px;
            margin-top: 24px;
        }
        .stButton > button {
            background: white !important;
            border: 1.5px solid #dadce0 !important;
            border-radius: 8px !important;
            padding: 12px 20px !important;
            font-size: 14px !important;
            font-weight: 500 !important;
            color: #202124 !important;
            width: 100% !important;
            transition: all 0.15s ease !important;
        }
        .stButton > button:hover {
            border-color: #00b4c8 !important;
            background: #f8f9fa !important;
            transform: translateY(-1px) !important;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1) !important;
        }
        </style>
    """, unsafe_allow_html=True)
    
    st.markdown('<div class="action-buttons-grid">', unsafe_allow_html=True)
    c_wrong, c_another, c_review = st.columns(3)
    
    # Retry wrong only
    with c_wrong:
        if st.button("🔄 Retry Wrong Questions", key="summary_retry_wrong_inline", use_container_width=True):
            wrong_items = [uid_to_item[uid] for uid in wrong_uids if uid in uid_to_item]
            
            # 🆕 需求1：去重wrong_items
            if wrong_items:
                seen_wrong_uids: Set[str] = set()
                dedup_wrong_items: List[Item] = []
                for it in wrong_items:
                    uid = get_item_uid(it)
                    if uid not in seen_wrong_uids:
                        seen_wrong_uids.add(uid)
                        dedup_wrong_items.append(it)
                wrong_items = dedup_wrong_items
            
            if wrong_items:
                st.session_state.items = wrong_items
                st.session_state.item_idx = 0
                st.session_state.correct_count = 0
                st.session_state.answered_count = 0
                st.session_state.ability = 1.0
                st.session_state.ability_variance = 1.0  # 🆕 贝叶斯方差
                st.session_state.answered_items = set()
                st.session_state.answers_by_item = {}
                st.session_state.item_selection_counts = {}  # 🆕 UCB统计
                st.session_state.total_selections = 0  # 🆕 UCB总选择次数
                # 🆕 需求1：确保round_uids唯一
                round_uids_dict = {get_item_uid(it): None for it in wrong_items}
                st.session_state.round_uids = list(round_uids_dict.keys())
                st.session_state.show_summary = False
                st.rerun()
            else:
                st.success("Perfect! No wrong items to review!")
    
    # Start another round
    with c_another:
        if st.button("🚀 Start New Round", key="summary_start_another_inline", use_container_width=True):
            _start_new_round_from_filters()
    
    # Review this round
    with c_review:
        if st.button("📖 Review This Round", key="summary_review_round_inline", use_container_width=True):
            st.session_state.show_summary = False
            st.session_state.item_idx = 0
            st.rerun()
    
    st.markdown('</div>', unsafe_allow_html=True)


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
    """创建/获取Selector实例（新增Q-Learning初始化）"""
    if Selector is None or Scorer is None or AdaptiveParams is None:
        return None
    if "_selector" in st.session_state:
        return cast(Selector, st.session_state["_selector"])  # type: ignore
    
    params = AdaptiveParams()
    scorer = Scorer(params)
    
    # 🆕 从最近15条rounds.jsonl初始化Q值
    try:
        recent_rounds = _load_recent_rounds(limit=15)
        if recent_rounds:
            history_data = []
            base_dir = _history_dir()
            for r in recent_rounds:
                detail_file = r.get("detail_file", "")
                if detail_file:
                    detail_path = os.path.join(base_dir, detail_file)
                    if os.path.exists(detail_path):
                        try:
                            with open(detail_path, "r", encoding="utf-8") as f:
                                detail = json.load(f)
                            history_data.append(detail)
                        except Exception:
                            pass
            
            if history_data:
                scorer.initialize_from_history(history_data)
    except Exception:
        pass  # 如果初始化失败，Q值保持默认0
    
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
    """加载匹配筛选条件的题目
    
    🆕 改进：先加载所有匹配的题目，然后随机选择max_items个
    这样确保每次生成的题目都不同（而不是总是文件开头的那几道题）
    """
    all_matched_items: List[Item] = []
    progress = st.progress(0.0)
    total_bytes = os.path.getsize(file_path)
    processed_bytes = 0
    
    # 第一步：加载所有匹配筛选条件的题目（不限制数量）
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            processed_bytes += len(line.encode("utf-8"))
            try:
                raw_item = json_loads(line)
            except Exception:
                continue
            item = normalize_marketreg_item(raw_item)
            if item_matches_filters(item, selected_fields, selected_types, selected_difficulties, selected_docs):
                all_matched_items.append(item)
            progress.progress(min(0.999, processed_bytes / max(1, total_bytes)))
    
    progress.progress(1.0)
    
    # 第二步：从所有匹配的题目中随机选择max_items个
    if len(all_matched_items) <= max_items:
        # 如果匹配的题目不足，返回全部
        return all_matched_items
    else:
        # 随机选择max_items个题目
        return random.sample(all_matched_items, max_items)


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


def _format_question_text(text: str) -> str:
    """Format question text for better readability."""
    if not text:
        return text
    
    # Escape HTML special characters
    import html
    text = html.escape(str(text))
    
    # Replace patterns like "陈述1:", "陈述2:", "Statement 1:", etc. with styled versions
    text = re.sub(r'(陈述\s*[0-9]+\s*[：:])', r'<div style="margin-top:12px;"><strong>\1</strong>', text)
    text = re.sub(r'(Statement\s*[0-9]+\s*[：:])', r'<div style="margin-top:12px;"><strong>\1</strong>', text)
    
    # Add closing div for styled sections
    text = re.sub(r'</strong>(.*?)(?=<div style="margin-top:12px;">|$)', r'</strong>\1</div>', text, flags=re.DOTALL)
    
    # Replace double newlines with paragraph breaks
    text = text.replace('\n\n', '</p><p style="margin:12px 0;">')
    
    # Replace single newlines with line breaks
    text = text.replace('\n', '<br>')
    
    return text


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
    
    # Clean, modern question card design with enhanced text formatting
    st.markdown("""
        <style>
        .question-card {
            background: white;
            border-radius: 12px;
            padding: 20px 24px;
            margin: 0 0 16px 0;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.12);
            border-left: 4px solid #00b4c8;
        }
        .question-text {
            font-size: 15px;
            line-height: 1.7;
            color: #202124;
            font-weight: 400;
            margin-top: 12px;
        }
        .question-text p {
            margin: 8px 0;
        }
        /* Style for statements/陈述 */
        .question-text strong {
            color: #00b4c8;
            font-weight: 600;
        }
        /* Style for line breaks and paragraphs */
        .question-text br + br {
            display: block;
            margin: 10px 0;
            content: "";
        }
        .question-label {
            font-size: 12px;
            color: #5f6368;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 2px;
        }
        .badges-container {
            margin-bottom: 10px;
            display: flex;
            flex-wrap: wrap;
            align-items: center;
        }
        </style>
    """, unsafe_allow_html=True)
    
    # Render question in a clean card
    st.markdown('<div class="question-card">', unsafe_allow_html=True)
    
    # Badges at top
    st.markdown('<div class="badges-container">', unsafe_allow_html=True)
    render_item_badges(item)
    st.markdown('</div>', unsafe_allow_html=True)

    # Question content with smart formatting
    question = item.get("question")
    question_html = ""
    if isinstance(question, list):
        for idx, q in enumerate(question):
            question_html += f'<div class="question-label">Question {idx + 1}</div>'
            question_html += f'<div class="question-text">{_format_question_text(q)}</div>'
    elif isinstance(question, str):
        question_html = f'<div class="question-text">{_format_question_text(question)}</div>'
    else:
        question_html = '<div class="question-text" style="color: #999;">No valid question field</div>'
    
    st.markdown(question_html + '</div>', unsafe_allow_html=True)

    q_type = str(item.get("type", "")).strip().lower()
    answers = flatten_answers(item.get("answer"))
    user_inputs: Dict[str, Any] = {}
    is_correct: Optional[bool] = None

    # Get unique item UID for component keys and prior answers to lock UI
    item_id = get_item_uid(item)
    answers_by_item: Dict[str, Any] = st.session_state.get("answers_by_item", {}) or {}
    prior = answers_by_item.get(item_id)
    is_locked = prior is not None
    
    # Clean answer section styling
    st.markdown("""
        <style>
        /* Radio buttons - clean Material Design style with enhanced options */
        .stRadio > label {
            font-size: 14px !important;
            font-weight: 500 !important;
            color: #5f6368 !important;
            margin-bottom: 12px !important;
        }
        .stRadio > label > div[role="radiogroup"] {
            gap: 8px !important;
        }
        .stRadio > label > div[role="radiogroup"] > label {
            background: white !important;
            border: 1.5px solid #dadce0 !important;
            border-radius: 8px !important;
            padding: 12px 16px !important;
            margin: 0 !important;
            transition: all 0.15s ease !important;
            cursor: pointer !important;
            min-height: 48px !important;
            display: flex !important;
            align-items: center !important;
        }
        .stRadio > label > div[role="radiogroup"] > label > div {
            display: flex !important;
            align-items: flex-start !important;
            gap: 10px !important;
            width: 100% !important;
        }
        .stRadio > label > div[role="radiogroup"] > label > div > div:first-child {
            flex-shrink: 0 !important;
            margin-top: 2px !important;
        }
        .stRadio > label > div[role="radiogroup"] > label > div > div:last-child {
            flex: 1 !important;
            line-height: 1.6 !important;
            color: #202124 !important;
        }
        .stRadio > label > div[role="radiogroup"] > label:hover {
            border-color: #00b4c8 !important;
            background: #f8f9fa !important;
            box-shadow: 0 1px 3px rgba(0,0,0,0.08) !important;
        }
        .stRadio > label > div[role="radiogroup"] > label[data-checked="true"] {
            background: linear-gradient(135deg, #e6f4f7 0%, #f0f9fa 100%) !important;
            border-color: #00b4c8 !important;
            border-width: 2px !important;
            box-shadow: 0 2px 4px rgba(0,180,200,0.15) !important;
        }
        .stRadio > label > div[role="radiogroup"] > label[data-checked="true"] > div > div:last-child {
            color: #00838f !important;
            font-weight: 500 !important;
        }
        /* Multiselect */
        .stMultiSelect > div > div {
            border-radius: 8px !important;
            border: 1.5px solid #dadce0 !important;
        }
        .stMultiSelect > div > div:focus-within {
            border-color: #00b4c8 !important;
            box-shadow: 0 0 0 3px rgba(0,180,200,0.1) !important;
        }
        /* Text inputs */
        .stTextInput > div > div > input,
        .stTextArea > div > div > textarea {
            border-radius: 8px !important;
            border: 1.5px solid #dadce0 !important;
            padding: 10px 14px !important;
            font-size: 14px !important;
            transition: all 0.15s ease !important;
            line-height: 1.6 !important;
        }
        .stTextInput > div > div > input:focus,
        .stTextArea > div > div > textarea:focus {
            border-color: #00b4c8 !important;
            box-shadow: 0 0 0 3px rgba(0,180,200,0.1) !important;
        }
        </style>
    """, unsafe_allow_html=True)
    
    if q_type == "single_choice":
        options = item.get("options")
        if not options:
            options = parse_options_from_question(question)
        if options:
            # Set default index based on prior answer to truly lock the widget selection
            default_index = None
            if is_locked and isinstance(prior, dict):
                # If locked, restore previous choice
                if prior.get("choice") in options:
                    try:
                        default_index = options.index(prior.get("choice"))
                    except Exception:
                        default_index = None
                # Also restore is_correct from prior
                if prior.get("is_correct") is not None:
                    is_correct = bool(prior.get("is_correct"))
            
            choice = st.radio(
                "Select your answer",
                options,
                index=default_index,
                key=f"sc_radio_{item_id}",
                disabled=is_locked,
            )
            user_inputs["choice"] = choice
            
            # Only evaluate if not locked or if is_correct wasn't restored from prior
            if not is_locked or is_correct is None:
                # Correct answer often like 'C'
                correct_letter = answers[0] if answers else ""
                is_correct = evaluate_single_choice(choice, correct_letter)
        else:
            st.warning("⚠️ No options detected; cannot auto-grade")
            # If locked, show prior choice_text and disable editing
            text_default = str(prior.get("choice_text", "")) if (is_locked and isinstance(prior, dict)) else ""
            text = st.text_input(
                "Your answer (e.g. A/B/C/D)",
                key=f"sc_text_{item_id}",
                value=text_default if is_locked else None,
                disabled=is_locked,
            )
            user_inputs["choice_text"] = text
            if text:
                is_correct = evaluate_single_choice(text, answers[0] if answers else "")

    elif q_type == "fill_blank":
        # If locked, prefill and disable, and restore is_correct from prior
        if is_locked and isinstance(prior, dict):
            text_default = str(prior.get("text", ""))
            # Restore is_correct from prior if available
            if prior.get("is_correct") is not None:
                is_correct = bool(prior.get("is_correct"))
        else:
            text_default = ""
        
        text = st.text_area(
            "Your answer",
            height=100,
            key=f"fb_text_{item_id}",
            value=text_default if is_locked else None,
            disabled=is_locked,
        )
        user_inputs["text"] = text
        
        # Only evaluate if not locked or if is_correct wasn't restored
        if (not is_locked or is_correct is None) and answers:
            is_correct = evaluate_fill_blank(text, answers)
        elif not answers:
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
        if is_locked and isinstance(prior, dict):
            if isinstance(prior.get("choices"), list):
                default_selection = [opt for opt in options if opt in prior.get("choices")]
            # Restore is_correct from prior if available
            if prior.get("is_correct") is not None:
                is_correct = bool(prior.get("is_correct"))
        
        selected = st.multiselect(
            "Select all that apply",
            options,
            default=default_selection if is_locked else [],
            key=f"mc_multi_{item_id}",
            disabled=is_locked,
        )
        user_inputs["choices"] = selected
        
        # Only evaluate if not locked or if is_correct wasn't restored
        if (not is_locked or is_correct is None) and selected:
            is_correct = evaluate_multiple_choice(selected, answers, options)
        elif not selected:
            is_correct = None

    elif q_type == "true_false":
        # True/False question with radio buttons
        tf_options = ["True", "False"]
        # If locked, set default index to previous selection to lock widget value
        default_index = None
        if is_locked and isinstance(prior, dict):
            if prior.get("display_choice") in tf_options:
                try:
                    default_index = tf_options.index(prior.get("display_choice"))
                except Exception:
                    default_index = None
            # Restore is_correct from prior if available
            if prior.get("is_correct") is not None:
                is_correct = bool(prior.get("is_correct"))
        
        choice = st.radio(
            "True or False",
            tf_options,
            index=default_index,
            key=f"tf_radio_{item_id}",
            disabled=is_locked,
            horizontal=True,
        )
        user_inputs["choice"] = choice
        
        # Only evaluate if not locked or if is_correct wasn't restored
        if (not is_locked or is_correct is None) and choice and answers:
            # Extract True/False from the choice
            selected_value = "True" if "True" in choice else "False"
            correct_answer = answers[0] if answers else ""
            is_correct = evaluate_true_false(selected_value, correct_answer)
        elif not choice or not answers:
            is_correct = None

    else:
        # Open-ended types: case_analysis, comprehension, etc.
        text_default = str(prior.get("text", "")) if (is_locked and isinstance(prior, dict)) else ""
        text = st.text_area(
            "Your response",
            height=150,
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
    
    # Clean expander styling
    st.markdown("""
        <style>
        .streamlit-expanderHeader {
            background: white !important;
            border: 1.5px solid #dadce0 !important;
            border-radius: 8px !important;
            padding: 12px 16px !important;
            font-weight: 500 !important;
            font-size: 14px !important;
            color: #5f6368 !important;
            transition: all 0.15s ease !important;
            margin-top: 16px !important;
        }
        .streamlit-expanderHeader:hover {
            border-color: #00b4c8 !important;
            background: #f8f9fa !important;
        }
        .streamlit-expanderContent {
            background: white !important;
            border-radius: 0 0 8px 8px !important;
            padding: 16px !important;
            border: 1.5px solid #dadce0 !important;
            border-top: none !important;
            margin-top: -2px !important;
        }
        .answer-item {
            background: #e6f4e6;
            border-left: 3px solid #34a853;
            padding: 10px 14px;
            border-radius: 6px;
            margin: 8px 0;
            font-size: 14px;
            color: #202124;
        }
        .analysis-item {
            background: #f8f9fa;
            padding: 10px 14px;
            border-radius: 6px;
            margin: 8px 0;
            font-size: 14px;
            line-height: 1.6;
            color: #5f6368;
        }
        .kp-item {
            background: #fef7e0;
            border-left: 3px solid #f9ab00;
            padding: 10px 14px;
            border-radius: 6px;
            margin: 8px 0;
            font-size: 14px;
            color: #202124;
        }
        .section-title {
            font-size: 13px;
            font-weight: 600;
            color: #5f6368;
            margin: 12px 0 6px 0;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        </style>
    """, unsafe_allow_html=True)

    # 做错或查看已锁定错题时自动展开解析
    expand_analysis = bool(is_correct is False)
    with st.expander("View Answer & Explanation", expanded=expand_analysis):
        if answers:
            st.markdown('<div class="section-title">Correct Answer</div>', unsafe_allow_html=True)
            for a in answers:
                st.markdown(f'<div class="answer-item">{a}</div>', unsafe_allow_html=True)
        
        analysis = item.get("analysis")
        if analysis:
            st.markdown('<div class="section-title">Explanation</div>', unsafe_allow_html=True)
            if isinstance(analysis, list):
                for a in analysis:
                    st.markdown(f'<div class="analysis-item">{str(a)}</div>', unsafe_allow_html=True)
            else:
                st.markdown(f'<div class="analysis-item">{str(analysis)}</div>', unsafe_allow_html=True)
        
        kps = item.get("knowledge_points")
        if kps:
            st.markdown('<div class="section-title">Knowledge Points</div>', unsafe_allow_html=True)
            kp_text = ", ".join([str(x) for x in kps]) if isinstance(kps, list) else str(kps)
            st.markdown(f'<div class="kp-item">{kp_text}</div>', unsafe_allow_html=True)

    # Clean result indicators
    if is_correct is True:
        st.markdown("""
            <div style="background: #e6f4e6; border-radius: 8px; padding: 10px 14px; margin: 12px 0 0 0; 
                        border-left: 3px solid #34a853;">
                <span style="font-size: 14px; font-weight: 600; color: #1e8e3e;">✓ Correct</span>
            </div>
        """, unsafe_allow_html=True)
    elif is_correct is False:
        st.markdown("""
            <div style="background: #fce8e6; border-radius: 8px; padding: 10px 14px; margin: 12px 0 0 0; 
                        border-left: 3px solid #ea4335;">
                <span style="font-size: 14px; font-weight: 600; color: #c5221f;">✗ Incorrect</span>
            </div>
        """, unsafe_allow_html=True)

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
    # Fine-tune quiz UI spacing and badge value color (CSS-only, no logic change)
    try:
        st.markdown(
            """
            <style>
            /* 1) Shrink spacing between top metrics and progress bar */
            .holo-header{ padding: 2px 12px !important; margin: 0 0 2px 0 !important }
            .qa-progress-bar{ width:80% !important; margin: 2px auto 6px auto !important }
            /* 3) Darken only badge values a bit (labels keep original color) */
            .badge-pill{ color:#1f2937 !important }
            .badge-pill .lbl{ color:#2d3436 !important; font-weight:600 !important }
            </style>
            """,
            unsafe_allow_html=True,
        )
    except Exception:
        pass
    # Round Summary: switch to creamy glass style with finer look
    try:
        st.markdown(
            """
            <style>
            .sumCard{
              position: relative; background: rgba(255,255,255,0.78) !important; backdrop-filter: blur(12px) !important;
              border-radius:14px !important; box-shadow: 0 2px 12px rgba(0,0,0,0.05) !important;
            }
            .sumCard::before{ content:""; position:absolute; inset:0; border-radius:14px; pointer-events:none;
              box-shadow: inset 0 0 0 1px rgba(255,255,255,0.9) }
            .sumCard .sectionTitle{ color:#2d3436 !important; margin-bottom:8px !important }
            .sumCard details>summary{
              background: rgba(255,255,255,0.78) !important; border-radius:12px !important; padding:8px 12px !important;
              box-shadow: 0 2px 10px rgba(0,0,0,0.05), inset 0 0 0 1px rgba(255,255,255,0.9) !important;
            }
            .sumCard details[open]{ box-shadow: 0 4px 14px rgba(0,0,0,0.06), inset 0 0 0 1px rgba(255,255,255,0.9) !important }
            .sumCard .stDataFrame [data-testid="stTable"][role="table"]{ color:#2d3436 }
            .sumCard .stDataFrame thead tr th{ background: rgba(255,255,255,0.85) !important }
            .sumCard .stDataFrame tbody tr td{ background: rgba(255,255,255,0.70) !important }
            .sumCard .stDataFrame{ filter:none !important; box-shadow:none !important }
            </style>
            """,
            unsafe_allow_html=True,
        )
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
    # Fine-tune quiz HUD colors, spacing, and progress bar scale (CSS-only overrides)
    try:
        st.markdown(
            """
            <style>
            /* Metrics trio styling - creamy mint, consistent with current theme */
            .holo-header{ background: transparent !important; box-shadow:none !important; padding:4px 0 !important; margin:0 0 4px 0 !important }
            [data-testid="stMetric"]{
              background: linear-gradient(180deg, #FFFFFF 0%, #F0FBF7 100%) !important;
              border-radius:14px !important; padding:6px 10px !important;
              box-shadow: 0 4px 16px rgba(0,0,0,0.06), inset 0 0 0 1px rgba(255,255,255,0.9) !important;
              margin: 4px 6px 2px 6px !important;
            }
            [data-testid="stMetricLabel"]{ color:#2D3436 !important }
            [data-testid="stMetricValue"]{ color:#1BB371 !important; text-shadow:none; transition: all .6s ease }

            /* Reduce gap between metrics row and progress bar */
            .qa-progress-bar{ margin: 2px 0 8px 0 !important; gap:8px }
            /* Enlarge progress cells by 1.5x */
            .qa-progress-bar .cell{ width:42px !important; height:42px !important; font-size:18px !important; border-radius:8px !important }
            </style>
            """,
            unsafe_allow_html=True,
        )
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

    if not has_items_hdr:
        # 初始页：顶端四按钮（与右侧悬浮栏相同功能）
        # 首页按钮统一方形样式（仅首页注入，避免影响答题/历史/总结页）
        st.markdown(
            """
            <style>
            .block-container .stButton>button{
              display:flex !important; 
              align-items:center !important; 
              justify-content:center !important; 
              flex-direction:column !important;
              gap: 12px !important;
              width:66% !important; 
              aspect-ratio:1 / 1 !important; 
              border-radius:18px !important; 
              font-size:36px !important; 
              font-weight:700 !important;
              letter-spacing: 1.2px !important;
              margin:10px auto !important; 
              white-space: pre-line !important; 
              text-align:center !important; 
              line-height:1.35 !important;
              color: #202124 !important;
              background: linear-gradient(135deg, rgba(0,180,200,.2) 0%, rgba(0,180,200,.1) 100%) !important;
              box-shadow: 0 3px 10px rgba(0,0,0,0.12), inset 0 0 0 1.5px rgba(0,180,200,.28) !important;
              transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1) !important;
            }
            /* Make emoji larger - target first line */
            .block-container .stButton>button::first-line {
              font-size: 60px !important;
              line-height: 1 !important;
            }
            .block-container .stButton>button:hover{ 
              transform: translateY(-4px) scale(1.03) !important; 
              box-shadow: 0 8px 20px rgba(0,180,200,0.3), inset 0 0 0 1.5px rgba(0,180,200,.4) !important;
              background: linear-gradient(135deg, rgba(0,180,200,.28) 0%, rgba(0,180,200,.15) 100%) !important;
            }
            .block-container .stButton>button:active{ 
              transform: translateY(-2px) scale(0.99) !important; 
              box-shadow: 0 4px 12px rgba(0,180,200,0.35), inset 0 0 0 2px rgba(0,180,200,.45) !important;
            }
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
        sel_field = st.sidebar.multiselect("the Legal Field", sorted(values["field"]))
        # Type 显示名映射
        type_options = sorted(values["type"])
        type_display_options = [get_type_display_name(t) for t in type_options]
        sel_type_display = st.sidebar.multiselect("Question Type", type_display_options)
        display_to_key = {get_type_display_name(t): t for t in type_options}
        sel_type = [display_to_key[d] for d in sel_type_display]
        sel_diff = st.sidebar.multiselect("Difficulty level", sorted(values["difficulty"]))
        sel_doc = st.sidebar.multiselect("Source of regulatory documents", sorted(values["doc"]))
    else:
        sel_field = sel_type = sel_diff = sel_doc = []
    st.sidebar.markdown("**Items to Load**")
    max_items = st.sidebar.slider("Items to Load", min_value=5, max_value=200, value=50, step=5, label_visibility="collapsed")
    st.sidebar.markdown("**Review Frequency**")
    review_mode = st.sidebar.selectbox("Review Mode", ["Standard", "Light", "Intensive", "Custom"], index=0)
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
                # 🆕 需求2：在处理前先随机打乱，确保相同筛选条件下每次题目不同
                random.shuffle(items)
                
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
                            # 🆕 新算法字段
                            self.ability_variance = st.session_state.get("ability_variance", 1.0)
                            self.q_values = {}
                            self.item_selection_counts = st.session_state.get("item_selection_counts", {})
                            self.total_selections = st.session_state.get("total_selections", 0)
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
                    
                    # 🆕 需求1：最终安全去重检查（确保初始化时绝对没有重复）
                    final_sorted: List[Item] = []
                    final_seen_uids: Set[str] = set()
                    for it in items_sorted:
                        uid = get_item_uid(it)
                        if uid not in final_seen_uids:
                            final_seen_uids.add(uid)
                            final_sorted.append(it)
                    items_sorted = final_sorted
                    
                    # 🆕 需求2：最终随机打乱（确保每次题目顺序不同）
                    random.shuffle(items_sorted)
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
                    
                    # 🆕 需求2：最终随机打乱（确保每次题目顺序不同）
                    random.shuffle(items_sorted)
                    
                st.session_state.items = items_sorted
                st.session_state.item_idx = 0
                st.session_state.correct_count = 0
                st.session_state.answered_count = 0
                st.session_state.ability = ability
                st.session_state.ability_variance = 1.0  # 🆕 贝叶斯方差
                # Reset per-session states to avoid遗留提交影响新会话
                st.session_state.answered_items = set()
                st.session_state.answers_by_item = {}
                st.session_state.item_selection_counts = {}  # 🆕 UCB统计
                st.session_state.total_selections = 0  # 🆕 UCB总选择次数
                st.session_state.recent_correct_complex_ids = []
                # 记录当前轮次的 UID 序列与起止时间，用于统计
                # 🆕 需求1：确保round_uids也是唯一的（使用dict保序去重）
                round_uids_dict = {get_item_uid(it): None for it in items_sorted}
                st.session_state.round_uids = list(round_uids_dict.keys())
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

        # Modern, elegant header with integrated stats and progress
        answered = int(st.session_state.get("answered_count", 0))
        correct = int(st.session_state.get("correct_count", 0))
        acc = (correct / answered) if answered else 0.0
        ability = st.session_state.get('ability', 3.0)
        
        st.markdown("""
            <style>
            /* Reduce top padding */
            .block-container {
                padding-top: 1rem !important;
            }
            /* Hide default metrics */
            .stMetric {
                display: none !important;
            }
            </style>
        """, unsafe_allow_html=True)
        
        # Beautiful integrated header
        st.markdown(f"""
            <div style="background: linear-gradient(135deg, #00b4c8 0%, #0099b3 100%); 
                        border-radius: 16px; padding: 20px 24px; margin-bottom: 16px;
                        box-shadow: 0 4px 16px rgba(0,180,200,0.25);">
                <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 16px;">
                    <div style="flex: 1; min-width: 80px;">
                        <div style="color: rgba(255,255,255,0.8); font-size: 11px; font-weight: 600; 
                                    text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px;">Question</div>
                        <div style="color: white; font-size: 24px; font-weight: 700; font-family: 'SF Mono', monospace;">
                            {idx + 1}<span style="color: rgba(255,255,255,0.6); font-size: 18px;">/{n}</span>
                        </div>
                    </div>
                    <div style="flex: 1; min-width: 100px;">
                        <div style="color: rgba(255,255,255,0.8); font-size: 11px; font-weight: 600; 
                                    text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px;">Accuracy</div>
                        <div style="color: white; font-size: 24px; font-weight: 700; font-family: 'SF Mono', monospace;">
                            {acc:.0%}
                        </div>
                    </div>
                    <div style="flex: 1; min-width: 80px;">
                        <div style="color: rgba(255,255,255,0.8); font-size: 11px; font-weight: 600; 
                                    text-transform: uppercase; letter-spacing: 1px; margin-bottom: 4px;">Ability</div>
                        <div style="color: white; font-size: 24px; font-weight: 700; font-family: 'SF Mono', monospace;">
                            {ability:.1f}<span style="color: rgba(255,255,255,0.6); font-size: 16px;">/5.0</span>
                        </div>
                    </div>
                </div>
            </div>
        """, unsafe_allow_html=True)
        # Progress indicator bar (cells) above question info badges
        try:
            # Use current display order to align cells with on-screen question order (round_uids may differ after re-sorting)
            uids = [get_item_uid(it) for it in items]
            answers_by_item_dict = st.session_state.get("answers_by_item", {}) or {}
            cells = []
            for i, uid in enumerate(uids):
                done = uid in answers_by_item_dict
                cls = "cell done" if done else "cell todo"
                if i == idx:
                    cls += " current"
                cells.append(f"<div class='{cls}' title='Question {i+1}'>{i+1}</div>")
            st.markdown(
                """
                <style>
                /* Enhanced Progress Bar with Beautiful Animations */
                .qa-progress-bar{ 
                    display:flex; gap:6px; align-items:center; overflow-x:auto; 
                    padding:8px 0; margin:0 0 20px 0;
                }
                .qa-progress-bar .cell{ 
                    width:32px; height:32px; border-radius:8px; 
                    display:flex; align-items:center; justify-content:center; 
                    font-size:12px; font-weight:600;
                    background:#f1f3f4; 
                    color:#5f6368; 
                    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
                    cursor: pointer;
                    flex-shrink: 0;
                    position: relative;
                    overflow: hidden;
                }
                /* Ripple effect on hover */
                .qa-progress-bar .cell::before {
                    content: "";
                    position: absolute;
                    inset: 0;
                    background: radial-gradient(circle, rgba(0,180,200,0.2) 0%, transparent 70%);
                    transform: scale(0);
                    transition: transform 0.3s ease;
                }
                .qa-progress-bar .cell:hover::before {
                    transform: scale(1);
                }
                .qa-progress-bar .cell:hover {
                    transform: translateY(-2px) scale(1.08);
                    box-shadow: 0 4px 8px rgba(0,0,0,0.15);
                }
                /* Completed questions - green gradient with shadow */
                .qa-progress-bar .cell.done{ 
                    background: linear-gradient(135deg, #34a853 0%, #2d9348 100%); 
                    color: white;
                    box-shadow: 0 2px 4px rgba(52,168,83,0.3);
                }
                .qa-progress-bar .cell.done:hover {
                    box-shadow: 0 4px 12px rgba(52,168,83,0.5);
                }
                /* Current question - blue gradient with pulse animation */
                .qa-progress-bar .cell.current{ 
                    background: linear-gradient(135deg, #00b4c8 0%, #0099b3 100%);
                    color: white;
                    transform: scale(1.15);
                    box-shadow: 0 4px 8px rgba(0,180,200,0.4),
                                0 0 0 4px rgba(0,180,200,0.15);
                    animation: progressPulse 2s ease-in-out infinite;
                }
                @keyframes progressPulse {
                    0%, 100% { 
                        box-shadow: 0 4px 8px rgba(0,180,200,0.4), 0 0 0 4px rgba(0,180,200,0.15);
                    }
                    50% { 
                        box-shadow: 0 6px 12px rgba(0,180,200,0.6), 0 0 0 6px rgba(0,180,200,0.25);
                    }
                }
                .qa-progress-bar .cell.current:hover {
                    animation: none;
                    transform: scale(1.2);
                }
                </style>
                """,
                unsafe_allow_html=True,
            )
            st.markdown(f"<div class='qa-progress-bar'>{''.join(cells)}</div>", unsafe_allow_html=True)
        except Exception:
            pass

        item = items[idx]
        is_correct, _inputs = show_item_view(item)

        # Clean, modern button styling
        st.markdown("""
            <style>
            .stButton > button {
                background: white !important;
                border: 1.5px solid #dadce0 !important;
                border-radius: 8px !important;
                padding: 10px 20px !important;
                font-size: 14px !important;
                font-weight: 500 !important;
                color: #5f6368 !important;
                transition: all 0.15s ease !important;
                width: 100% !important;
                margin-top: 16px !important;
            }
            .stButton > button:hover {
                border-color: #00b4c8 !important;
                background: #f8f9fa !important;
                color: #202124 !important;
            }
            /* Primary button (Submit) */
            .stButton > button[kind="primary"] {
                background: linear-gradient(135deg, #00b4c8 0%, #0099b3 100%) !important;
                color: white !important;
                border: none !important;
                box-shadow: 0 1px 3px rgba(0,0,0,0.12), 0 1px 2px rgba(0,0,0,0.24) !important;
            }
            .stButton > button[kind="primary"]:hover {
                background: linear-gradient(135deg, #00c4d8 0%, #00a9c3 100%) !important;
                box-shadow: 0 2px 4px rgba(0,0,0,0.15), 0 2px 3px rgba(0,0,0,0.3) !important;
            }
            </style>
        """, unsafe_allow_html=True)

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
            prev_clicked = st.button("← Previous", key=f"prev_btn_{idx}", use_container_width=True)
        with col2:
            # Always render Next button; gate on click to show warning only when attempted
            next_clicked = st.button("Next →", key=f"next_btn_{idx}", use_container_width=True)
        with col3:
            final_submit_btn = st.button("Submit", key=f"final_submit_btn_{idx}", use_container_width=True, type="primary")

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
                    
                    # 🆕 贝叶斯能力值更新（替代固定步长±0.15）
                    try:
                        from adaptive.ability import update_ability_bayesian
                        current_ability = float(st.session_state.get("ability", 1.0))
                        current_variance = float(st.session_state.get("ability_variance", 1.0))
                        difficulty = float(_parse_difficulty_num(item.get("difficulty")))
                        
                        new_ability, new_variance = update_ability_bayesian(
                            current_ability, current_variance, is_correct, difficulty
                        )
                        st.session_state.ability = new_ability
                        st.session_state.ability_variance = new_variance
                    except Exception:
                        # 降级：使用旧算法
                        st.session_state.ability = float(st.session_state.get("ability", 1.0)) + 0.15
                    
                    try:
                        if _parse_difficulty_num(item.get("difficulty")) >= 3:
                            arr = cast(List[str], st.session_state.get("recent_correct_complex_ids", []))
                            arr = ([str(item.get("id"))] + [x for x in arr if x != str(item.get("id"))])[:10]
                            st.session_state["recent_correct_complex_ids"] = arr
                    except Exception:
                        pass
                else:
                    # 🆕 贝叶斯能力值更新（答错）
                    try:
                        from adaptive.ability import update_ability_bayesian
                        current_ability = float(st.session_state.get("ability", 1.0))
                        current_variance = float(st.session_state.get("ability_variance", 1.0))
                        difficulty = float(_parse_difficulty_num(item.get("difficulty")))
                        
                        new_ability, new_variance = update_ability_bayesian(
                            current_ability, current_variance, is_correct, difficulty
                        )
                        st.session_state.ability = new_ability
                        st.session_state.ability_variance = new_variance
                    except Exception:
                        # 降级：使用旧算法
                        st.session_state.ability = float(st.session_state.get("ability", 1.0)) - 0.15
                
                # 🆕 Q-Learning更新
                try:
                    selector = _ensure_selector()
                    if selector and hasattr(selector, 'scorer'):
                        reward = 1.0 if is_correct else -0.5
                        selector.scorer.update_q_value(current_item_id, reward)
                except Exception:
                    pass
                
                # 🆕 UCB统计更新
                try:
                    counts = st.session_state.get("item_selection_counts", {})
                    counts[current_item_id] = counts.get(current_item_id, 0) + 1
                    st.session_state["item_selection_counts"] = counts
                    st.session_state["total_selections"] = st.session_state.get("total_selections", 0) + 1
                except Exception:
                    pass

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
                            # 兼容性处理：旧格式字典 → 新格式ReviewEntry
                            if isinstance(old, dict):
                                # 从旧格式转换：推断EF和repetitions
                                bucket = int(old.get("bucket", 0))
                                next_ts = int(old.get("next_ts_ms", 0))
                                # 根据bucket推断EF和repetitions（近似值）
                                ef = old.get("easiness_factor", 2.5 if bucket == 0 else 2.0 + bucket * 0.2)
                                reps = old.get("repetitions", max(0, bucket))
                                interval = old.get("interval_days", [1, 3, 7, 21][min(bucket, 3)])
                                old_entry = ReviewEntry(
                                    easiness_factor=float(ef),
                                    interval_days=float(interval),
                                    repetitions=int(reps),
                                    next_ts_ms=next_ts
                                )
                            else:
                                # 已经是ReviewEntry对象
                                old_entry = old
                        else:
                            old_entry = None
                        new_entry = sched.on_result(old_entry, bool(is_correct))
                        # 保存为新格式（包含所有SM-2字段）
                        rs[current_item_id] = {
                            "easiness_factor": new_entry.easiness_factor,
                            "interval_days": new_entry.interval_days,
                            "repetitions": new_entry.repetitions,
                            "next_ts_ms": new_entry.next_ts_ms,
                            # 向后兼容：也保留bucket字段
                            "bucket": new_entry.bucket
                        }
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
                                self.answers_by_item = {}
                                # 🆕 新算法字段
                                self.ability_variance = st.session_state.get("ability_variance", 1.0)
                                self.q_values = {}
                                self.item_selection_counts = st.session_state.get("item_selection_counts", {})
                                self.total_selections = st.session_state.get("total_selections", 0)
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
                    
                    # 🆕 需求1：合并后最终去重，确保一轮题目中完全不同
                    combined = items[: idx + 1] + resorted
                    final_items: List[Item] = []
                    final_seen: Set[str] = set()
                    for it in combined:
                        uid = get_item_uid(it)
                        if uid not in final_seen:
                            final_seen.add(uid)
                            final_items.append(it)
                    st.session_state.items = final_items

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
