#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FunASR模型配置
统一管理模型名称、版本等配置
"""

import os

# 模型版本，可通过环境变量覆盖
MODEL_REVISION = os.environ.get("FUNASR_MODEL_REVISION", "v2.0.4")

# 模型配置
MODELS = {
    "asr": {
        "name": os.environ.get(
            "FUNASR_ASR_MODEL",
            "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        ),
        "type": "asr",
    },
    "vad": {
        "name": os.environ.get(
            "FUNASR_VAD_MODEL",
            "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
        ),
        "type": "vad",
    },
    "punc": {
        "name": os.environ.get(
            "FUNASR_PUNC_MODEL",
            "iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
        ),
        "type": "punc",
    },
}

# 获取模型列表（用于下载脚本）
def get_models_for_download():
    """返回用于下载的模型配置列表"""
    return [
        {
            "name": MODELS["asr"]["name"],
            "type": "asr",
        },
        {
            "name": MODELS["vad"]["name"],
            "type": "vad",
        },
        {
            "name": MODELS["punc"]["name"],
            "type": "punc",
        },
    ]

