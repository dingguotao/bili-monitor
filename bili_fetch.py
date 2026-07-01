#!/usr/bin/env python3
"""B站动态抓取脚本 — 用 cookie 调 API 获取指定用户的最新动态

用法:
    python3 bili_fetch.py <uid> [minutes_after]

参数:
    uid            : B站用户 UID（必填）
    minutes_after  : 仅返回最近 N 分钟内的动态；不传或传 0 表示全部
"""

import json
import ssl
import sys
import time
import urllib.request

from config_loader import config

_COOKIE = config.get("bili_cookie")
_FEATURES = config.get("bilibili.api_features")
_TIMEOUT = config.get("bilibili.request_timeout_seconds", 30)
_UA = config.get("bilibili.user_agent")
_VERIFY_SSL = config.get("bilibili.verify_ssl", True)

# 内网环境常缺少根证书，允许通过 config 关闭验证
_SSL_CTX = None if _VERIFY_SSL else ssl._create_unverified_context()


def fetch_dynamics(host_mid):
    url = (
        f"https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
        f"?host_mid={host_mid}"
        f"&timezone_offset=-480&platform=web"
        f"&features={_FEATURES}"
        f"&web_location=333.1387"
    )
    headers = {
        "accept": "application/json",
        "cookie": _COOKIE,
        "referer": f"https://space.bilibili.com/{host_mid}/dynamic",
        "user-agent": _UA,
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=_TIMEOUT, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_item(item):
    """提取单条动态的简要信息"""
    t = item.get("type", "")
    modules = item.get("modules", {})
    author = modules.get("module_author", {})
    dynamic_mod = modules.get("module_dynamic", {})
    id_str = item.get("id_str", "")
    pub_time = author.get("pub_time", "")
    pub_ts = int(author.get("pub_ts", 0))

    result = {
        "type": t,
        "id_str": id_str,
        "pub_time": pub_time,
        "pub_ts": pub_ts,
        "text": "",
        "url": f"https://www.bilibili.com/opus/{id_str}",
    }

    major = dynamic_mod.get("major") or {}

    # OPUS 类型：有 summary.text
    if major.get("type") == "MAJOR_TYPE_OPUS":
        opus = major.get("opus", {})
        if opus:
            summary = opus.get("summary", {})
            result["text"] = summary.get("text", "")
            if not result["text"]:
                result["text"] = opus.get("title", "")

    # DRAW 类型：纯图片动态，提取图片描述
    elif major.get("type") == "MAJOR_TYPE_DRAW":
        draw = major.get("draw", {})
        items = draw.get("items", [])
        if items:
            # 图片数量
            result["text"] = f"🖼️ {len(items)}张图"

    # 视频动态
    elif major.get("type") == "MAJOR_TYPE_ARCHIVE" or t == "DYNAMIC_TYPE_AV":
        archive = major.get("archive", {})
        if archive:
            result["text"] = f"🎬 {archive.get('title', '')}"

    # 专栏文章
    elif major.get("type") == "MAJOR_TYPE_ARTICLE" or t == "DYNAMIC_TYPE_ARTICLE":
        article = major.get("article", {})
        if article:
            result["text"] = f"📝 {article.get('title', '')}"

    # 转发动态
    elif t == "DYNAMIC_TYPE_FORWARD":
        opus = dynamic_mod.get("opus", {})
        if opus:
            summary = opus.get("summary", {})
            result["text"] = summary.get("text", "")

    # 兼容旧格式
    if not result["text"]:
        desc = dynamic_mod.get("desc", {})
        if desc and isinstance(desc, dict):
            result["text"] = desc.get("text", "")

    return result

def main():
    # 参数: <uid> [minutes_after]
    if len(sys.argv) < 2:
        print("用法: python3 bili_fetch.py <uid> [minutes_after]", file=sys.stderr)
        sys.exit(2)

    host_mid = sys.argv[1]
    minutes_after = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    data = fetch_dynamics(host_mid)
    if data.get("code") != 0:
        print(f"API错误: code={data.get('code')} msg={data.get('message')}", file=sys.stderr)
        sys.exit(1)

    items = data.get("data", {}).get("items", [])
    if not items:
        print("[]")
        sys.exit(0)

    now_ts = int(time.time())
    cutoff_ts = now_ts - minutes_after * 60

    new_items = []
    for item in items:
        p = parse_item(item)
        if p["text"]:
            if minutes_after > 0:
                if p["pub_ts"] >= cutoff_ts:
                    new_items.append(p)
            else:
                new_items.append(p)

    # 始终输出合法 JSON 数组（空也是 []），便于上层 json.loads
    output = [
        {"pub_time": x["pub_time"], "id_str": x["id_str"], "text": x["text"], "url": x["url"]}
        for x in new_items
    ]
    print(json.dumps(output, ensure_ascii=False))

if __name__ == "__main__":
    main()
