from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st


ROOT = Path(__file__).resolve().parent
SCANNER = ROOT / "scanner"
DFA_PATH = ROOT / "main_grammar_lexeme.dfa"
FLEX_PATH = ROOT / "scanner.l"
TEST_PATH = ROOT / "test.c"
TOKENS_DFA_PATH = ROOT / "tokens.txt"
TOKENS_FLEX_PATH = ROOT / "tokens-flex.txt"

KEYWORD_TYPES = {"INT", "FLOAT", "VOID", "IF", "ELSE", "WHILE", "RETURN", "INPUT", "PRINT", "NIL"}
LITERAL_TYPES = {"ID", "NUM", "FLO"}
OPERATOR_TYPES = {"ADD", "SUB", "MUL", "DIV", "LT", "LE", "GT", "GE", "ASG", "EQ", "NE", "AND", "OR", "NOT", "AMP", "AAS", "AAA"}
DELIMITER_TYPES = {"LPA", "RPA", "LBK", "RBK", "LBR", "RBR", "CMA", "SCO"}

CATEGORY_LABELS = {
    "keyword": "关键字",
    "literal": "标识符 / 常量",
    "operator": "运算符",
    "delimiter": "界符",
    "skip": "空白",
    "error": "错误处理",
    "other": "其他",
}


def classify_token_category(token_type: str) -> str:
    if token_type in KEYWORD_TYPES:
        return "keyword"
    if token_type in LITERAL_TYPES:
        return "literal"
    if token_type in OPERATOR_TYPES:
        return "operator"
    if token_type in DELIMITER_TYPES:
        return "delimiter"
    return "other"


def classify_rule_group(token_type: str) -> str:
    if token_type == "SKIP":
        return "skip"
    if token_type == "ERROR":
        return "error"
    return classify_token_category(token_type)


def display_symbol(symbol: str) -> str:
    mapping = {" ": "space", "\n": "\\n", "\t": "\\t", "\r": "\\r"}
    return mapping.get(symbol, symbol)


@st.cache_data(show_spinner=False)
def read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def parse_token_output(raw_output: str, engine: str) -> list[dict[str, object]]:
    tokens: list[dict[str, object]] = []
    pattern = re.compile(r"^\(([^,]+),\s*(.*)\)$")
    for index, raw_line in enumerate(raw_output.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        match = pattern.match(line)
        if not match:
            continue
        token_type = match.group(1).strip()
        lexeme = match.group(2)
        tokens.append(
            {
                "index": index,
                "engine": engine.upper(),
                "token_type": token_type,
                "lexeme": lexeme,
                "category": CATEGORY_LABELS[classify_token_category(token_type)],
                "length": len(lexeme),
            }
        )
    return tokens


def normalize_tokens(tokens: list[dict[str, object]]) -> list[tuple[object, ...]]:
    return [(token["index"], token["token_type"], token["lexeme"], token["category"], token["length"]) for token in tokens]


@st.cache_data(show_spinner=False)
def parse_dfa(path: str) -> dict[str, object]:
    alphabet: list[str] = []
    states: list[str] = []
    accept_states: list[str] = []
    start_state = ""
    transitions: list[dict[str, str]] = []
    in_transitions = False

    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue

        if in_transitions:
            parts = line.split()
            if len(parts) != 3:
                continue
            source, symbol_token, target = parts
            symbol = "," if symbol_token == "comma" else symbol_token
            transitions.append({"source": source, "symbol": symbol, "target": target})
            continue

        key, value = [item.strip() for item in line.split(":", 1)]
        if key == "alphabet":
            alphabet = [item.strip() for item in value.split(",") if item.strip()]
        elif key == "states":
            states = [item.strip() for item in value.split(",") if item.strip()]
        elif key == "start":
            start_state = value
        elif key == "accept":
            accept_states = [item.strip() for item in value.split(",") if item.strip()]
        elif key == "transitions":
            in_transitions = True

    transition_map = {(item["source"], item["symbol"]): item["target"] for item in transitions}
    return {
        "alphabet": alphabet,
        "states": states,
        "start_state": start_state,
        "accept_states": accept_states,
        "transitions": transitions,
        "transition_map": transition_map,
    }


@st.cache_data(show_spinner=False)
def parse_flex_rules(path: str) -> list[dict[str, str]]:
    content = Path(path).read_text(encoding="utf-8")
    sections = content.split("%%")
    if len(sections) < 3:
        return []

    rules: list[dict[str, str]] = []
    rule_block = sections[1]
    for raw_line in rule_block.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        pattern = ""
        token_type = ""

        token_match = re.search(r'emit_token\("([^"]+)"\)', stripped)
        if token_match:
            token_type = token_match.group(1)
            pattern = stripped.split("{", 1)[0].rstrip()
        elif "advance_position" in stripped:
            token_type = "SKIP"
            pattern = stripped.split("{", 1)[0].rstrip()
        elif "emit_error" in stripped:
            token_type = "ERROR"
            pattern = stripped.split("{", 1)[0].rstrip()

        if token_type and pattern:
            rules.append(
                {
                    "pattern": pattern,
                    "token_type": token_type,
                    "group": CATEGORY_LABELS[classify_rule_group(token_type)],
                }
            )

    return rules


@st.cache_data(show_spinner=False)
def run_scanner(engine: str, source_code: str) -> dict[str, object]:
    command = [str(SCANNER), "--engine", engine, "--text", source_code]
    if engine == "dfa":
        command.extend(["--dfa", str(DFA_PATH)])

    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "未找到 scanner 可执行文件，请先执行 make。",
            "tokens": [],
        }

    return {
        "ok": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "tokens": parse_token_output(result.stdout, engine),
    }


