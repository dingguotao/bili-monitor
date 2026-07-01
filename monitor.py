#!/usr/bin/env python3
"""B 站动态监控 — 抓取 → 去重推送动态 → 始终扫 48h 跨 UP 股票重叠 → 重叠则预警

用法:
    python3 monitor.py            # 正常监控：每次都做动态推送 + 跨 UP 比对
    python3 monitor.py --force    # 强制推送：每个启用 UP 推最新一条，仍会跑跨 UP 比对
    python3 monitor.py --init     # 初始化：把当前所有动态吸收为已推送，不发任何消息
    python3 monitor.py --dry-run  # 演练：打印 payload 不发飞书，不更新状态

推送成功后才更新 memory/bili_sent_ids_<uid>.json，避免推送失败导致动态丢失。
跨 UP 预警靠 memory/alerted_pairs.json 去重，同一组合 48h 内只发一次。
"""

import argparse
import datetime
import json
import os
import ssl
import sys
import time
import urllib.request

import bili_fetch
import stock_match
from config_loader import config

_DIR = os.path.dirname(os.path.abspath(__file__))
FEISHU_WEBHOOK    = config.get("feishu_webhook")
MEMORY_DIR        = config.get_path("monitor.memory_dir")
ERROR_LOG         = config.get_path("monitor.error_log_file")
RECENT_STOCKS     = config.get_path("monitor.recent_stocks_file")
ALERTED_PAIRS     = config.get_path("monitor.alerted_pairs_file")
KEEP_RECENT_SENT_IDS  = config.get("monitor.keep_recent_sent_ids", 200)
FEISHU_MESSAGE_MAX_BYTES = config.get("feishu.message_max_bytes", 20480)
FEISHU_BATCH_MAX_BYTES   = config.get("feishu.batch_max_bytes", 18000)
STOCK_MATCH_WINDOW_SEC   = config.get("monitor.stock_match_window_seconds", 172800)

_VERIFY_SSL = config.get("bilibili.verify_ssl", True)
_SSL_CTX = None if _VERIFY_SSL else ssl._create_unverified_context()

# 错误收集器：cmd_run 执行期间收集的错误列表
_error_collector = []


