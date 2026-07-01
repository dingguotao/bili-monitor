# bili-monitor

监控 B 站指定 UP 主的动态更新，推送到飞书群。支持跨 UP 的股票预警聚合。

## 功能

- 定时抓取 B 站 UP 主动态（高频时段 2 分钟一次，其他时段 1 小时一次）
- 新动态推送到飞书（interactive 卡片，自动分批）
- 从动态正文中识别 A 股股票名称，48 小时内不同 UP 提到同一只股票触发跨 UP 预警
- 状态持久化（已推送 ID、股票命中记录、已触发预警对），重启不重复推

## 目录结构

```
bili-monitor/
├── config.yaml              # 配置（含真实凭证，git 忽略）
├── config.example.yaml      # 配置模板
├── config_loader.py         # 配置加载器
├── bili_fetch.py            # B 站 API 客户端
├── monitor.py               # 主流程（抓取 → 匹配 → 推送）
├── scheduler.py             # 内置调度器（含高低频切换）
├── stock_match.py           # 股票名匹配
├── stock_dict.json          # A 股名称词典
├── stopwords.json           # 停用词
├── run.sh                   # 一键启动脚本
├── memory/                  # 运行时状态（git 忽略）
└── data/                    # 日志（git 忽略）
```

## 快速开始

1. 复制配置模板并填入真实凭证：
   ```bash
   cp config.example.yaml config.yaml
   # 编辑 config.yaml，填入 bili_cookie、feishu_webhook 和 monitored_ups
   ```

2. 首次运行前初始化（把当前动态记为已读，避免刷屏）：
   ```bash
   python3 monitor.py --init
   ```

3. 启动调度器：
   ```bash
   ./run.sh
   # 或直接：python3 scheduler.py
   ```

## 添加 / 修改监控的 UP 主

编辑 `config.yaml` 中的 `monitored_ups` 数组：

```yaml
monitored_ups:
  - name: UP主名称
    uid: "12345678"
    enabled: true
```

新增 UP 后建议再跑一次 `python3 monitor.py --init` 只初始化新 UP 的游标。

## 配置项说明

见 `config.example.yaml` 的字段名（见名知义）。主要字段：

- `bili_cookie` / `feishu_webhook` — 认证凭证
- `bilibili.*` — B 站 API 请求参数
- `feishu.*` — 飞书消息拆分与卡片主题
- `monitor.*` — 状态文件路径、股票聚合窗口
- `scheduler.*` — 高低频切换时段与间隔
- `monitored_ups` — 待监控的 UP 主列表

## 状态文件

- `memory/last_seen.json` — 每个 UP 上次推送的最新动态 ID
- `memory/recent_stocks.jsonl` — 近期股票命中日志（48h 滚动窗口）
- `memory/alerted_pairs.json` — 已触发的跨 UP 预警去重

删除 `memory/` 会让下次运行把当前动态全部当"新的"推一遍，慎重。