def build_category_frame(tokens: list[dict[str, object]]) -> pd.DataFrame:
    if not tokens:
        return pd.DataFrame(columns=["category", "count"])
    frame = pd.DataFrame(tokens)
    grouped = frame.groupby("category", as_index=False).size().rename(columns={"size": "count"})
    return grouped.sort_values("count", ascending=False)


def build_token_frequency_frame(tokens: list[dict[str, object]], limit: int = 10) -> pd.DataFrame:
    if not tokens:
        return pd.DataFrame(columns=["token_type", "count"])
    counts = Counter(token["token_type"] for token in tokens)
    top_items = counts.most_common(limit)
    return pd.DataFrame(top_items, columns=["token_type", "count"])


def build_rule_group_frame(rules: list[dict[str, str]]) -> pd.DataFrame:
    if not rules:
        return pd.DataFrame(columns=["group", "count"])
    frame = pd.DataFrame(rules)
    grouped = frame.groupby("group", as_index=False).size().rename(columns={"size": "count"})
    return grouped.sort_values("count", ascending=False)


def build_rule_token_map(rules: list[dict[str, str]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for rule in rules:
        token_type = rule["token_type"]
        if token_type in {"SKIP", "ERROR"} or token_type in mapping:
            continue
        mapping[token_type] = rule["pattern"]
    return mapping


def build_rule_hit_frame(tokens: list[dict[str, object]], rules: list[dict[str, str]]) -> pd.DataFrame:
    if not tokens:
        return pd.DataFrame(columns=["pattern", "token_type", "hits"])
    rule_token_map = build_rule_token_map(rules)
    hits = Counter(token["token_type"] for token in tokens)
    rows = []
    for token_type, count in hits.items():
        rows.append(
            {
                "pattern": rule_token_map.get(token_type, "(由规则合并生成)"),
                "token_type": token_type,
                "hits": count,
            }
        )
    frame = pd.DataFrame(rows)
    return frame.sort_values("hits", ascending=False).head(12)


def build_state_degree_frame(dfa: dict[str, object], limit: int = 12) -> pd.DataFrame:
    counts = Counter(item["source"] for item in dfa["transitions"])
    rows = [{"state": state, "degree": degree} for state, degree in counts.most_common(limit)]
    return pd.DataFrame(rows)


def build_sankey(dfa: dict[str, object]) -> go.Figure:
    counts = Counter((item["source"], item["target"]) for item in dfa["transitions"] if item["source"] != "q_dead")
    top_edges = counts.most_common(18)
    nodes = sorted({dfa["start_state"], *dfa["accept_states"][:6], *(src for (src, _), _ in top_edges), *(dst for (_, dst), _ in top_edges)})
    index_map = {name: idx for idx, name in enumerate(nodes)}

    labels = nodes

    figure = go.Figure(
        go.Sankey(
            arrangement="snap",
            node={
                "label": labels,
                "pad": 24,
                "thickness": 24,
                "line": {"color": "rgba(15,23,42,0.28)", "width": 1.2},
                "color": ["#cbd5e1" if name == dfa["start_state"] else "#99f6e4" if name in dfa["accept_states"] else "#bfdbfe" for name in nodes],
            },
            link={
                "source": [index_map[source] for (source, _), _ in top_edges],
                "target": [index_map[target] for (_, target), _ in top_edges],
                "value": [value for _, value in top_edges],
                "color": ["rgba(14,116,144,0.22)"] * len(top_edges),
            },
        )
    )
    figure.update_layout(
        margin={"l": 8, "r": 8, "t": 10, "b": 10},
        paper_bgcolor="rgba(255,255,255,0.88)",
        plot_bgcolor="rgba(255,255,255,0.88)",
        font={"family": "Segoe UI, Arial, sans-serif", "size": 15, "color": "#0f172a"},
        height=420,
    )
    return figure


def simulate_dfa_path(dfa: dict[str, object], lexeme: str) -> tuple[list[dict[str, str]], bool]:
    state = dfa["start_state"]
    path = [{"step": "0", "symbol": "start", "source": "∅", "target": state, "status": "start"}]
    transition_map = dfa["transition_map"]

    for index, symbol in enumerate(lexeme, start=1):
        next_state = transition_map.get((state, symbol))
        if next_state is None:
            path.append(
                {
                    "step": str(index),
                    "symbol": display_symbol(symbol),
                    "source": state,
                    "target": "reject",
                    "status": "reject",
                }
            )
            return path, False
        status = "accept" if next_state in dfa["accept_states"] else "live"
        path.append(
            {
                "step": str(index),
                "symbol": display_symbol(symbol),
                "source": state,
                "target": next_state,
                "status": status,
            }
        )
        state = next_state

    return path, state in dfa["accept_states"]


def render_path_cards(path: list[dict[str, str]]) -> None:
    palette = {
        "start": "#dbeafe",
        "live": "#e0f2fe",
        "accept": "#dcfce7",
        "reject": "#fee2e2",
    }
    cards = []
    for item in path:
        cards.append(
            f"""
            <div class="path-card" style="background:{palette[item['status']]};">
                <div class="path-step">#{item['step']}</div>
                <div class="path-symbol">{item['symbol']}</div>
                <div class="path-state">{item['source']} → {item['target']}</div>
            </div>
            """
        )
    st.html(f"<div class='path-row'>{''.join(cards)}</div>")


def metric_card(title: str, value: str, note: str, accent: str) -> None:
    st.markdown(
        f"""
        <div class="metric-card" style="border-top:3px solid {accent};">
            <div class="metric-title">{title}</div>
            <div class="metric-value">{value}</div>
            <div class="metric-note">{note}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def hero_card(title: str, value: str, detail: str) -> None:
    st.markdown(
        f"""
        <div class="hero-card">
            <div class="hero-title">{title}</div>
            <div class="hero-value">{value}</div>
            <div class="hero-detail">{detail}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_hero_panel(cards: list[tuple[str, str, str]]) -> None:
    card_html = []
    for title, value, detail in cards:
        card_html.append(
            f"""
            <div class="hero-card">
                <div class="hero-title">{title}</div>
                <div class="hero-value">{value}</div>
                <div class="hero-detail">{detail}</div>
            </div>
            """
        )

    st.html(
        f"""
        <div class="hero-panel">
            <h1 style="margin:0;">词法分析系统可视化展示</h1>
            <p style="margin:0.55rem 0 0 0; color:#4b5563; max-width:860px; line-height:1.7;">
                面向课程报告与答辩场景，围绕同一份主文法输入，分别展示
                <strong>DFA 状态驱动</strong> 与 <strong>正则表达式规则驱动</strong>
                两种词法分析实现的结构特征、命中结果与一致性。
            </p>
            <div class="hero-grid">
                {''.join(card_html)}
            </div>
        </div>
        """
    )


def inject_styles() -> None:
    st.markdown(
        """
        <style>
        .stApp {
            background:
                radial-gradient(circle at top left, rgba(15,118,110,0.10), transparent 28%),
                radial-gradient(circle at top right, rgba(180,83,9,0.10), transparent 24%),
                linear-gradient(180deg, #f5f1e8 0%, #f8f5ef 42%, #f7f4ee 100%);
            color: #10212b;
            font-family: "Avenir Next", "Segoe UI", sans-serif;
        }
        .block-container {
            max-width: 1240px;
            padding-top: 2.1rem;
            padding-bottom: 3rem;
        }
        h1, h2, h3 {
            font-family: Georgia, "Times New Roman", serif !important;
            letter-spacing: -0.02em;
            color: #111827;
        }
        .eyebrow {
            display: inline-block;
            padding: 0.32rem 0.7rem;
            border: 1px solid rgba(15, 23, 42, 0.12);
            border-radius: 999px;
            background: rgba(255,255,255,0.58);
            color: #0f766e;
            font-size: 0.78rem;
            letter-spacing: 0.08em;
            text-transform: uppercase;
        }
        .hero-panel {
            margin-top: 0.8rem;
            margin-bottom: 1.3rem;
            padding: 1.4rem 1.5rem 1.2rem 1.5rem;
            border: 1px solid rgba(15, 23, 42, 0.08);
            border-radius: 24px;
            background: rgba(255,255,255,0.72);
            box-shadow: 0 20px 45px rgba(15, 23, 42, 0.06);
            backdrop-filter: blur(12px);
        }
        .hero-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 0.9rem;
            margin-top: 1rem;
        }
        .hero-card {
            padding: 1rem 1.05rem;
            border-radius: 18px;
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(245,247,250,0.96));
            border: 1px solid rgba(15, 23, 42, 0.06);
        }
        .hero-title {
            font-size: 0.82rem;
            color: #52606d;
            margin-bottom: 0.3rem;
        }
        .hero-value {
            font-size: 1.9rem;
            font-weight: 700;
            color: #111827;
            line-height: 1.1;
        }
        .hero-detail {
            margin-top: 0.28rem;
            font-size: 0.88rem;
            color: #5b6776;
        }
        .section-shell {
            border: 1px solid rgba(15, 23, 42, 0.08);
            border-radius: 24px;
            padding: 1.2rem;
            background: rgba(255,255,255,0.70);
            box-shadow: 0 16px 40px rgba(15, 23, 42, 0.05);
        }
        .method-dfa {
            background:
                linear-gradient(180deg, rgba(9,29,45,0.96), rgba(8,47,73,0.94)),
                linear-gradient(135deg, #082f49, #0f766e);
            color: #ecfeff;
        }
        .method-regex {
            background:
                linear-gradient(180deg, rgba(255,250,240,0.98), rgba(254,243,224,0.94)),
                linear-gradient(135deg, #fffbeb, #fed7aa);
            color: #431407;
        }
        .method-title {
            font-size: 0.78rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            opacity: 0.82;
        }
        .method-name {
            font-size: 1.7rem;
            font-family: Georgia, "Times New Roman", serif;
            margin-top: 0.2rem;
            margin-bottom: 0.4rem;
        }
        .method-note {
            font-size: 0.95rem;
            line-height: 1.55;
            opacity: 0.92;
        }
        .metric-card {
            min-height: 120px;
            padding: 1rem;
            border-radius: 18px;
            background: rgba(255,255,255,0.84);
            border: 1px solid rgba(15,23,42,0.06);
            box-shadow: 0 12px 24px rgba(15,23,42,0.04);
        }
        .metric-title {
            color: #607080;
            font-size: 0.83rem;
        }
        .metric-value {
            margin-top: 0.3rem;
            font-size: 1.7rem;
            font-weight: 700;
            color: #111827;
        }
        .metric-note {
            margin-top: 0.28rem;
            color: #5d6a77;
            font-size: 0.88rem;
            line-height: 1.45;
        }
        .token-pill {
            display: inline-flex;
            align-items: center;
            margin: 0 0.48rem 0.48rem 0;
            padding: 0.5rem 0.75rem;
            border-radius: 999px;
            background: rgba(255,255,255,0.72);
            border: 1px solid rgba(15,23,42,0.08);
            font-size: 0.86rem;
            gap: 0.4rem;
        }
        .token-pill strong {
            font-family: "SFMono-Regular", Consolas, monospace;
        }
        .rule-card {
            padding: 0.92rem 1rem;
            border-radius: 18px;
            background: rgba(255,255,255,0.70);
            border: 1px solid rgba(67,20,7,0.08);
            margin-bottom: 0.75rem;
        }
        .rule-pattern {
            font-family: "SFMono-Regular", Consolas, monospace;
            font-size: 0.9rem;
            color: #7c2d12;
            word-break: break-all;
        }
        .rule-meta {
            margin-top: 0.45rem;
            color: #6b3b1f;
            font-size: 0.84rem;
        }
        .path-row {
            display: flex;
            flex-wrap: wrap;
            gap: 0.7rem;
            margin-top: 0.5rem;
        }
        .path-card {
            min-width: 150px;
            padding: 0.8rem 0.9rem;
            border-radius: 16px;
            border: 1px solid rgba(15, 23, 42, 0.08);
            box-shadow: 0 10px 20px rgba(15, 23, 42, 0.04);
        }
        .path-step {
            font-size: 0.75rem;
            color: #5b6776;
        }
        .path-symbol {
            margin-top: 0.25rem;
            font-weight: 700;
            font-family: "SFMono-Regular", Consolas, monospace;
            color: #111827;
        }
        .path-state {
            margin-top: 0.3rem;
            color: #334155;
            font-size: 0.88rem;
        }
        .compare-banner {
            padding: 0.95rem 1rem;
            border-radius: 18px;
            border: 1px solid rgba(15,23,42,0.08);
            background: rgba(255,255,255,0.78);
            margin-bottom: 1rem;
        }
        .sidebar-note {
            font-size: 0.85rem;
            color: #64748b;
            line-height: 1.5;
        }
        @media (max-width: 900px) {
            .hero-grid {
                grid-template-columns: repeat(2, minmax(0, 1fr));
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


st.set_page_config(
    page_title="词法分析可视化展示",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
)

inject_styles()

dfa = parse_dfa(str(DFA_PATH))
flex_rules = parse_flex_rules(str(FLEX_PATH))
sample_code = read_text(str(TEST_PATH)).strip()
sample_tokens_dfa = parse_token_output(read_text(str(TOKENS_DFA_PATH)), "dfa") if TOKENS_DFA_PATH.exists() else []
sample_tokens_flex = parse_token_output(read_text(str(TOKENS_FLEX_PATH)), "flex") if TOKENS_FLEX_PATH.exists() else []

with st.sidebar:
    st.markdown("### 演示输入")
    source_mode = st.radio("代码来源", ["实验样例 test.c", "自定义输入"], label_visibility="collapsed")
    default_source = sample_code if source_mode == "实验样例 test.c" else "int value;\nvalue = +12;\nif (value >= 10) { value += 1; }"
    source_code = st.text_area("输入待分析代码", value=default_source, height=260)
    st.markdown("<div class='sidebar-note'>页面会实时调用仓库中的 <code>./scanner</code>，分别展示 DFA 与正则规则两种词法分析路径。</div>", unsafe_allow_html=True)
    st.markdown("---")
    preview_lexeme = st.text_input("DFA 路径演示词素", value="count")
    st.caption("建议输入 `count`、`12.5`、`+=`、`if` 这类单个词素。")

dfa_run = run_scanner("dfa", source_code)
flex_run = run_scanner("flex", source_code)
dfa_tokens = dfa_run["tokens"]
flex_tokens = flex_run["tokens"]
shared_tokens = dfa_tokens if dfa_tokens else sample_tokens_dfa
normalized_dfa = normalize_tokens(dfa_tokens)
normalized_flex = normalize_tokens(flex_tokens)
token_match = normalized_dfa == normalized_flex and bool(normalized_dfa)
match_ratio = 100 if token_match else round(sum(a == b for a, b in zip(normalized_dfa, normalized_flex)) / max(len(normalized_dfa), len(normalized_flex), 1) * 100)

st.markdown("<span class='eyebrow'>Compiler Design Lab 2</span>", unsafe_allow_html=True)
render_hero_panel(
    [
        ("当前 token 总数", str(len(dfa_tokens)), "基于当前输入代码实时计算"),
        ("输出一致性", f"{match_ratio}%", "DFA 与 flex 结果对齐程度"),
        ("DFA 状态规模", str(len(dfa["states"])), f"其中接受态 {len(dfa['accept_states'])} 个"),
        ("Regex 规则数", str(len(flex_rules)), "来自 scanner.l 的实际规则"),
    ]
)

overview_tab, dfa_tab, regex_tab, compare_tab = st.tabs(["总览", "DFA 视角", "正则视角", "结果对比"])

with overview_tab:
    left, right = st.columns([1.08, 0.92], gap="large")
    with left:
        st.markdown(
            """
            <div class="section-shell method-dfa">
                <div class="method-title">Method A</div>
                <div class="method-name">DFA 状态机驱动</div>
                <div class="method-note">
                    以状态转移为核心，通过最长匹配与接受态判定完成词素识别。
                    这一块更强调结构化、确定性和工程可验证性。
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        category_frame = build_category_frame(dfa_tokens)
        if not category_frame.empty:
            fig = px.bar(
                category_frame,
                x="count",
                y="category",
                orientation="h",
                color="count",
                color_continuous_scale=["#dbeafe", "#0f766e"],
            )
            fig.update_layout(
                title="DFA 输出的词法类别分布",
                height=320,
                margin={"l": 10, "r": 10, "t": 50, "b": 10},
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                coloraxis_showscale=False,
                font={"family": "Avenir Next, Segoe UI, sans-serif", "color": "#0f172a"},
            )
            fig.update_yaxes(title=None)
            fig.update_xaxes(title=None, showgrid=False)
            st.plotly_chart(fig, use_container_width=True)
        st.markdown("#### 当前输入")
        st.code(source_code, language="c")

    with right:
        st.markdown(
            """
            <div class="section-shell method-regex">
                <div class="method-title">Method B</div>
                <div class="method-name">正则表达式规则驱动</div>
                <div class="method-note">
                    以模式描述和规则优先级为核心，通过 flex 的正则匹配快速归类。
                    这一块更强调规则栈、表达力和实现紧凑性。
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        rule_group_frame = build_rule_group_frame(flex_rules)
        if not rule_group_frame.empty:
            fig = px.pie(
                rule_group_frame,
                names="group",
                values="count",
                hole=0.58,
                color="group",
                color_discrete_sequence=["#b45309", "#d97706", "#f59e0b", "#fb923c", "#fcd34d", "#9a3412"],
            )
            fig.update_layout(
                title="scanner.l 规则构成",
                height=320,
                margin={"l": 10, "r": 10, "t": 50, "b": 10},
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                showlegend=True,
                font={"family": "Avenir Next, Segoe UI, sans-serif", "color": "#431407"},
            )
            st.plotly_chart(fig, use_container_width=True)

        st.markdown("#### 代表性输出 token")
        pill_html = []
        for token in shared_tokens[:12]:
            pill_html.append(f"<span class='token-pill'><strong>{token['token_type']}</strong> {token['lexeme']}</span>")
        st.markdown("".join(pill_html), unsafe_allow_html=True)

with dfa_tab:
    top_left, top_right = st.columns([0.9, 1.1], gap="large")
    with top_left:
        metric_cols = st.columns(3)
        with metric_cols[0]:
            metric_card("状态总数", str(len(dfa["states"])), "完整状态机规模", "#0f766e")
        with metric_cols[1]:
            metric_card("接受态", str(len(dfa["accept_states"])), "可直接形成合法词法单元", "#0891b2")
        with metric_cols[2]:
            metric_card("转移边", str(len(dfa["transitions"])), "来自 DFA 规则文件", "#164e63")

        state_degree_frame = build_state_degree_frame(dfa)
        if not state_degree_frame.empty:
            fig = px.bar(
                state_degree_frame,
                x="state",
                y="degree",
                color="degree",
                color_continuous_scale=["#d1fae5", "#0f766e"],
            )
            fig.update_layout(
                title="高连接度状态",
                height=340,
                margin={"l": 10, "r": 10, "t": 50, "b": 10},
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                coloraxis_showscale=False,
                font={"family": "Avenir Next, Segoe UI, sans-serif"},
            )
            fig.update_xaxes(title=None)
            fig.update_yaxes(title=None, showgrid=True, gridcolor="rgba(15,118,110,0.10)")
            st.plotly_chart(fig, use_container_width=True)

    with top_right:
        st.markdown("#### 状态流转概览")
        st.plotly_chart(build_sankey(dfa), use_container_width=True)

    st.markdown("#### 词素路径演示")
    path, accepted = simulate_dfa_path(dfa, preview_lexeme)
    st.caption(f"当前演示词素：`{preview_lexeme}`，最终判定：{'接受' if accepted else '拒绝'}")
    render_path_cards(path)

with regex_tab:
    regex_left, regex_right = st.columns([1.05, 0.95], gap="large")
    with regex_left:
        metric_cols = st.columns(3)
        with metric_cols[0]:
            metric_card("规则总数", str(len(flex_rules)), "抽取自 scanner.l 的有效模式", "#b45309")
        with metric_cols[1]:
            metric_card("规则分组", str(len({rule['group'] for rule in flex_rules})), "按语义类别归并展示", "#d97706")
        with metric_cols[2]:
            metric_card("样例命中", str(len(flex_tokens)), "当前输入下的规则触发结果", "#f59e0b")

        hit_frame = build_rule_hit_frame(flex_tokens, flex_rules)
        if not hit_frame.empty:
            fig = px.bar(
                hit_frame,
                x="hits",
                y="token_type",
                color="hits",
                hover_data=["pattern"],
                orientation="h",
                color_continuous_scale=["#ffedd5", "#c2410c"],
            )
            fig.update_layout(
                title="高频规则命中",
                height=360,
                margin={"l": 10, "r": 10, "t": 50, "b": 10},
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                coloraxis_showscale=False,
                font={"family": "Avenir Next, Segoe UI, sans-serif"},
            )
            fig.update_xaxes(title=None, showgrid=False)
            fig.update_yaxes(title=None)
            st.plotly_chart(fig, use_container_width=True)

    with regex_right:
        st.markdown("#### 核心正则规则")
        for rule in flex_rules[:8]:
            st.markdown(
                f"""
                <div class="rule-card">
                    <div class="rule-pattern">{rule['pattern']}</div>
                    <div class="rule-meta">输出 token: <strong>{rule['token_type']}</strong> · 规则类别: {rule['group']}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

with compare_tab:
    st.markdown(
        f"""
        <div class="compare-banner">
            <strong>一致性结论：</strong>
            当前输入下两种实现的 token 序列{'完全一致' if token_match else '存在差异'}，
            对齐率为 <strong>{match_ratio}%</strong>。
        </div>
        """,
        unsafe_allow_html=True,
    )

    compare_left, compare_right = st.columns(2, gap="large")
    with compare_left:
        st.markdown("#### DFA 输出")
        st.dataframe(pd.DataFrame(dfa_tokens), use_container_width=True, hide_index=True)
    with compare_right:
        st.markdown("#### Regex / flex 输出")
        st.dataframe(pd.DataFrame(flex_tokens), use_container_width=True, hide_index=True)

    if not dfa_run["ok"] or not flex_run["ok"]:
        st.warning("至少有一个引擎未成功返回结果，请检查 scanner 可执行文件是否已构建。")
        if dfa_run["stderr"]:
            st.code(dfa_run["stderr"])
        if flex_run["stderr"]:
            st.code(flex_run["stderr"])

st.markdown("---")
st.caption(
    "运行方式：`./.venv/bin/streamlit run streamlit_app.py`。页面展示数据来自 `main_grammar_lexeme.dfa`、`scanner.l`、`test.c` 与实时 scanner 输出。"
)
