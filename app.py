#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信公众号 AI 助手 —— 让 DeepSeek 变成你微信里一个能聊天的朋友。
不用开电脑、不用装 App、不用担心封号，只要有网就能用。

    ┌────────┐  发消息   ┌───────────┐   POST    ┌──────────────┐
    │ 你的微信 │ ───────▶ │ 微信服务器  │ ────────▶ │ 本服务（云上） │
    │        │ ◀─────── │  公众号    │ ◀──────── │  ↓ DeepSeek  │
    └────────┘  被动回复  └───────────┘   5 秒内   └──────────────┘

零依赖，纯标准库。部署见 README.md。

环境变量：
    WX_TOKEN          公众号后台「服务器配置」里自己填的 Token
    DEEPSEEK_API_KEY  DeepSeek 的 key
    SELF_NICKNAME     它怎么称呼你（可选，默认「你」）
    DATA_DIR          待办本落盘目录（可选）
    WX_AI_SECRET      管理接口的密码（可选，设了才能用 /admin/*）
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from urllib.parse import parse_qs

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

WX_TOKEN = os.environ.get("WX_TOKEN", "change-me")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
SELF_NICKNAME = os.environ.get("SELF_NICKNAME", "你")
DATA_DIR = os.environ.get("DATA_DIR", "/tmp")
ADMIN_SECRET = os.environ.get("WX_AI_SECRET", "")

# 微信给 5 秒，留 0.9 秒做网络和序列化
LLM_BUDGET = float(os.environ.get("LLM_BUDGET", "4.1"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "220"))

# --------------------------------------------------------------------------
# 人设
# --------------------------------------------------------------------------

PERSONA = """你是「{nick}」的微信好友，也是 ta 的学习搭子。

说话方式必须是真人微信聊天的样子：
- 一次只回一两句话，最多三句。绝不长篇大论。
- 不用 markdown、不用列表、不用编号、不用加粗。就是纯文字聊天。
- 不说"好的""收到""很高兴为你服务""有什么可以帮你"这类客服话。
- 可以像真人一样用"嗯""行""那""欸""总之"起头，也可以反问一句。
- 拿不准的事直接说拿不准，别编。

你能做这些：
- 陪 ta 聊，记得住这次对话的上下文。
- ta 发来一段群通知、作业要求、考试安排，你帮他判断要不要做、最晚什么时候、还差什么。
- ta 说「待办」或者「今天有啥」时，用嘴把记下的事说出来，不要列清单。
"""

HISTORY: dict[str, list[dict]] = {}
HISTORY_LIMIT = 12              # 保留最近 6 轮

_todos: dict[str, list[dict]] = {}
_todos_loaded = False
_lock = threading.Lock()

# --------------------------------------------------------------------------
# 任务抽取（规则版，云端不依赖其他模块）
# --------------------------------------------------------------------------

RULES = [
    (re.compile(r"作业|习题|课后题|大作业|课程设计|实验报告|论文|综述|读书笔记"), 3),
    (re.compile(r"考试|测验|小测|期中|期末|补考|重修|机考|上机考"), 3),
    (re.compile(r"答辩|组会|汇报|开题|讲座|上课|补课|调课|班会"), 3),
    (re.compile(r"提交|上交|上传|发我|发给我|交到|打包发"), 3),
    (re.compile(r"ddl|DDL|deadline|DeadLine|截止|到期"), 3),
    (re.compile(r"报名|选课|退课|评教|问卷|登记|填表|签字|盖章"), 3),
    (re.compile(r"记得|别忘|务必|尽快|抓紧"), 1),
]
NEGATIVE = re.compile(r"取消|延期|推迟|改期|已交|交了|已完成|写完了|搞定了")

TIME_HM = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")
DATE_MD = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")
WEEK = re.compile(r"(本|这|下下|下)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天])")
DOW = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def quick_due(text: str, now: Optional[datetime] = None) -> Optional[str]:
    now = now or datetime.now()
    today = now.date()
    t: Optional[tuple[int, int]] = None

    m = TIME_HM.search(text)
    if m and 0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59:
        t = (int(m.group(1)), int(m.group(2)))
    else:
        m = re.search(r"(上午|下午|晚上|中午|早上)?\s*(\d{1,2})\s*点\s*(半)?", text)
        if m:
            h = int(m.group(2))
            if m.group(1) in ("下午", "晚上") and h < 12:
                h += 12
            if 0 <= h <= 23:
                t = (h, 30 if m.group(3) else 0)

    date = None
    m = DATE_MD.search(text)
    if m:
        try:
            date = datetime(today.year, int(m.group(1)), int(m.group(2))).date()
            if date < today - timedelta(days=45):
                date = date.replace(year=date.year + 1)
        except ValueError:
            date = None

    if date is None:
        m = WEEK.search(text)
        if m:
            target = DOW.get(m.group(2))
            prefix = m.group(1) or ""
            if target is not None:
                # 以「本周一」为基准，才能正确推「下周三」
                monday = today - timedelta(days=today.weekday())
                if prefix == "下":
                    date = monday + timedelta(days=7 + target)
                elif prefix == "下下":
                    date = monday + timedelta(days=14 + target)
                elif prefix in ("本", "这"):
                    date = monday + timedelta(days=target)
                else:
                    date = today + timedelta(days=(target - today.weekday()) % 7)

    if date is None:
        for word, off in (("后天", 2), ("明天", 1), ("今晚", 0), ("今天", 0)):
            if word in text:
                date = today + timedelta(days=off)
                break

    if date is None and t is None:
        return None

    if date is None:
        date = today if (t and (t[0], t[1]) >= (now.hour, now.minute)) else today + timedelta(days=1)
    hh, mm = t or (23, 59)
    return datetime(date.year, date.month, date.day, hh, mm).strftime("%Y-%m-%d %H:%M")


def maybe_task(text: str) -> Optional[dict]:
    score = sum(w for rx, w in RULES if rx.search(text))
    if NEGATIVE.search(text):
        score -= 6
    due = quick_due(text)
    if due:
        score += 3
    if score < 5:
        return None
    title = re.sub(r"^【[^】]{0,20}】\s*", "", text)
    title = re.sub(r"^@所有人\s*", "", title).strip()
    if len(title) > 24:
        title = title[:23].rstrip() + "…"
    return {"title": title, "due": due}


# --------------------------------------------------------------------------
# 待办本
# --------------------------------------------------------------------------

def _todo_file() -> str:
    return os.path.join(DATA_DIR, "wx_todos.json")


def load_todos() -> dict[str, list[dict]]:
    global _todos_loaded
    if _todos_loaded:
        return _todos
    _todos_loaded = True
    try:
        with open(_todo_file(), "r", encoding="utf-8") as f:
            _todos.update(json.load(f))
    except Exception:  # noqa: BLE001
        pass
    return _todos


def save_todos() -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_todo_file(), "w", encoding="utf-8") as f:
            json.dump(_todos, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def add_todo(openid: str, task: dict) -> bool:
    load_todos()
    items = _todos.setdefault(openid, [])
    for it in items:
        if it["title"] == task["title"] and not it.get("done"):
            it["due"] = task.get("due") or it.get("due")
            save_todos()
            return False
    items.append({
        "title": task["title"],
        "due": task.get("due"),
        "done": False,
        "at": datetime.now().strftime("%m-%d %H:%M"),
    })
    del items[:-80]
    save_todos()
    return True


def todo_summary(openid: str) -> str:
    load_todos()
    items = [x for x in _todos.get(openid, []) if not x.get("done")]
    if not items:
        return ""
    items.sort(key=lambda x: x.get("due") or "9999")
    return "、".join(f"{it['title']}（{it['due'] or '没说什么时候'}）" for it in items[:6])


# --------------------------------------------------------------------------
# DeepSeek
# --------------------------------------------------------------------------

def _http_post_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def ask_deepseek(openid: str, user_text: str, todo_hint: str = "",
                 due_hint: str = "") -> str:
    if not DEEPSEEK_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY 没配")

    with _lock:
        hist = HISTORY.setdefault(openid, [])
        snapshot = list(hist[-HISTORY_LIMIT:])

    now = datetime.now()
    weekday = "一二三四五六日"[now.weekday()]
    system = (
        PERSONA.format(nick=SELF_NICKNAME)
        + f"\n\n现在是 {now.year}年{now.month}月{now.day}日 周{weekday} {now:%H:%M}。"
        + "提到「周五」「下周三」这类说法时，按这个日期算，别自己猜。"
    )

    messages = [{"role": "system", "content": system}]
    messages.extend(snapshot)

    extra = ""
    if due_hint:
        extra += f"\n[系统备注：这条消息里识别到的截止时间是 {due_hint}，说的时候以这个为准]"
    if todo_hint:
        extra += f"\n[系统备注：ta 目前记着这些事 —— {todo_hint}]"
    messages.append({"role": "user", "content": user_text + extra})

    res = _http_post_json(
        DEEPSEEK_BASE + "/chat/completions",
        {
            "model": DEEPSEEK_MODEL,
            "messages": messages,
            "temperature": 1.3,
            "max_tokens": MAX_TOKENS,
            "stream": False,
        },
        {"Authorization": f"Bearer {DEEPSEEK_KEY}"},
        timeout=LLM_BUDGET,
    )
    reply = (res.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    if not reply:
        raise RuntimeError("空回复")

    with _lock:
        hist.append({"role": "user", "content": user_text})
        hist.append({"role": "assistant", "content": reply})
        del hist[:-HISTORY_LIMIT]

    return reply


# --------------------------------------------------------------------------
# 微信协议
# --------------------------------------------------------------------------

def check_signature(signature: str, timestamp: str, nonce: str) -> bool:
    raw = "".join(sorted([WX_TOKEN, timestamp, nonce])).encode("utf-8")
    return hashlib.sha1(raw).hexdigest() == signature


def parse_xml(body: bytes) -> dict[str, str]:
    root = ET.fromstring(body)
    return {child.tag: (child.text or "") for child in root}


def cdata(s: str) -> str:
    return s.replace("]]>", "]] >")


def text_reply(msg: dict, content: str) -> bytes:
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{cdata(msg.get('FromUserName', ''))}]]></ToUserName>"
        f"<FromUserName><![CDATA[{cdata(msg.get('ToUserName', ''))}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{cdata(content)}]]></Content>"
        "</xml>"
    ).encode("utf-8")


_reply_cache: dict[str, tuple[str, float]] = {}
_QUERY_RE = re.compile(r"待办|今天有啥|有啥事|清单|要做什么|还有啥")


def handle_message(msg: dict) -> str:
    openid = msg.get("FromUserName", "")
    mtype = msg.get("MsgType", "")

    if mtype == "event":
        ev = msg.get("Event", "")
        if ev in ("subscribe", "SCAN"):
            nick = "你的" if SELF_NICKNAME == "你" else f"{SELF_NICKNAME}的"
            return (f"来了。我是{nick}学习搭子，群里的作业、考试、组会直接转给我，"
                    "我帮你记着。说「待办」我就把记的事念给你听。")
        return "嗯。"

    if mtype != "text":
        return "图片语音我这边收不全，打字给我吧。"

    text = (msg.get("Content") or "").strip()
    if not text:
        return "？"

    if _QUERY_RE.search(text):
        summary = todo_summary(openid)
        return f"记着这些：{summary}。别的没了。" if summary else \
               "暂时没记下什么。群里看到要交的东西直接转给我。"

    note = ""
    task = maybe_task(text)
    if task and add_todo(openid, task):
        note = f"（记下了：{task['title']}" + (f"，{task['due']} 前）" if task.get("due") else "）")

    try:
        answer = ask_deepseek(
            openid, text, todo_summary(openid),
            due_hint=(task or {}).get("due") or "",
        )
    except socket.timeout:
        answer = "刚走神了，你再说一遍？"
    except Exception as e:  # noqa: BLE001
        print(f"[wx] deepseek 失败: {type(e).__name__}: {e}", flush=True)
        answer = "我这会儿有点卡，等下再问我。"

    return answer + note


# --------------------------------------------------------------------------
# WSGI
# --------------------------------------------------------------------------

def _respond(start_response, body: bytes, status="200 OK",
             ctype="application/xml; charset=utf-8") -> list[bytes]:
    start_response(status, [
        ("Content-Type", ctype),
        ("Content-Length", str(len(body))),
    ])
    return [body]


def app(environ: dict, start_response: Callable) -> list[bytes]:
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET")
    qs = {k: v[0] for k, v in parse_qs(environ.get("QUERY_STRING", "")).items()}

    length = int(environ.get("CONTENT_LENGTH") or 0)
    body = environ["wsgi.input"].read(length) if length else b""

    if path == "/" and method == "GET":
        return _respond(start_response, b"wx-ai is running", ctype="text/plain; charset=utf-8")

    if path == "/health" and method == "GET":
        payload = json.dumps({
            "ok": True,
            "deepseek_configured": bool(DEEPSEEK_KEY),
            "token_configured": WX_TOKEN != "change-me",
            "users": len(_todos),
        }, ensure_ascii=False).encode("utf-8")
        return _respond(start_response, payload, ctype="application/json; charset=utf-8")

    if path == "/admin/push" and method == "POST":
        # 手动塞一条待办（给 agent/digest.py 之类的程序用）
        if ADMIN_SECRET and qs.get("secret") != ADMIN_SECRET:
            return _respond(start_response, b'{"error":"forbidden"}', "403 Forbidden",
                            "application/json; charset=utf-8")
        try:
            data = json.loads(body.decode("utf-8"))
            openid = data["openid"]
            for t in data.get("tasks", []):
                add_todo(openid, {"title": t["title"], "due": t.get("due")})
            return _respond(start_response, b'{"ok":true}', ctype="application/json; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            payload = json.dumps({"error": str(e)}).encode("utf-8")
            return _respond(start_response, payload, "400 Bad Request",
                            "application/json; charset=utf-8")

    if path != "/wx":
        return _respond(start_response, b"not found", "404 Not Found", "text/plain; charset=utf-8")

    if not check_signature(qs.get("signature", ""), qs.get("timestamp", ""), qs.get("nonce", "")):
        return _respond(start_response, b"bad signature", "403 Forbidden",
                        "text/plain; charset=utf-8")

    if method == "GET":
        echo = qs.get("echostr", "")
        return _respond(start_response, echo.encode("utf-8"), ctype="text/plain; charset=utf-8")

    try:
        msg = parse_xml(body)
    except Exception:  # noqa: BLE001
        return _respond(start_response, b"", ctype="application/xml")

    msg_id = msg.get("MsgId") or f"{msg.get('FromUserName','')}-{int(time.time())}"
    cached = _reply_cache.get(msg_id)
    if cached and time.time() - cached[1] < 300:
        return _respond(start_response, text_reply(msg, cached[0]))

    try:
        answer = handle_message(msg)
    except Exception as e:  # noqa: BLE001
        print(f"[wx] 处理异常: {type(e).__name__}: {e}", flush=True)
        answer = "嗯……刚出了点岔子，你再说一次。"

    _reply_cache[msg_id] = (answer, time.time())
    if len(_reply_cache) > 500:
        cutoff = time.time() - 600
        for k in [k for k, (_, ts) in _reply_cache.items() if ts < cutoff]:
            _reply_cache.pop(k, None)

    return _respond(start_response, text_reply(msg, answer))


# --------------------------------------------------------------------------
# 本地跑
# --------------------------------------------------------------------------

if __name__ == "__main__":
    from wsgiref.simple_server import make_server

    port = int(os.environ.get("PORT", "8000"))
    print(f"[wx-ai] :{port}  DeepSeek={'✓' if DEEPSEEK_KEY else '✗ 没配'}  "
          f"Token={'✓' if WX_TOKEN != 'change-me' else '✗ 用的默认值'}", flush=True)
    make_server("0.0.0.0", port, app).serve_forever()
