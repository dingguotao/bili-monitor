#!/usr/bin/env python3
"""股票名提取 + 跨 UP 相似检测。

核心规则（用户约定）：
- 词典：A 股全量股票，预先从新浪行情拉取，存于 stock_dict.json
- 提取：动态正文中出现 完整名/前2字+任意字符 时，命中候选股票集
- stopwords：过滤常见伪命中（"今天/明天/分歧"等 2-3 字高频词）
- 相似判定：两条动态命中的候选集 有交集 = 相似（同一 code 或 前2字相同）
- 时间窗口：最近 48h 内的命中才参与跨 UP 比对
"""

import json
import os
import re
import time

from config_loader import config

STOCK_DICT = config.get_path("stock_match.dict_file")
STOPWORDS = config.get_path("stock_match.stopwords_file")


def _load_dict():
    with open(STOCK_DICT, encoding="utf-8") as f:
        stocks = json.load(f)
    # 1. 全名 → [code]
    by_name = {}
    # 2. 前2字 → [(code, name)]
    by_prefix = {}
    for s in stocks:
        name = s["name"]
        code = s["code"]
        by_name.setdefault(name, []).append(code)
        # 跳过 *ST / ST 这类标记类前缀
        clean = re.sub(r"^[\*\sST]+", "", name)
        if len(clean) >= 2:
            by_prefix.setdefault(clean[:2], []).append((code, name))
    return stocks, by_name, by_prefix


def _load_stopwords():
    if not os.path.exists(STOPWORDS):
        return set()
    with open(STOPWORDS, encoding="utf-8") as f:
        return set(json.load(f))


_STOCKS, _BY_NAME, _BY_PREFIX = _load_dict()
_STOP = _load_stopwords()

# 中文分片：每段连续中文（含可选末尾拼音）当作一个 chunk
_CHUNK_RE = re.compile(r"([一-龥]+)([a-zA-Z]{0,4})")


def _candidates(text):
    """生成 (前2字 prefix, 完整 token) 候选。

    对每段连续中文 chunk：
      - 长度 == 2：作为一个候选 (chunk, chunk+py)
      - 长度 >= 3：滑动窗口取前 2 字、前 3 字、前 4 字，作为候选 prefix
        （股票简称多为 2-4 字开头，「光华科技」「先导智能」「百合花」都符合）
    """
    for m in _CHUNK_RE.finditer(text):
        cn, py = m.group(1), m.group(2)
        if len(cn) < 2:
            continue
        # 候选 1：前 2 字 + 拼音（光华sj、太极sy）
        yield (cn[:2], cn[:2] + py)
        # 候选 2：3-4 字完整名（百合花、三祥新材）
        for k in (3, 4):
            if len(cn) >= k:
                yield (cn[:k], cn[:k])


def extract_stocks(text):
    """从一段文字里抽出命中的股票候选集。

    返回 [{"code": "002741", "name": "光华科技", "match": "光华sj", "mode": "prefix"}]
    去重按 code。命中模式：
      - exact: 完整名命中（如「深科技」「光华科技」等 3-4 字完整名）
      - prefix: 前2字+拼音模糊命中（「光华sj」匹配「光华科技/光华股份」）

    规则：**精确命中优先，一旦某前 2 字对应的股票名被精确命中，就不再用该前 2 字模糊**。
    比如正文里出现「深科技」→ 精确命中「深科技」，此时不再用「深科」去模糊匹配「深科达」。
    """
    if not text:
        return []
    hits = {}  # code -> dict（保留首次命中信息）
    exact_prefixes = set()  # 已精确命中股票名的前 2 字

    def add_exact(name, codes, match):
        for code in codes:
            if code not in hits:
                hits[code] = {"code": code, "name": name, "match": match, "mode": "exact"}
        if len(name) >= 2:
            exact_prefixes.add(name[:2])

    # 1. 全文全名匹配（3 字以上完整名嵌在正文中，如"上车深科技"里的"深科技"）
    for name, codes in _BY_NAME.items():
        if len(name) >= 3 and name in text:
            add_exact(name, codes, name)

    # 2. Chunk 前缀 3-4 字精确命中（补 _candidates 覆盖到的场景）
    candidates = list(_candidates(text))
    for prefix, token in candidates:
        if prefix in _STOP or token in _STOP:
            continue
        if len(prefix) >= 3 and prefix in _BY_NAME:
            add_exact(prefix, _BY_NAME[prefix], token)

    # 3. 前 2 字模糊：跳过已被精确命中过的前缀
    for prefix, token in candidates:
        if prefix in _STOP or token in _STOP:
            continue
        if len(prefix) != 2:
            continue
        if prefix in exact_prefixes:
            continue
        for code, name in _BY_PREFIX.get(prefix, []):
            if code in hits:
                continue
            hits[code] = {"code": code, "name": name, "match": token, "mode": "prefix"}

    return list(hits.values())


def similarity_key(hit):
    """两条动态的命中如果有任一相同 similarity_key，视为相似。

    用前2字做 key — 「光华sj」和「光华gf」会共享 key=「光华」，触发预警。
    """
    return hit["name"][:2]


if __name__ == "__main__":
    # 演练：从命令行接受文本，打印命中
    import sys
    text = sys.stdin.read() if not sys.stdin.isatty() else " ".join(sys.argv[1:])
    if not text:
        print("用法: echo '动态正文' | python3 stock_match.py")
        sys.exit(1)
    for h in extract_stocks(text):
        print(f"  [{h['mode']}] match={h['match']!r}  → {h['code']} {h['name']}  key={similarity_key(h)}")
