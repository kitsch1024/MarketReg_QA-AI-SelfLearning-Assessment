# ==== 视觉层变更日志 ====
# - 新增 inject_holo_light_theme(): 注入全局「雾灰→淡青瓷」轻量主题、悬浮折叠侧边栏、影院级居中布局、按钮光束样式、题项伪滑动、微交互动画与表格配色
# - 新增 render_holo_header(): 顶部固定「全息仪表盘」(LED 数字屏风格) 展示 Current/Accuracy/Ability，实时读取 session_state
# - 仅通过 CSS/样式实现动画与交互感受，不改动任何业务逻辑/函数签名/状态键
# - 运行方式：直接执行本文件；内部导入 app.py 的 main() 并在之前注入主题与头部 UI

import streamlit as st

from app import main as base_main  # 业务逻辑完全复用


def inject_holo_light_theme() -> None:
    """视觉层注入：轻量淡色科技风 + 微交互动画（CSS-only）。"""
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
        /* 顶部全息仪表盘 */
        .holo-header{ position: sticky; top:0; z-index:999; margin:-8px -16px 16px -16px; padding:8px 16px;
          background: linear-gradient(180deg, rgba(255,255,255,.65), rgba(255,255,255,.30));
          backdrop-filter: saturate(1.2) blur(6px);
          box-shadow: inset 0 0 0 1px rgba(0,180,200,.15), 0 6px 12px rgba(0,0,0,.06);
        }
        .holo-grid{ display:flex; gap:16px; align-items:center }
        .holo-tile{ flex:1; border-radius:12px; padding:8px 12px; background: rgba(255,255,255,.85);
          box-shadow: inset 0 0 0 1px rgba(0,180,200,.15) }
        .holo-label{ font-size:12px; color: rgba(45,52,54,.7); margin-bottom:4px }
        .holo-led{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, "Liberation Mono", monospace; font-weight:800; font-size:22px; letter-spacing:1px }
        .holo-led.cyan{ color: var(--primary) }
        .holo-led.green{ color: var(--ok) }

        /* 悬浮折叠舱侧边栏 */
        [data-testid="stSidebar"]{
          background: linear-gradient(180deg, rgba(255,255,255,.35), rgba(255,255,255,0) 12%), var(--panel) !important;
          box-shadow: inset 0 0 0 1px rgba(0,180,200,.15);
          width:56px !important; min-width:56px !important; transition: width .25s ease, min-width .25s ease; overflow:hidden;
        }
        [data-testid="stSidebar"]:hover{ width:320px !important; min-width:320px !important }
        [data-testid="stSidebar"] *{ color: var(--text) !important }
        [data-testid="stSidebar"] .stTextInput:first-of-type input{ color:#000 !important; caret-color:#000 !important }

        /* 影院级居中主区：限制内容宽度至视口 60% */
        .block-container{ max-width:60vw; margin: 0 auto; border-radius:16px;
          background: linear-gradient(180deg, rgba(255,255,255,.25), rgba(255,255,255,0) 10%), var(--panel);
          box-shadow: inset 0 0 0 1px rgba(0,180,200,.15), 0 18px 48px rgba(0,0,0,.08);
          backdrop-filter: saturate(1.08) blur(6px);
        }

        /* 轨道式按钮（光束） */
        .stButton>button{
          background: linear-gradient(90deg, rgba(0,180,200,.25), rgba(255,167,38,.20));
          color: var(--text); border:none; border-radius:999px; padding:10px 18px;
          box-shadow: inset 0 0 0 1px rgba(0,180,200,.18), 0 2px 8px rgba(0,0,0,.06);
          transition: all .15s ease;
        }
        .stButton>button:hover{ transform: translateY(-1px); box-shadow: inset 0 0 0 1px rgba(0,180,200,.25), 0 4px 12px rgba(0,0,0,.10) }
        .stButton>button:active{ box-shadow: inset 0 0 0 1px rgba(0,180,200,.30), 0 0 18px rgba(0,180,200,.25) }

        /* 表单输入（浅色雾面） */
        .stTextInput>div>div, .stTextArea>div>div, .stSelectbox>div>div, .stMultiSelect>div>div{
          background: rgba(255,255,255,.80) !important; border:none !important; border-radius:12px !important;
          box-shadow: inset 0 0 0 1px rgba(0,180,200,.15) !important; color: var(--text) !important;
        }
        .stTextInput>div>div>input, .stTextArea textarea{ background: transparent !important; color: var(--text) !important }

        /* 题项伪滑动（每屏一个选项，滚动切换） */
        .stRadio [role="radiogroup"], .stMultiSelect>div>div{ max-height:62vh; overflow-y:auto; scroll-snap-type: y mandatory }
        .stRadio [role="radio"]{ scroll-snap-align:start }

        /* 单选：选中后脉冲光晕 */
        @keyframes pulseGlow { 0%{ box-shadow:0 0 0 0 rgba(0,180,200,.35) } 100%{ box-shadow:0 0 0 16px rgba(0,180,200,0) } }
        .stRadio [role="radio"][aria-checked="true"]{ animation: pulseGlow 0.6s ease-out 1; border-radius:12px }

        /* 多选：选中项 tick 电弧 */
        @keyframes tickArc { 0%{ background-position:0% 100% } 100%{ background-position:100% 0% } }
        [data-baseweb="tag"]{ position:relative }
        [data-baseweb="tag"]:after{ content:""; position:absolute; inset:-1px; border-radius:8px;
          background: conic-gradient(from 0deg, var(--ok), transparent 25%, transparent 100%);
          opacity:.6; mix-blend: multiply; pointer-events:none; animation: tickArc .6s ease-out 1 }

        /* 填空/开放题：聚焦电流行 */
        @keyframes electricLine { 0%{ background-position:0 100% } 100%{ background-position:100% 100% } }
        .stTextArea>div>div{ background-image: linear-gradient(90deg, rgba(0,180,200,.35) 0%, rgba(0,180,200,0) 40%, rgba(0,180,200,.35) 80%);
          background-size: 200% 2px; background-repeat: no-repeat; background-position: 0 100%; }
        .stTextArea textarea:focus{ outline:none }
        .stTextArea:focus-within>div>div{ animation: electricLine 1.2s linear infinite }

        /* 错题回顾：glitch 风格标题（若出现） */
        @keyframes glitch { 0%{ transform: translate(0) } 20%{ transform: translate(-1px,1px) } 40%{ transform: translate(1px,-1px) } 60%{ transform: translate(-1px,-1px) } 80%{ transform: translate(1px,1px) } 100%{ transform: translate(0) } }
        .sectionTitle.glitch{ position:relative; animation: glitch 0.7s steps(2,end) 2 }

        /* 大表格容器高度与滚动 */
        .stDataFrame{ max-height:400px; overflow:auto }
        .stDataFrame [data-testid="stTable"][role="table"]{ color: var(--text) }
        .stDataFrame thead tr th{ background: rgba(255,255,255,.90) }
        .stDataFrame tbody tr td{ background: rgba(255,255,255,.85) }

        /* 避免与全息仪表盘重复：默认隐藏 st.metric，仅在总结卡片内显示 */
        [data-testid="stMetric"]{ display:none !important }
        .sumCard [data-testid="stMetric"]{ display:flex !important }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_holo_header() -> None:
    try:
        total_items = int(st.session_state.get("items") and len(st.session_state.get("items")) or 0)
    except Exception:
        total_items = 0
    current_idx = int(st.session_state.get("item_idx", 0)) + (1 if total_items else 0)
    answered = int(st.session_state.get("answered_count", 0))
    correct = int(st.session_state.get("correct_count", 0))
    acc = (correct / answered) if answered else 0.0
    ability = float(st.session_state.get("ability", 0.0))
    st.markdown(
        f"""
        <div class='holo-header'>
          <div class='holo-grid'>
            <div class='holo-tile'>
              <div class='holo-label'>Current</div>
              <div class='holo-led'>{current_idx}/{total_items}</div>
            </div>
            <div class='holo-tile'>
              <div class='holo-label'>Accuracy</div>
              <div class='holo-led cyan'>{acc:.0%}</div>
            </div>
            <div class='holo-tile'>
              <div class='holo-label'>Ability</div>
              <div class='holo-led green'>{ability:.2f}</div>
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    # 先设置页面配置，必须是首个 Streamlit 调用
    st.set_page_config(page_title="Adaptive Power Knowledge Learning (JSONL QA)", layout="wide")
    # 注入视觉层 + 顶部仪表盘；业务主流程沿用 app.py
    inject_holo_light_theme()
    render_holo_header()
    # 避免在 app.py 中二次调用 set_page_config 触发异常：暂时替换为 no-op
    _orig_set_page_config = st.set_page_config
    try:
        st.set_page_config = lambda *args, **kwargs: None  # type: ignore
        base_main()
    finally:
        st.set_page_config = _orig_set_page_config


if __name__ == "__main__":
    main()


