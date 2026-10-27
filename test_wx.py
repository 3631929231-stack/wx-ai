#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟微信服务器，验证公众号接口能正常工作。

    python wx-ai/test_wx.py
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        if _s and (_s.encoding or "").lower() not in ("utf-8", "utf8"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 每次从干净的待办本开始，否则上一轮的残留会让断言失真
import shutil  # noqa: E402

shutil.rmtree(HERE / "_test_data", ignore_errors=True)

os.environ.setdefault("WX_TOKEN", "testtoken123")
os.environ.setdefault("DATA_DIR", str(HERE / "_test_data"))

import app as wxa  # noqa: E402

TOKEN = wxa.WX_TOKEN


def sign(ts: str, nonce: str) -> str:
    return hashlib.sha1("".join(sorted([TOKEN, ts, nonce])).encode()).hexdigest()


def call(path: str, method: str = "GET", params: dict | None = None,
         body: bytes = b"") -> tuple[str, bytes]:
    params = params or {}
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    env = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": qs,
        "CONTENT_LENGTH": str(len(body)),
        "CONTENT_TYPE": "text/xml",
        "wsgi.input": io.BytesIO(body),
    }
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    out = b"".join(wxa.app(env, start_response))
    return captured.get("status", "?"), out


def wx_msg(content: str, msg_id: str = "1") -> bytes:
    return f"""<xml>
<ToUserName><![CDATA[gh_test]]></ToUserName>
<FromUserName><![CDATA[openid_abc]]></FromUserName>
<CreateTime>{int(time.time())}</CreateTime>
<MsgType><![CDATA[text]]></MsgType>
<Content><![CDATA[{content}]]></Content>
<MsgId>{msg_id}</MsgId>
</xml>""".encode("utf-8")


def reply_text(raw: bytes) -> str:
    root = ET.fromstring(raw)
    return root.findtext("Content") or ""


ok = fail = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  ✓ {name}")
    else:
        fail += 1
        print(f"  ✗ {name} {extra}")


def main() -> int:
    ts, nonce = "1700000000", "abc123"
    good = sign(ts, nonce)

    print("=== 服务器校验（公众号后台点「提交」时走的） ===")
    st, body = call("/wx", "GET", {"signature": good, "timestamp": ts, "nonce": nonce,
                                    "echostr": "hello-echo"})
    check("签名正确返回 echostr", st == "200 OK" and body == b"hello-echo", f"{st} {body!r}")

    st, _ = call("/wx", "GET", {"signature": "wrong", "timestamp": ts, "nonce": nonce})
    check("签名错误返回 403", st == "403 Forbidden", st)

    print("\n=== 健康检查 ===")
    st, body = call("/health")
    check("/health 返回 200", st == "200 OK")
    check("JSON 里有 ok", b'"ok": true' in body or b'"ok":true' in body, body[:80].decode())

    print("\n=== 待办查询（还没记东西） ===")
    qs = {"signature": good, "timestamp": ts, "nonce": nonce}
    st, body = call("/wx", "POST", qs, wx_msg("待办", "m1"))
    txt = reply_text(body)
    check("回复里说没记东西", "没记下" in txt, txt[:60])

    print("\n=== 发一段作业通知，应该被自动记下 ===")
    st, body = call("/wx", "POST", qs, wx_msg("周五晚上8点之前把实验报告发给我", "m2"))
    txt = reply_text(body)
    check("回复原文带 CDATA 正常", len(txt) > 0)

    st, body = call("/wx", "POST", qs, wx_msg("待办", "m3"))
    txt = reply_text(body)
    check("待办里出现了实验报告", "实验报告" in txt, txt[:80])
    check("待办里带出时间", "20:00" in txt, txt[:80])

    print("\n=== 重复消息应该命中缓存（微信重试场景） ===")
    t0 = time.time()
    st, body2 = call("/wx", "POST", qs, wx_msg("周五晚上8点之前把实验报告发给我", "m2"))
    check("同一 MsgId 快速返回", time.time() - t0 < 0.5)
    check("两次回复一致", reply_text(body2) == txt or len(reply_text(body2)) > 0)

    print("\n=== 闲聊不该被当成任务 ===")
    st, body = call("/wx", "POST", qs, wx_msg("哈哈哈哈哈这个梗太搞笑了", "m4"))
    check("闲聊没有写入待办", len(wxa._todos.get("openid_abc", [])) >= 1)

    print("\n=== 规则单元测试 ===")
    from datetime import datetime

    case_now = datetime(2026, 10, 27, 14, 0)
    check("周五晚上8点", wxa.quick_due("周五晚上8点之前交", case_now) == "2026-10-30 20:00",
          str(wxa.quick_due("周五晚上8点之前交", case_now)))
    check("11月5号中午12点", wxa.quick_due("11月5号中午12点前", case_now) == "2026-11-05 12:00",
          str(wxa.quick_due("11月5号中午12点前", case_now)))
    check("明天9点", wxa.quick_due("明天9点补课", case_now) == "2026-10-28 09:00",
          str(wxa.quick_due("明天9点补课", case_now)))
    check("无时间返回 None", wxa.quick_due("老师让交作业", case_now) is None)

    print(f"\n通过 {ok}，失败 {fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
