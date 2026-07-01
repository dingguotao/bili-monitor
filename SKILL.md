# B站 UP 主动态监控 Skill

监控 `config.yaml` 中配置的多个 B 站 UP 主的最新动态，对每个目标分别去重、整理后通过飞书机器人推送到群，同时做 48h 跨 UP 股票命中预警。

---

## 工作目录

本 skill 的工作目录为 `bili-monitor/`，以下所有路径均相对于该目录。执行命令前需确保 cwd 位于此目录：

```bash
cd bili-monitor
```

## 核心文件

| 组件 | 相对路径 | 用途 |
|------|----------|------|
| 主程序入口 | `monitor.py` | 抓取→去重→推送→标记→跨 UP 预警 一体化 |
| 数据获取脚本 | `bili_fetch.py` | 接收 UID 参数，调 B 站 API 输出动态 JSON |
| 股票提取模块 | `stock_match.py` | 从动态正文里识别股票名（词典+模糊匹配+stopwords） |
| 配置加载器 | `config_loader.py` | 读 `config.yaml`，提供全局 `config` 单例 |
| **总配置文件** | `config.yaml` | 认证、UP 列表、飞书阈值、调度频率等全部配置 |
| 调度守护 | `scheduler.py` | 高低频轮询守护进程 |
| 服务脚本 | `run.sh` | `start / stop / restart / status / fg / logs` |
| A 股词典 | `stock_dict.json` | 5200+ 只 A 股代码+名称 |
| 干扰词 | `stopwords.json` | 误报过滤词典，发现误报后追加 |
| 去重状态 | `memory/bili_sent_ids_<uid>.json` | 每个 UP 一份，记录已推送的动态 ID |
| 股票滚动记录 | `memory/recent_stocks.jsonl` | 48h 滚动窗口，记录每条动态命中的股票 |
| 预警去重 | `memory/alerted_pairs.json` | 同一对 (股票key, UP组) 48h 内只警报一次 |
| 错误日志 | `data/bili-monitor-error.log` | 记录抓取失败等异常 |
| 调度日志 | `data/scheduler.log` | 调度守护进程日志 |

---

## config.yaml 结构

顶层键：

- `bili_cookie`：B 站登录 cookie 字符串（**直接写内容**，不是文件路径）
- `feishu_webhook`：飞书机器人 webhook URL
- `bilibili`：API features、超时、UA
- `feishu`：消息体阈值、卡片主题色
- `monitor`：去重 ID 保留数、股票匹配窗口秒数、日志/状态文件路径
- `scheduler`：高低频间隔、高频时段、超时
- `stock_match`：词典和 stopwords 文件路径
- `monitored_ups`：要监控的 UP 主列表

## 当前监控目标

**唯一数据源：`config.yaml` 的 `monitored_ups`。** 任何时候回答"监控了哪些 UP"、"多少个启用"之类的问题，必须直接读 `config.yaml`。

- 仅处理 `enabled: true` 的条目（缺省视为 true）
- 每条记录字段：`name` / `uid` / `topic` / `focus` / `enabled`
- 空间 URL 拼接规则：`https://space.bilibili.com/<uid>`

快速查看当前启用列表：

```bash
cd bili-monitor
python3 -c "from config_loader import config; [print(f\"{u['name']} ({u['uid']}) - {u['topic']}\") for u in config.get_enabled_ups()]"
```

---

## 定时任务

生产环境下由 `scheduler.py`（`run.sh start` 拉起）自动调度：工作日 09:00-14:59 每 2 分钟一次，其它时间每 60 分钟一次。间隔来自 `scheduler.high_freq_interval_seconds` / `scheduler.low_freq_interval_seconds`。

`monitor.py` 单次行为：

1. 从 `config.yaml` 读 `monitored_ups`，仅处理 `enabled: true`
2. 对每个 UP：读 `memory/bili_sent_ids_<uid>.json`，调 `bili_fetch.fetch_dynamics()`，比对去重
3. 全部为空 → 静默退出
4. 有新动态 → 组装飞书交互卡片，POST 到 `config.feishu_webhook`
5. **每批推送成功后**立刻更新对应 `bili_sent_ids_<uid>.json`（避免部分成功丢数据）
6. 对每条新动态调 `stock_match.extract_stocks` 提取股票名，追加到 `memory/recent_stocks.jsonl`（48h 滚动）
7. 扫描整个 `recent_stocks.jsonl`：若同一股票 key（前 2 字）有 ≥ 2 个不同 UP 提到 → 触发**跨 UP 股票预警**
8. 预警去重：同一组合 48h 内只警报一次，状态存 `memory/alerted_pairs.json`

