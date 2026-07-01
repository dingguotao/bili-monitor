#!/usr/bin/env python3
"""配置加载模块 —— 从 config.yaml 读出配置项供各脚本使用。

用法:
    from config_loader import config

    cookie = config.get('bili_cookie')
    timeout = config.get('bilibili.request_timeout_seconds')
    memory_dir = config.get_path('monitor.memory_dir')
    ups = config.get_enabled_ups()
"""

import os
import yaml

_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_FILE = os.path.join(_DIR, "config.yaml")


class Config:
    """点式路径读取 YAML 配置的小工具。"""

    def __init__(self, config_file=None):
        self.config_file = config_file or _CONFIG_FILE
        with open(self.config_file, encoding="utf-8") as f:
            self._data = yaml.safe_load(f)

    def get(self, key_path, default=None):
        """按 'a.b.c' 形式取值，路径任一段缺失返回 default。"""
        value = self._data
        for key in key_path.split("."):
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value

    def get_path(self, key_path):
        """把配置里的相对路径拼成基于脚本目录的绝对路径。"""
        rel = self.get(key_path)
        if rel is None:
            raise KeyError(f"配置项 '{key_path}' 不存在")
        return os.path.join(_DIR, rel)

    def get_enabled_ups(self):
        """返回 enabled != false 的 UP 主列表。"""
        return [t for t in self.get("monitored_ups", []) if t.get("enabled", True)]


# 全局单例
config = Config()


if __name__ == "__main__":
    print(f"cookie 长度: {len(config.get('bili_cookie'))}")
    print(f"webhook: {config.get('feishu_webhook')}")
    print(f"B站请求超时: {config.get('bilibili.request_timeout_seconds')}s")
    print(f"高频间隔: {config.get('scheduler.high_freq_interval_seconds')}s")
    print(f"低频间隔: {config.get('scheduler.low_freq_interval_seconds')}s")
    print(f"memory_dir: {config.get_path('monitor.memory_dir')}")
    print(f"\n启用 UP {len(config.get_enabled_ups())} 个:")
    for t in config.get_enabled_ups():
        print(f"  - {t['name']} ({t['uid']}) · {t['topic']}")
