# -*- coding: utf-8 -*-
"""插件全局常量。

版本号从 metadata.yaml 运行时读取（单一数据源），避免多处硬编码导致漂移。
读取失败时回退占位版本并记录警告。
"""
import os
from astrbot.api import logger

PLUGIN_NAME = "astrbot_plugin_group_guardian"


def _load_version_from_metadata() -> str:
    """从 metadata.yaml 读取版本号。失败返回 'v0.0.0' 并记录警告。"""
    try:
        # metadata.yaml 与本文件同目录
        meta_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata.yaml")
        if not os.path.exists(meta_path):
            logger.warning("[GroupMgr] metadata.yaml 不存在，版本号回退 v0.0.0")
            return "v0.0.0"
        with open(meta_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                # 匹配 "version: v1.2.3" 行（兼容带/不带引号）
                if line.startswith("version:"):
                    val = line.split(":", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
        logger.warning("[GroupMgr] metadata.yaml 未找到 version 字段，回退 v0.0.0")
        return "v0.0.0"
    except Exception as e:
        logger.warning(f"[GroupMgr] 读取 metadata.yaml 版本号失败: {e}，回退 v0.0.0")
        return "v0.0.0"


PLUGIN_VERSION = _load_version_from_metadata()