---

## 手动触发

```bash
cd bili-monitor
python3 monitor.py                # 正常一次
python3 monitor.py --force        # 忽略去重，每个 UP 推最新一条
python3 monitor.py --dry-run      # 演练：打印 payload 不发飞书，也不写入状态
python3 monitor.py --force --dry-run   # 强推 + 演练
python3 monitor.py --init         # 把当前所有动态视为已推送，不发消息（新增 UP 第一次跑用）
```

### 新增 UP 后的标准流程

1. 在 `config.yaml` 的 `monitored_ups` 数组追加条目
2. 立即跑 `python3 monitor.py --init`，把这个 UP 当前的历史动态全部标为已推送
3. 等下一轮定时任务自然触发

跳过步骤 2 直接跑 `monitor.py`，会把该 UP 当前所有历史动态当成新的全推一遍。

---

## 推送格式

飞书交互卡片，`msg_type: interactive`，标题栏颜色取自 `config.feishu.card_theme.push`（动态）/ `card_theme.alert`（预警），markdown 正文按 UP 分段。

### 限制（飞书官方）

| 项 | 值 | 对应配置项 |
|----|----|-----------|
| 请求体大小 | ≤ 20 KB | `feishu.message_max_bytes` |
| 单批阈值 | 18 KB（预留 2 KB 抖动余量） | `feishu.batch_max_bytes` |
| 频率 | 100/min，5/s（单租户单机器人） | — |

超过单批阈值会自动切成多批发送，标题带 "第 x/y 批"。

### 常见返回码

- `0` 成功
- `9499` 请求体格式错误
- `19021` 签名校验失败
- `11232` 触发限流

---

## 数据去重规则

- 每个 UP 主一份独立状态文件：`memory/bili_sent_ids_<uid>.json`
- 结构：JSON 数组，只存已推送的 `id_str`
- 写入时保留最近 `monitor.keep_recent_sent_ids`（默认 200）条
- 文件不存在时视为空数组 `[]`

---

## 错误处理

| 情况 | 处理 |
|------|------|
| 某个 UP 抓取失败 | 写入 error.log，跳过该 UP，其他 UP 继续 |
| API 返回 code≠0（如 cookie 失效） | 写入 error.log，跳过该 UP |
| 飞书 webhook 返回非 0 | 写入 error.log，附带 code/msg；不重试 |
| 飞书 webhook 返回 11232（限流） | 写入 error.log，本轮放弃，下轮再试 |
| 状态文件损坏 | 重新初始化为 `[]`（可能导致重复推送，可接受） |

---

## 新增监控目标

当用户说「再加个 UP 主，id 是 xxx」或类似需求时，按以下步骤执行：

### 步骤 1：验证 UID 有效性并获取昵称

```bash
cd bili-monitor
python3 bili_fetch.py <新UID> 0
```

- 返回 `[]` 或报错 `API错误`：UID 无效或该 UP 没有动态，向用户确认
- 返回正常 JSON 数组：UID 有效，继续下一步

获取 UP 昵称（从 API 抓 `module_author.name`）：

```bash
python3 -c "
import bili_fetch
data = bili_fetch.fetch_dynamics('<新UID>')
items = data['data']['items']
print(items[0]['modules']['module_author']['name'] if items else 'no items')
"
```

### 步骤 2：根据动态内容判断主题分类

观察上一步抓到的动态正文，归类到合适的 `topic` 和 `focus`，参考现有 UP 风格：

| 关键词出现 | topic | focus |
|-----------|-------|-------|
| 复盘 / 涨停板 / 龙虎榜 | `复盘 / 股票` | `复盘要点、提到的股票和操作建议` |
| 短线 / 龙头 / 尾盘 / 次日 | `A股 / 短线龙头` | `提到的股票、龙头机会和尾盘/次日操作建议` |
| 个股分析 / 买入 / 关注 / 止盈 | `A股 / 股票分析` | `股票名称和推荐意见（买入、关注、止盈、离场等）` |
| 其他 | 自行总结 | 自行总结 |