def log_error(msg):
    os.makedirs(os.path.dirname(ERROR_LOG), exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(ERROR_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")
    # 同时收集到内存，供飞书推送
    if _error_collector is not None:
        _error_collector.append({"time": ts, "msg": msg})


def load_targets():
    return config.get_enabled_ups()


def sent_ids_path(uid):
    return os.path.join(MEMORY_DIR, f"bili_sent_ids_{uid}.json")


def load_sent_ids(uid):
    path = sent_ids_path(uid)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log_error(f"[{uid}] sent_ids 文件损坏，重置为空: {e}")
        return []


def save_sent_ids(uid, ids):
    os.makedirs(MEMORY_DIR, exist_ok=True)
    # 去重 + 截断尾部，最近的留下来
    seen = list(dict.fromkeys(ids))[-KEEP_RECENT_SENT_IDS:]
    with open(sent_ids_path(uid), "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False)


def fetch_and_parse(target):
    """抓 + 解析 + 过滤空 text + 按 pub_ts 升序。失败返回 None。"""
    uid = target["uid"]
    try:
        data = bili_fetch.fetch_dynamics(uid)
    except Exception as e:
        log_error(f"[{target['name']}/{uid}] 抓取失败: {e}")
        return None
    if data.get("code") != 0:
        log_error(f"[{target['name']}/{uid}] API 错误 code={data.get('code')} msg={data.get('message')}")
        return None
    items = data.get("data", {}).get("items", [])
    parsed = [bili_fetch.parse_item(x) for x in items]
    parsed = [p for p in parsed if p and p.get("text") and p.get("pub_ts")]
    parsed.sort(key=lambda p: p["pub_ts"])
    return parsed


def split_new_items(parsed, uid, force=False):
    """从 parsed 中按 sent_ids 切分出待推送条目。"""
    if force:
        return (parsed[-1:], set()) if parsed else ([], set())
    sent = set(load_sent_ids(uid))
    new_items = [p for p in parsed if p["id_str"] not in sent]
    return new_items, sent


def collect_new_items(target, force=False):
    """兼容老接口：抓取并切分。"""
    parsed = fetch_and_parse(target)
    if parsed is None:
        return [], set()
    return split_new_items(parsed, target["uid"], force=force)


def build_block(target, item):
    """单条动态转换成推送数据块。"""
    dt = datetime.datetime.fromtimestamp(item["pub_ts"])
    return {
        "uid": target["uid"],
        "name": target["name"],
        "topic": target["topic"],
        "id_str": item["id_str"],
        "time": dt.strftime("%m-%d %H:%M"),
        "text": item["text"],
        "url": item["url"],
    }


def build_feishu_payload(blocks, batch_idx=None, batch_total=None):
    """组装动态推送为飞书交互卡片，使用蓝色标题栏。

    格式：
      ┌─ 📢 B 站动态推送 (共 N 条)   蓝色标题栏
      │
      │  【UP 昵称】                 ← 可点击跳转 TA 的空间
      │  🕐 07-01 11:31
      │  正文内容...
      │  📄 查看原文
      │  ───
      │  ... 下一个 UP
      └───

    batch_idx / batch_total：拆分发送时标题追加 "第 x/y 批" 提示。
    """
    lines = []
    for i, b in enumerate(blocks):
        if i > 0:
            lines.append("---")
        up_link = f"[【{b['name']}】](https://space.bilibili.com/{b['uid']}/dynamic)"
        lines.append(f"**{up_link}** · 🕐 {b['time']}")
        lines.append(f"{b['text']} [查看原文]({b['url']})")
    if batch_idx and batch_total and batch_total > 1:
        title = f"📢 B 站动态推送 (共 {len(blocks)} 条 · 第 {batch_idx}/{batch_total} 批)"
    else:
        title = f"📢 B 站动态推送 (共 {len(blocks)} 条)"
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": config.get("feishu.card_theme.push", "blue"),
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {"tag": "markdown", "content": "\n".join(lines)},
            ],
        },
    }


def _payload_size(payload):
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def split_blocks_for_feishu(blocks):
    """把 blocks 切成若干 batch，每 batch 打包后 ≤ FEISHU_BATCH_MAX_BYTES (18KB)。

    保守阈值预留 2KB 余量，避免 payload 边界抖动导致偶发超限。
    贪心：从头累加，遇到单块就爆 18KB 时（罕见），单独发那一块。
    返回 [[block, ...], [block, ...], ...]
    """
    if not blocks:
        return []
    batches = []
    cur = []
    for b in blocks:
        trial = cur + [b]
        # 用最终会用的 title 结构做体积估算，让批数标题带来的字节数进入判断
        payload = build_feishu_payload(trial, batch_idx=len(batches) + 1, batch_total=99)
        if _payload_size(payload) <= FEISHU_BATCH_MAX_BYTES:
            cur = trial
        else:
            if cur:
                batches.append(cur)
                cur = [b]
            else:
                # 单块自身超限：只能孤零零一批发出去（push 时仍可能失败）
                batches.append([b])
                cur = []
    if cur:
        batches.append(cur)
    return batches


