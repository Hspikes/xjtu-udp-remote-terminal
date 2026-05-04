#!/usr/bin/env python3
"""Streamlit monitor UI for the UDP remote terminal server."""

from __future__ import annotations

import argparse
from datetime import datetime
from html import escape
import json
import os
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import streamlit as st


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UDP terminal Streamlit monitor")
    parser.add_argument("--api", default="http://127.0.0.1:9100", help="monitor API base URL")
    parser.add_argument(
        "--token",
        default=os.environ.get("UDPTERM_MONITOR_TOKEN"),
        help="optional monitor API bearer token",
    )
    args, _unknown = parser.parse_known_args(argv)
    return args


def format_time(timestamp: float | int | None) -> str:
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M:%S")


def format_size(size: int | float | None) -> str:
    if size is None:
        return "-"
    value = float(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def inject_styles() -> None:
    st.markdown(
        """
        <style>
        .stApp {
            background: linear-gradient(180deg, #f6f8fb 0%, #eef3f7 48%, #f8fafc 100%);
            color: #111827;
            font-family: "Inter", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
        }
        .block-container {
            max-width: 1260px;
            padding-top: 1.6rem;
            padding-bottom: 2.6rem;
        }
        h1, h2, h3, [data-testid="stMarkdownContainer"] h1,
        [data-testid="stMarkdownContainer"] h2,
        [data-testid="stMarkdownContainer"] h3 {
            color: #0f172a;
            letter-spacing: 0;
        }
        [data-testid="stSidebar"] {
            background: #0f172a;
            border-right: 1px solid rgba(148, 163, 184, 0.18);
        }
        [data-testid="stSidebar"] * {
            color: #e5edf6;
        }
        [data-testid="stSidebar"] input,
        [data-testid="stSidebar"] textarea,
        [data-testid="stSidebar"] select {
            color: #0f172a;
        }
        [data-testid="stSidebar"] [data-baseweb="select"] > div {
            background: #ffffff;
        }
        [data-testid="stSidebar"] [data-baseweb="select"] * {
            color: #0f172a !important;
        }
        [data-testid="stSidebar"] .stButton button {
            border-radius: 8px;
            border: 1px solid rgba(125, 211, 252, 0.35);
            background: #0ea5a4;
            color: #ffffff;
            font-weight: 650;
        }
        [data-testid="stSidebar"] .stButton button:hover {
            border-color: rgba(255, 255, 255, 0.55);
            background: #0f766e;
            color: #ffffff;
        }
        .topline {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            margin-bottom: 0.8rem;
        }
        .eyebrow {
            display: inline-flex;
            align-items: center;
            min-height: 28px;
            padding: 0.24rem 0.66rem;
            border: 1px solid rgba(15, 118, 110, 0.18);
            border-radius: 999px;
            background: #ecfeff;
            color: #0f766e;
            font-size: 0.76rem;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
        }
        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 0.45rem;
            padding: 0.36rem 0.72rem;
            border-radius: 999px;
            border: 1px solid rgba(15, 23, 42, 0.10);
            background: rgba(255, 255, 255, 0.72);
            color: #334155;
            font-size: 0.82rem;
            font-weight: 650;
        }
        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 99px;
            background: #94a3b8;
        }
        .status-live .status-dot {
            background: #14b8a6;
            box-shadow: 0 0 0 4px rgba(20, 184, 166, 0.14);
        }
        .hero-panel {
            margin: 0.25rem 0 1.25rem 0;
            padding: 1.35rem 1.45rem;
            border: 1px solid rgba(15, 23, 42, 0.08);
            border-radius: 8px;
            background:
                linear-gradient(135deg, rgba(255, 255, 255, 0.96), rgba(240, 253, 250, 0.86)),
                linear-gradient(90deg, #ffffff, #f1f5f9);
            box-shadow: 0 16px 36px rgba(15, 23, 42, 0.07);
        }
        .hero-title {
            margin: 0;
            color: #0f172a;
            font-size: 2.05rem;
            line-height: 1.16;
            font-weight: 800;
        }
        .hero-copy {
            max-width: 880px;
            margin: 0.55rem 0 0 0;
            color: #475569;
            line-height: 1.68;
            font-size: 0.98rem;
        }
        .hero-meta {
            display: flex;
            flex-wrap: wrap;
            gap: 0.55rem;
            margin-top: 1rem;
        }
        .meta-chip {
            display: inline-flex;
            align-items: center;
            min-height: 30px;
            padding: 0.28rem 0.7rem;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.78);
            border: 1px solid rgba(15, 23, 42, 0.08);
            color: #334155;
            font-size: 0.84rem;
        }
        .metric-card {
            min-height: 124px;
            padding: 1rem;
            border-radius: 8px;
            background: rgba(255, 255, 255, 0.88);
            border: 1px solid rgba(15, 23, 42, 0.08);
            box-shadow: 0 10px 24px rgba(15, 23, 42, 0.05);
        }
        .metric-title {
            color: #64748b;
            font-size: 0.82rem;
            font-weight: 700;
        }
        .metric-value {
            margin-top: 0.42rem;
            color: #0f172a;
            font-size: 1.72rem;
            font-weight: 800;
            line-height: 1.12;
            word-break: break-word;
        }
        .metric-note {
            margin-top: 0.38rem;
            color: #64748b;
            font-size: 0.86rem;
            line-height: 1.45;
        }
        .section-title {
            margin: 1.1rem 0 0.55rem 0;
            padding-left: 0.75rem;
            border-left: 4px solid #0f766e;
        }
        .section-title h3 {
            margin: 0;
            font-size: 1.12rem;
        }
        .section-title p {
            margin: 0.22rem 0 0 0;
            color: #64748b;
            font-size: 0.9rem;
        }
        .detail-strip {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 0.85rem;
            margin: 0.6rem 0 1rem 0;
        }
        .detail-item {
            padding: 0.85rem 0.9rem;
            border-radius: 8px;
            border: 1px solid rgba(15, 23, 42, 0.08);
            background: rgba(255, 255, 255, 0.82);
        }
        .detail-label {
            color: #64748b;
            font-size: 0.78rem;
            font-weight: 700;
        }
        .detail-value {
            margin-top: 0.32rem;
            color: #0f172a;
            font-size: 1rem;
            font-weight: 760;
            word-break: break-word;
        }
        .sidebar-note {
            margin-top: 0.75rem;
            color: #bfd4e8;
            font-size: 0.84rem;
            line-height: 1.55;
        }
        div[data-testid="stDataFrame"] {
            border: 1px solid rgba(15, 23, 42, 0.08);
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 8px 20px rgba(15, 23, 42, 0.04);
        }
        .stTabs [data-baseweb="tab-list"] {
            gap: 0.35rem;
        }
        .stTabs [data-baseweb="tab"] {
            border-radius: 8px 8px 0 0;
            padding: 0.55rem 0.95rem;
            font-weight: 650;
        }
        @media (max-width: 900px) {
            .topline {
                align-items: flex-start;
                flex-direction: column;
            }
            .detail-strip {
                grid-template-columns: repeat(2, minmax(0, 1fr));
            }
            .hero-title {
                font-size: 1.65rem;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_metric_card(title: str, value: object, note: str, accent: str) -> None:
    st.markdown(
        f"""
        <div class="metric-card" style="border-top: 3px solid {accent};">
            <div class="metric-title">{escape(title)}</div>
            <div class="metric-value">{escape(str(value))}</div>
            <div class="metric-note">{escape(note)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_section_title(title: str, note: str) -> None:
    st.markdown(
        f"""
        <div class="section-title">
            <h3>{escape(title)}</h3>
            <p>{escape(note)}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_hero(stats: dict, sessions: list[dict], api_base: str) -> None:
    summary = stats.get("summary", {})
    server = stats.get("server", {})
    server_addr = f"{server.get('host', '-') or '-'}:{server.get('port', '-')}"
    active_sessions = int(summary.get("active_sessions", 0) or 0)
    status_class = "status-live" if active_sessions else ""
    status_text = "运行中" if active_sessions else "等待连接"
    st.markdown(
        f"""
        <div class="topline">
            <span class="eyebrow">Network Lab 7</span>
            <span class="status-badge {status_class}">
                <span class="status-dot"></span>{escape(status_text)}
            </span>
        </div>
        <div class="hero-panel">
            <h1 class="hero-title">UDP Remote Terminal Monitor</h1>
            <p class="hero-copy">
                面向基于 UDP 的远程终端实验，集中展示服务运行状态、在线会话、
                用户目录与最近命令记录。页面只读取监控 API，不提供远程执行入口。
            </p>
            <div class="hero-meta">
                <span class="meta-chip">API: {escape(api_base)}</span>
                <span class="meta-chip">UDP: {escape(server_addr)}</span>
                <span class="meta-chip">更新时间: {escape(format_time(time.time()))}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def api_get(api_base: str, path: str, token: str | None = None) -> Any:
    url = api_base.rstrip("/") + path
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=3.0) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            message = payload.get("error", str(exc))
        except Exception:
            message = str(exc)
        raise RuntimeError(f"{exc.code}: {message}") from exc
    except URLError as exc:
        raise RuntimeError(f"API unreachable: {exc.reason}") from exc


def render_metrics(stats: dict, sessions: list[dict]) -> None:
    summary = stats.get("summary", {})
    server = stats.get("server", {})
    server_addr = f"{server.get('host', '-') or '-'}:{server.get('port', '-')}"
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        render_metric_card("连接数", summary.get("active_sessions", 0), "当前保持认证的终端会话", "#0f766e")
    with col2:
        render_metric_card("在线用户", summary.get("connected_users", 0), "按用户名去重统计", "#0284c7")
    with col3:
        render_metric_card("已知用户", summary.get("known_users", 0), "来自服务端用户库", "#7c3aed")
    with col4:
        render_metric_card("UDP 服务", server_addr, f"页面会话数 {len(sessions)}", "#d97706")


def render_session_table(sessions: list[dict]) -> None:
    rows = []
    for session in sessions:
        rows.append(
            {
                "client_id": session.get("client_id"),
                "用户": session.get("username"),
                "客户端地址": f"{session.get('ip')}:{session.get('udp_port')}",
                "当前目录": session.get("current_dir", "."),
                "窗口": f"{session.get('rows')}x{session.get('cols')}",
                "空闲秒数": round(float(session.get("idle_seconds", 0.0)), 1),
                "待发包": session.get("pending_packets", 0),
                "未确认包": session.get("unacked_packets", 0),
            }
        )
    st.dataframe(rows, hide_index=True, use_container_width=True)


def render_files(api_base: str, token: str | None, client_id: int, default_path: str) -> None:
    requested_path = st.text_input("文件路径", value=default_path or ".", help="路径限制在当前用户 home 目录内")
    query = urlencode({"path": requested_path})
    files = api_get(api_base, f"/api/sessions/{client_id}/files?{query}", token)
    render_section_title("文件列表", f"当前路径：{files.get('base_path', '.')}")
    entries = files.get("entries", [])
    if not entries:
        st.info("当前目录为空")
        return
    rows = [
        {
            "名称": entry.get("name", ""),
            "类型": "目录" if entry.get("type") == "directory" else "文件",
            "大小": format_size(entry.get("size")),
            "修改时间": format_time(entry.get("mtime")),
            "相对路径": entry.get("path", ""),
        }
        for entry in entries
    ]
    st.dataframe(rows, hide_index=True, use_container_width=True)


def render_commands(commands: list[dict]) -> None:
    render_section_title("最近命令", "采集自远程 shell prompt 阶段记录")
    if not commands:
        st.info("暂无命令记录")
        return
    rows = [
        {
            "时间": format_time(command.get("timestamp")),
            "目录": command.get("cwd", "."),
            "命令": command.get("command", ""),
        }
        for command in commands
    ]
    st.dataframe(rows, hide_index=True, use_container_width=True)


def render_detail_summary(detail: dict) -> None:
    values = [
        ("用户", detail.get("username", "-")),
        ("客户端地址", f"{detail.get('ip')}:{detail.get('udp_port')}"),
        ("窗口大小", f"{detail.get('rows')}x{detail.get('cols')}"),
        ("空闲时长", f"{float(detail.get('idle_seconds', 0.0)):.1f}s"),
    ]
    items = []
    for label, value in values:
        items.append(
            f"""
            <div class="detail-item">
                <div class="detail-label">{escape(label)}</div>
                <div class="detail-value">{escape(str(value))}</div>
            </div>
            """
        )
    st.html(f"<div class='detail-strip'>{''.join(items)}</div>")
    st.caption(
        f"连接时间：{format_time(detail.get('connected_at'))}；"
        f"最后活跃：{format_time(detail.get('last_seen_at'))}；"
        f"当前目录：{detail.get('current_dir', '.')}"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    st.set_page_config(
        page_title="UDP Remote Terminal Monitor",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    inject_styles()

    with st.sidebar:
        st.markdown("### 监控连接")
        api_base = st.text_input("API Endpoint", value=args.api)
        token = st.text_input("Access Token", value=args.token or "", type="password") or None
        refresh_interval = st.selectbox("刷新间隔", ["关闭", "1 秒", "2 秒", "5 秒"], index=2)
        manual_refresh = st.button("立即刷新", use_container_width=True)
        st.markdown(
            "<div class='sidebar-note'>监控页通过只读 HTTP API 获取快照，适合课堂演示、调试和答辩展示。</div>",
            unsafe_allow_html=True,
        )
        if manual_refresh:
            st.rerun()

    try:
        stats = api_get(api_base, "/api/stats", token)
        sessions = api_get(api_base, "/api/sessions", token)
    except RuntimeError as exc:
        st.error(str(exc))
        return 1

    render_hero(stats, sessions, api_base)
    render_metrics(stats, sessions)
    overview_tab, detail_tab = st.tabs(["连接总览", "会话详情"])

    with overview_tab:
        render_section_title("Session Overview", "在线连接、窗口尺寸和可靠传输队列状态")
        if sessions:
            render_session_table(sessions)
        else:
            st.info("暂无活动连接")

    with detail_tab:
        if sessions:
            client_ids = [int(session["client_id"]) for session in sessions]
            selected = st.selectbox(
                "连接详情",
                client_ids,
                format_func=lambda value: next(
                    (
                        f"{value} / {session.get('username')} / {session.get('current_dir', '.')}"
                        for session in sessions
                        if int(session["client_id"]) == int(value)
                    ),
                    str(value),
                ),
            )
            encoded_id = quote(str(selected), safe="")
            try:
                detail = api_get(api_base, f"/api/sessions/{encoded_id}", token)
            except RuntimeError as exc:
                st.error(str(exc))
                return 1

            render_detail_summary(detail)
            file_col, command_col = st.columns([3, 2], gap="large")
            with file_col:
                try:
                    render_files(api_base, token, selected, detail.get("current_dir", "."))
                except RuntimeError as exc:
                    st.error(str(exc))
            with command_col:
                render_commands(detail.get("recent_commands", []))
        else:
            st.info("暂无活动连接")

    interval_seconds = {"1 秒": 1, "2 秒": 2, "5 秒": 5}.get(refresh_interval)
    if interval_seconds:
        time.sleep(interval_seconds)
        st.rerun()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