判断不准时向用户确认。

### 步骤 3：追加到 config.yaml

用 Edit 工具在 `monitored_ups` 数组末尾追加：

```yaml
  - name: <昵称>
    uid: "<UID 字符串>"
    topic: <主题分类>
    focus: <整理时重点提取什么内容>
    enabled: true
```

`uid` 必须用字符串（带引号），因为大 UID 超过 JS 安全整数范围。

### 步骤 4：初始化并向用户确认

```bash
python3 monitor.py --init
```

回复格式：

```
已加入「<昵称>」（UID: <UID>），归类为 <topic>。
当前监控目标共 <N> 个，其中启用 <M> 个。
下次定时任务会自动覆盖。
```

---

### 临时停用 / 删除

- **临时停用**：把 `enabled` 改为 `false`，**不要删除条目**，保留历史 `memory/bili_sent_ids_<uid>.json` 避免下次启用时重复推送
- **彻底删除**：从 `config.yaml` 移除条目，同时删除对应的 `memory/bili_sent_ids_<uid>.json`

---

## 文件结构

```
bili-monitor/
├── SKILL.md                              ← 本文件
├── config.yaml                           ← 总配置（认证 / UP 列表 / 阈值）
├── config_loader.py                      ← 配置加载器
├── monitor.py                            ← 主入口（抓取→去重→推送→预警）
├── bili_fetch.py                         ← B 站 API 客户端
├── stock_match.py                        ← 股票提取与相似检测
├── scheduler.py                          ← 自动调度守护进程
├── run.sh                                ← start/stop/restart/status/fg/logs
├── stock_dict.json                       ← A 股全量词典
├── stopwords.json                        ← 干扰词过滤表
├── memory/
│   ├── bili_sent_ids_<uid>.json          ← 每个 UP 已推送动态 ID
│   ├── recent_stocks.jsonl               ← 48h 股票命中滚动记录
│   └── alerted_pairs.json                ← 已发预警组合去重
└── data/
    ├── bili-monitor-error.log            ← 错误日志
    └── scheduler.log                     ← 调度守护日志
```

---

## 跨 UP 股票预警机制

| 维度 | 实现 |
|------|------|
| 词典 | `stock_dict.json` 含 5200+ A 股股票名+代码 |
| 提取规则 | 滑动窗口取连续中文 2-4 字，加可选拼音后缀；前 2 字索引模糊匹配；3-4 字完整名精确匹配 |
| 相似 key | 用股票名前 2 字（`similarity_key`），「光华sj」「光华gf」「光华科技」共享 key `光华` |
| 时间窗口 | `monitor.stock_match_window_seconds`（默认 48h） |
| 触发条件 | 同一 key 在窗口内被 ≥ 2 个不同 UP 提到 |
| 推送形式 | 独立一条「🚨 跨 UP 股票预警」飞书卡片，红色标题栏，列出每个 UP 的命中词和原文链接 |
| 去重 | `(key, UP集合)` 组合 48h 内只推一次，避免反复打扰 |

### 误报处理

发现某词被误识别为股票（如「兄弟们」→「兄弟科技」），向 `stopwords.json` 追加该词即可。

### 词典刷新

A 股每周有新股上市/退市，建议每月跑一次词典更新：

```bash
cd bili-monitor
python3 -c "
import json, urllib.request
all_stocks = {}
def fetch(node):
    page = 1
    while True:
        url = f'http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page={page}&num=80&sort=symbol&asc=1&node={node}'
        req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0','Referer':'https://finance.sina.com.cn/'})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                arr = json.loads(r.read().decode('gbk',errors='ignore'))
        except Exception: break
        if not arr: break
        for x in arr:
            if x.get('code') and x.get('name'):
                all_stocks[x['code']] = x['name']
        if len(arr) < 80: break
        page += 1
for n in ['sh_a','sz_a','cyb','kcb']: fetch(n)
stocks = [{'code':c,'name':n} for c,n in sorted(all_stocks.items())]
json.dump(stocks, open('stock_dict.json','w',encoding='utf-8'), ensure_ascii=False, separators=(',',':'))
print(f'更新完毕，{len(stocks)} 只股票')
"
```