def push_feishu(payload):
    """同步推送单条 payload；返回 True/False。

    体积超上限时不再硬拒（拆分交给上层 split_blocks_for_feishu 处理），
    这里只兜底拦住"单块 payload 就爆 20KB"这种理论极端情况，仍然写 log 提醒。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(body) > FEISHU_MESSAGE_MAX_BYTES:
        log_error(f"单块 payload {len(body)} 字节仍超过 20KB，放弃这一批")
        return False
    req = urllib.request.Request(FEISHU_WEBHOOK, data=body,
                                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=config.get("feishu.request_timeout_seconds", 15), context=_SSL_CTX) as r:
            resp = json.loads(r.read())
    except Exception as e:
        log_error(f"飞书 POST 异常: {e}")
        return False
    if resp.get("code") != 0:
        log_error(f"飞书返回非 0: {resp}")
        return False
    return True


# ────────── 跨 UP 股票预警 ──────────

def load_recent_stocks():
    """读 recent_stocks.jsonl 并淘汰 48h 之前的记录。

    每行 {"ts": int, "up": str, "uid": str, "id_str": str, "url": str,
          "stocks": [{"code","name","match","mode"}]}
    """
    if not os.path.exists(RECENT_STOCKS):
        return []
    cutoff = int(time.time()) - STOCK_MATCH_WINDOW_SEC
    records = []
    with open(RECENT_STOCKS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("ts", 0) >= cutoff:
                records.append(rec)
    return records


def save_recent_stocks(records):
    """重写整个文件，已被淘汰的记录不再保留。"""
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(RECENT_STOCKS, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_alerted_pairs():
    """已预警的组合 → ts，避免反复打扰。

    key = "股票key|UP_A|UP_B"（两个 UP 名按字典序，单 UP 自比对不算）
    """
    if not os.path.exists(ALERTED_PAIRS):
        return {}
    try:
        with open(ALERTED_PAIRS, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    # 淘汰 48h 前的预警
    cutoff = int(time.time()) - STOCK_MATCH_WINDOW_SEC
    return {k: v for k, v in data.items() if v >= cutoff}


def save_alerted_pairs(pairs):
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(ALERTED_PAIRS, "w", encoding="utf-8") as f:
        json.dump(pairs, f, ensure_ascii=False)


def detect_cross_up_overlaps(records):
    """从 recent_stocks 记录中找出 ≥ 2 个 UP 都提到的股票 key。

    同一条动态（相同 uid + id_str）如果命中多个相似股票（如「深科技」「深科」），
    合并成一个 source：matches 是命中词列表，stock_names 是对应股票名列表。

    返回 [{"key": "光华", "names": ["光华科技","光华股份"], "sources": [
        {"up": ..., "uid": ..., "id_str": ..., "url": ..., "ts": ...,
         "matches": ["光华sj", "光华"], "stock_names": ["光华科技", "光华股份"]},
        ...
    ]}]
    """
    # key -> {"names": set, "sources_map": {(uid, id_str): source_dict}}
    by_key = {}
    for rec in records:
        for s in rec.get("stocks", []):
            k = stock_match.similarity_key(s)
            slot = by_key.setdefault(k, {"names": set(), "sources_map": {}})
            slot["names"].add(s["name"])
            src_key = (rec["uid"], rec["id_str"])
            src = slot["sources_map"].get(src_key)
            if src is None:
                src = {
                    "up": rec["up"], "uid": rec["uid"], "id_str": rec["id_str"],
                    "url": rec["url"], "ts": rec["ts"],
                    "matches": [], "stock_names": [],
                }
                slot["sources_map"][src_key] = src
            # 同一条动态里，同一个命中词只保留一次，股票名同理
            if s["match"] not in src["matches"]:
                src["matches"].append(s["match"])
            if s["name"] not in src["stock_names"]:
                src["stock_names"].append(s["name"])

    overlaps = []
    for k, slot in by_key.items():
        sources = list(slot["sources_map"].values())
        ups = {s["up"] for s in sources}
        if len(ups) >= 2:
            overlaps.append({
                "key": k,
                "names": sorted(slot["names"]),
                "sources": sorted(sources, key=lambda x: x["ts"]),
            })
    return overlaps


def build_alert_payload(overlap):
    """组装预警消息为飞书交互卡片，公共关键词（key、股票名、命中词）用红色加粗突出。

    飞书 post 富文本不支持颜色，因此预警消息用 interactive 卡片 + markdown。
    markdown 语法：`<font color='red'>xxx</font>` 显示红色；`**xxx**` 加粗。

    展示 key 规则：
      - 若所有 UP 都精确命中同一支股票（names 长度=1），直接用完整股票名做 key
      - 否则用前 2 字分组 key + 📌 列出全部匹配的股票名
    """
    names = overlap["names"]
    up_count = len({s["up"] for s in overlap["sources"]})

    def red(text):
        return f"<font color='red'>**{text}**</font>"

    if len(names) == 1:
        # 唯一精确命中：标题里只显示完整股票名，省掉 📌 那段
        display_key = names[0]
        header_line = f"🚨 **{up_count}** 位 UP 同时提到 {red('「' + display_key + '」')}"
    else:
        display_key = overlap["key"]
        header_line = (
            f"🚨 **{up_count}** 位 UP 同时提到 {red('「' + display_key + '」')} "
            f"· 📌 {red('、'.join(names))}"
        )

    lines = [header_line, "---"]
    for s in overlap["sources"]:
        dt = datetime.datetime.fromtimestamp(s["ts"]).strftime("%m-%d %H:%M")
        up_link = f"[{s['up']}](https://space.bilibili.com/{s['uid']}/dynamic)"
        matches_str = "、".join(red(m) for m in s["matches"])
        names_str = "、".join(s["stock_names"])
        lines.append(
            f"🕐 {dt} · 【{up_link}】提到 {matches_str} → {names_str} "
            f"[查看原文]({s['url']})"
        )

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": config.get("feishu.card_theme.alert", "red"),
                "title": {"tag": "plain_text", "content": f"🚨 跨 UP 股票预警:{display_key}"},
            },
            "elements": [
                {"tag": "markdown", "content": "\n".join(lines)},
            ],
        },
    }


def build_error_payload(errors):
    """组装错误汇总为飞书交互卡片，使用灰色标题栏。

    格式：
      ┌─ ⚠️ 监控任务异常 (共 N 条)   灰色标题栏
      │
      │  🕐 07-01 16:30:15
      │  [UP昵称/UID] 抓取失败: ...
      │  ───
      │  🕐 07-01 16:30:20
      │  飞书返回非 0: ...
      └───

    errors: [{"time": "2026-07-01 16:30:15", "msg": "..."}]
    """
    lines = []
    for i, e in enumerate(errors):
        if i > 0:
            lines.append("---")
        lines.append(f"🕐 {e['time']}")
        # 避免单条消息过长导致 payload 超限，做温和截断
        msg = e['msg']
        if len(msg) > 500:
            msg = msg[:500] + "…(截断)"
        lines.append(msg)

    title = f"⚠️ 监控任务异常 (共 {len(errors)} 条)"
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": config.get("feishu.card_theme.error", "grey"),
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {"tag": "markdown", "content": "\n".join(lines)},
            ],
        },
    }


def push_error_summary(dry_run=False):
    """本轮结束时，把收集到的错误合并推一条灰色卡片到飞书。

    - 无错误 → 静默返回
    - dry_run → 仅打印不发送
    - 发送失败不写 error.log（避免死循环）
    """
    if not _error_collector:
        return
    payload = build_error_payload(_error_collector)
    body_size = _payload_size(payload)
    print(f"⚠️  收集到 {len(_error_collector)} 条错误，准备推送错误汇总卡片 ({body_size} 字节)")
    if dry_run:
        print("--- dry-run 错误汇总，不真实发送 ---")
        return
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(FEISHU_WEBHOOK, data=body,
                                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=config.get("feishu.request_timeout_seconds", 15), context=_SSL_CTX) as r:
            resp = json.loads(r.read())
        if resp.get("code") != 0:
            # 错误卡片自身推送失败，只打到 stdout，不再回写 error_collector 避免下轮又追加
            print(f"  ✗ 错误汇总卡片返回非 0: {resp}")
        else:
            print("  ✓ 错误汇总卡片已推送")
    except Exception as e:
        print(f"  ✗ 错误汇总卡片推送异常: {e}")


def run_alerts(new_records):
    """[已废弃] 旧版接口，保留以防外部调用；新逻辑全部在 cmd_run 内。"""
    raise RuntimeError("run_alerts 已废弃，请直接调用 cmd_run")


def cmd_init(targets):
    """初始化：把当前所有动态视为已推送，不发任何消息。"""
    for t in targets:
        try:
            data = bili_fetch.fetch_dynamics(t["uid"])
        except Exception as e:
            log_error(f"[{t['name']}/{t['uid']}] init 抓取失败: {e}")
            continue
        if data.get("code") != 0:
            log_error(f"[{t['name']}/{t['uid']}] init API 错误: {data.get('message')}")
            continue
        items = data.get("data", {}).get("items", [])
        ids = [it.get("id_str", "") for it in items if it.get("id_str")]
        save_sent_ids(t["uid"], ids)
        print(f"[init] {t['name']} 吸收 {len(ids)} 条历史动态")


def cmd_run(targets, force=False, dry_run=False):
    """每次执行：
    1) 抓取每个 UP 的动态（一次抓取，下面两步复用）
    2) 有未推过的新动态 → 推送 + 更新 sent_ids
    3) 不论第 2 步是否有动作，重新扫所有 UP 最近 48h 全部动态，
       发现跨 UP 重叠股票就推预警（靠 alerted_pairs.json 去重）
    """
    global _error_collector
    _error_collector = []  # 每次运行清空，避免多次调用累积

    now = int(time.time())
    cutoff = now - STOCK_MATCH_WINDOW_SEC

    # 1. 抓取并缓存解析结果
    parsed_per_target = {}     # uid -> [parsed]
    blocks_per_target = []     # [(target, sent_ids_set, [new_items])] 用于推送
    for t in targets:
        parsed = fetch_and_parse(t)
        if parsed is None:
            continue
        parsed_per_target[t["uid"]] = parsed
        new_items, sent = split_new_items(parsed, t["uid"], force=force)
        if new_items:
            print(f"[{t['name']}] {len(new_items)} 条新{'(force)' if force else ''}: "
                  + ", ".join(x["id_str"] for x in new_items))
            blocks_per_target.append((t, sent, new_items))
        else:
            print(f"[{t['name']}] 0 条新动态")

    # 2. 推送新动态（如有）
    #    - 展开成 [(target, item, block)] 全局按 pub_ts 排序
    #    - 用 split_blocks_for_feishu 切成 ≤ 20KB 的多批
    #    - **每一批推送成功后，立刻把这一批对应的 id_str 追加到各 UP 的 sent_ids**
    #      避免"整轮成功才写回"导致的部分成功丢失。
    push_ok = True  # 没新动态时也算成功，不阻塞预警
    if blocks_per_target:
        # (target, item, block) 三元组，用 uid+id_str 做映射键
        flat = []
        for (t, _sent, items) in blocks_per_target:
            for it in items:
                flat.append((t, it, build_block(t, it)))
        # 全局按发布时间排序，保证批次里时间顺序一致
        flat.sort(key=lambda x: x[1]["pub_ts"])

        # 直接把所有 blocks 拆分（不再截 top-N，交给拆分保护）
        blocks = [b for (_, _, b) in flat]
        batches = split_blocks_for_feishu(blocks)
        total_new = len(flat)
        print(f"待推 {total_new} 条 → 拆成 {len(batches)} 批")

        # 用 (uid, id_str) → (target, item) 做反查表，用于每批成功后精确写回
        by_key = {(t["uid"], it["id_str"]): (t, it) for (t, it, _b) in flat}
        # 每个 UP 起始的 sent_ids（不变）
        base_sent = {t["uid"]: list(sent) for (t, sent, _items) in blocks_per_target}
        # 每个 UP 本轮已经推送成功的 id 累积器
        confirmed_ids = {t["uid"]: [] for (t, _, _) in blocks_per_target}

        pushed_batch = 0
        pushed_count = 0
        for i, batch in enumerate(batches, start=1):
            payload = build_feishu_payload(batch, batch_idx=i, batch_total=len(batches))
            body_size = _payload_size(payload)
            print(f"  批 {i}/{len(batches)}: {body_size} 字节 · {len(batch)} 条")
            if dry_run:
                continue
            if not push_feishu(payload):
                push_ok = False
                # 汇总本批具体丢弃了什么，方便排查
                dropped = [f"{b['name']}/{b['id_str']}: {(b.get('text') or '')[:30]}" for b in batch]
                log_error(f"批 {i}/{len(batches)} 推送失败，丢弃 {len(batch)} 条:\n" + "\n".join(dropped))
                print(f"  ✗ 批 {i} 推送失败（详见 data/bili-monitor-error.log），后续批停止")
                break
            # 本批成功：立刻登记 ID
            for b in batch:
                confirmed_ids[b["uid"]].append(b["id_str"])
            pushed_batch += 1
            pushed_count += len(batch)

        if dry_run:
            print(f"--- dry-run 结束，共 {len(batches)} 批未真实发送 ---")
        else:
            # 无论后续批是否失败，把已确认的部分写回 sent_ids
            for uid, ids in confirmed_ids.items():
                if ids:
                    save_sent_ids(uid, base_sent[uid] + ids)
            if push_ok:
                print(f"✓ 全部 {len(batches)} 批推送成功，共 {total_new} 条")
            else:
                print(f"⚠️  部分成功：已推 {pushed_batch}/{len(batches)} 批 · {pushed_count}/{total_new} 条")

    # 3. 始终扫 48h 全部动态 → 跨 UP 重叠预警
    records = []
    for t in targets:
        parsed = parsed_per_target.get(t["uid"])
        if not parsed:
            continue
        n_hit = 0
        for p in parsed:
            if p["pub_ts"] < cutoff:
                continue
            hits = stock_match.extract_stocks(p.get("text") or "")
            if not hits:
                continue
            n_hit += len(hits)
            records.append({
                "ts": p["pub_ts"], "up": t["name"], "uid": t["uid"],
                "id_str": p["id_str"], "url": p.get("url") or "",
                "stocks": hits,
            })
        if n_hit:
            print(f"  [stocks] {t['name']} 2d 内命中 {n_hit} 次")

    if not records:
        print("无股票命中，跳过跨 UP 比对")
        return 0 if push_ok else 1

    overlaps = detect_cross_up_overlaps(records)
    if not overlaps:
        print("无跨 UP 重叠")
        return 0 if push_ok else 1

    print(f"发现 {len(overlaps)} 组跨 UP 重叠：")
    for ov in overlaps:
        ups = sorted({s["up"] for s in ov["sources"]})
        print(f"  ■ 「{ov['key']}」({'、'.join(ov['names'])}) — {ups}")

    if dry_run:
        print("--- dry-run 预警，不写 recent_stocks、不推飞书 ---")
        for ov in overlaps:
            payload = build_alert_payload(ov)
            size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            print(f"  预警 payload {size} 字节, key={ov['key']}")
        return 0 if push_ok else 1

    # 写回 recent_stocks（合并 + 去重 + 自动淘汰由 load_recent_stocks 完成）
    existing = load_recent_stocks()
    seen = {(r["uid"], r["id_str"]) for r in existing}
    save_recent_stocks(existing + [r for r in records if (r["uid"], r["id_str"]) not in seen])

    # 推预警 + alerted_pairs 去重
    alerted = load_alerted_pairs()
    sent_count = 0
    for ov in overlaps:
        ups = sorted({s["up"] for s in ov["sources"]})
        pair_key = f"{ov['key']}|" + "|".join(ups)
        if pair_key in alerted:
            print(f"  [skip] 已预警过: {pair_key}")
            continue
        if push_feishu(build_alert_payload(ov)):
            alerted[pair_key] = now
            sent_count += 1
            print(f"  ✓ 已发预警: {pair_key}")
        else:
            print(f"  ✗ 预警推送失败: {pair_key}")
    save_alerted_pairs(alerted)
    if sent_count:
        print(f"✓ 共发出 {sent_count} 条预警")
    return 0 if push_ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--force", action="store_true", help="忽略去重，每个启用 UP 推最新一条")
    g.add_argument("--init", action="store_true", help="初始化已推送列表，不发任何消息")
    ap.add_argument("--dry-run", action="store_true", help="打印 payload 不发飞书，不更新状态")
    args = ap.parse_args()

    targets = load_targets()
    print(f"启用 UP {len(targets)} 个: {[t['name'] for t in targets]}")

    if args.init:
        cmd_init(targets)
        return 0
    exit_code = cmd_run(targets, force=args.force, dry_run=args.dry_run)
    push_error_summary(dry_run=args.dry_run)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

