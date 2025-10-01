#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FunASR模型服务器
保持模型在内存中，通过stdin/stdout进行通信，同时提供最小CLI用于本地文件转写测试。
"""

import argparse
import json
import logging
import traceback
import signal
import tempfile
import os
import sys

# 在导入任何深度学习库之前设置环境变量
os.environ.setdefault("OMP_NUM_THREADS", "4")
# 默认使用 CPU 进行推理；如需使用 GPU，可在外部设置环境变量 FUNASR_DEVICE=cuda:0
os.environ.setdefault("FUNASR_DEVICE", "cpu")

from funasr_config import MODEL_REVISION, MODELS


# 获取日志文件路径
def get_log_path():
    """获取日志文件路径，保存到项目的 logs/ 目录"""
    # 获取项目根目录（funasr_server.py 所在目录）
    project_root = os.path.dirname(os.path.abspath(__file__))
    
    # 尝试从环境变量获取日志目录，否则使用项目目录下的 logs/
    if "ELECTRON_USER_DATA" in os.environ:
        log_dir = os.path.join(os.environ["ELECTRON_USER_DATA"], "logs")
    else:
        log_dir = os.path.join(project_root, "logs")

    # 确保日志目录存在
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "funasr_server.log")


def setup_logging(enable_console=True):
    """配置日志系统，带日志轮转功能
    
    Args:
        enable_console: 是否输出到控制台（CLI模式启用，stdin/stdout通信模式禁用）
    """
    from logging.handlers import RotatingFileHandler
    
    log_file_path = get_log_path()
    
    # 使用 RotatingFileHandler 实现日志轮转
    # maxBytes: 单个日志文件最大 10 MB
    # backupCount: 保留 3 个备份文件（总共最多 40 MB）
    file_handler = RotatingFileHandler(
        log_file_path,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=3,
        encoding="utf-8"
    )
    
    handlers = [file_handler]
    
    # 只在CLI模式下输出到控制台
    if enable_console:
        handlers.append(logging.StreamHandler(sys.stderr))  # 使用stderr避免干扰stdout
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,  # 强制重新配置
    )
    
    logger = logging.getLogger(__name__)
    logger.info(f"FunASR服务器日志文件: {log_file_path} (最大10MB，保留3个备份)")
    return logger


# 模块导入时自动初始化日志配置（CLI模式下会重新配置）
logger = setup_logging(enable_console=True)




class FunASRServer:
    def __init__(self):
        self.asr_model = None
        self.vad_model = None
        self.punc_model = None
        self.initialized = False
        self.running = True
        self.transcription_count = 0  # 转录计数器
        self.total_audio_duration = 0.0  # 总音频时长

        # 使用统一配置
        self.model_revision = MODEL_REVISION
        self.model_names = {
            "asr": MODELS["asr"]["name"],
            "vad": MODELS["vad"]["name"],
            "punc": MODELS["punc"]["name"],
        }

        self.device = self._select_device()
        logger.info(
            "FunASR服务器初始化，模型版本=%s，设备=%s",
            self.model_revision,
            self.device,
        )

        # 设置信号处理
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def __del__(self):
        """析构函数，确保释放模型资源"""
        try:
            self.cleanup()
        except Exception as e:
            logger.debug(f"析构函数清理时出错: {str(e)}")

    def cleanup(self):
        """清理所有模型和资源"""
        logger.info("开始清理 FunASR 服务器资源")
        try:
            # 清理模型引用
            self.asr_model = None
            self.vad_model = None
            self.punc_model = None
            
            # 执行最后一次内存清理
            self._cleanup_memory()
            
            logger.info("FunASR 服务器资源清理完成")
        except Exception as e:
            logger.error(f"清理 FunASR 资源时出错: {str(e)}")

    def _signal_handler(self, signum, frame):
        """处理退出信号，清理资源后正常退出"""
        logger.info(f"收到信号 {signum}，准备退出...")
        self.running = False
        try:
            self.cleanup()
        except Exception as e:
            logger.error(f"信号处理中清理资源失败: {str(e)}")
        # 正常退出
        sys.exit(0)

    def _select_device(self):
        """自动选择推理设备"""
        env_device = os.environ.get("FUNASR_DEVICE")
        if env_device:
            logger.info("使用环境变量指定的设备: %s", env_device)
            return env_device

        try:
            import torch

            if torch.cuda.is_available():
                cuda_device = "cuda:0"
                logger.info("检测到可用GPU，使用设备: %s", cuda_device)
                return cuda_device
        except Exception as e:
            logger.debug("检测GPU失败，原因: %s", str(e))

        return "cpu"

    def _load_asr_model(self):
        """加载ASR模型"""
        try:
            from funasr import AutoModel

            logger.info("开始加载ASR模型: %s", self.model_names["asr"])
            self.asr_model = AutoModel(
                model=self.model_names["asr"],
                model_revision=self.model_revision,
                device=self.device,
            )
            logger.info("ASR模型加载完成")
            return True
        except Exception as e:
            logger.error(f"ASR模型加载失败: {str(e)}")
            logger.debug(traceback.format_exc())
            self.asr_model = None
            return False

    def _load_vad_model(self):
        """加载VAD模型"""
        try:
            from funasr import AutoModel

            logger.info("开始加载VAD模型: %s", self.model_names["vad"])
            self.vad_model = AutoModel(
                model=self.model_names["vad"],
                model_revision=self.model_revision,
                device=self.device,
            )
            logger.info("VAD模型加载完成")
            return True
        except Exception as e:
            logger.error(f"VAD模型加载失败: {str(e)}")
            logger.debug(traceback.format_exc())
            self.vad_model = None
            return False

    def _load_punc_model(self):
        """加载标点恢复模型"""
        try:
            from funasr import AutoModel

            logger.info("开始加载标点恢复模型: %s", self.model_names["punc"])
            self.punc_model = AutoModel(
                model=self.model_names["punc"],
                model_revision=self.model_revision,
                device=self.device,
            )
            logger.info("标点恢复模型加载完成")
            return True
        except Exception as e:
            logger.error(f"标点恢复模型加载失败: {str(e)}")
            logger.debug(traceback.format_exc())
            self.punc_model = None
            return False

    def initialize(self):
        """并行初始化FunASR模型"""
        if self.initialized:
            return {"success": True, "message": "模型已初始化"}

        try:
            import threading
            import time

            logger.info("正在并行初始化FunASR模型...")
            start_time = time.time()

            # 创建加载结果存储
            results = {}

            def load_model_thread(model_name, load_func):
                """模型加载线程包装函数"""
                thread_start = time.time()
                results[model_name] = load_func()
                thread_time = time.time() - thread_start
                logger.info(f"{model_name}模型加载线程耗时: {thread_time:.2f}秒")

            # 创建并启动三个并行线程
            threads = [
                threading.Thread(
                    target=load_model_thread,
                    args=("asr", self._load_asr_model),
                    daemon=True,
                ),
                threading.Thread(
                    target=load_model_thread,
                    args=("vad", self._load_vad_model),
                    daemon=True,
                ),
                threading.Thread(
                    target=load_model_thread,
                    args=("punc", self._load_punc_model),
                    daemon=True,
                ),
            ]

            # 启动所有线程
            for thread in threads:
                thread.start()

            # 等待所有线程完成，设置超时
            timeout_occurred = False
            for thread in threads:
                thread.join(timeout=300)  # 5分钟超时
                if thread.is_alive():
                    timeout_occurred = True
                    logger.error("模型加载线程超时，线程仍在运行")
            
            # 检查是否有超时
            if timeout_occurred:
                return {
                    "success": False,
                    "error": "模型加载超时（超过5分钟）",
                    "type": "timeout_error",
                }

            # 检查加载结果
            failed_models = [name for name, success in results.items() if not success]

            if failed_models:
                error_msg = f"以下模型加载失败: {', '.join(failed_models)}"
                logger.error(error_msg)
                return {"success": False, "error": error_msg, "type": "init_error"}

            total_time = time.time() - start_time
            self.initialized = True
            logger.info(
                f"所有FunASR模型并行初始化完成，总耗时: {total_time:.2f}秒"
            )
            return {
                "success": True,
                "message": f"FunASR模型并行初始化成功，耗时: {total_time:.2f}秒",
            }

        except ImportError as e:
            error_msg = "FunASR未安装，请先安装FunASR: pip install funasr"
            logger.error(error_msg)
            return {"success": False, "error": error_msg, "type": "import_error"}

        except Exception as e:
            error_msg = f"FunASR模型初始化失败: {str(e)}"
            logger.error(error_msg)
            logger.error(traceback.format_exc())
            return {"success": False, "error": error_msg, "type": "init_error"}

    def transcribe_audio(self, audio_path, options=None):
        """转录音频文件"""
        if not self.initialized:
            init_result = self.initialize()
            if not init_result["success"]:
                return init_result

        try:
            # 检查音频文件是否存在
            if not os.path.exists(audio_path):
                return {"success": False, "error": f"音频文件不存在: {audio_path}"}

            logger.info(f"开始转录音频文件: {audio_path}")

            # 设置默认选项
            default_options = {
                "batch_size_s": 60,
                "hotword": "",
                "use_vad": True,
                "use_punc": True,  # 使用FunASR自带的标点恢复
                "language": "zh",
            }

            if options:
                default_options.update(options)

            # 执行语音识别
            if default_options["use_vad"] and self.vad_model:
                vad_result = self.vad_model.generate(
                    input=audio_path, batch_size_s=default_options["batch_size_s"]
                )
                logger.info("VAD处理完成")
            elif default_options["use_vad"] and not self.vad_model:
                logger.warning("use_vad=True 但VAD模型未加载，跳过VAD处理")

            # 执行ASR识别
            asr_result = self.asr_model.generate(
                input=audio_path,
                batch_size_s=default_options["batch_size_s"],
                hotword=default_options["hotword"],
                cache={},
            )

            # 提取识别文本
            if isinstance(asr_result, list) and len(asr_result) > 0:
                if isinstance(asr_result[0], dict) and "text" in asr_result[0]:
                    raw_text = asr_result[0]["text"]
                else:
                    raw_text = str(asr_result[0])
            else:
                raw_text = str(asr_result)

            logger.info(f"ASR识别完成，原始文本: {raw_text[:100]}...")

            # 使用FunASR进行标点恢复
            final_text = raw_text
            if default_options["use_punc"] and self.punc_model and raw_text.strip():
                try:
                    punc_result = self.punc_model.generate(input=raw_text)
                    if isinstance(punc_result, list) and len(punc_result) > 0:
                        if (
                            isinstance(punc_result[0], dict)
                            and "text" in punc_result[0]
                        ):
                            final_text = punc_result[0]["text"]
                        else:
                            final_text = str(punc_result[0])
                    logger.info("FunASR标点恢复完成")
                except Exception as e:
                    logger.warning(f"FunASR标点恢复失败，使用原始文本: {str(e)}")

            duration = self._get_audio_duration(audio_path)
            self.transcription_count += 1

            result = {
                "success": True,
                "text": final_text,
                "raw_text": raw_text,
                "confidence": (
                    getattr(asr_result[0], "confidence", 0.0)
                    if isinstance(asr_result, list)
                    else 0.0
                ),
                "duration": duration,
                "language": "zh-CN",
                "model_type": "pytorch",  # 标识使用的是pytorch版本
                "models": self.model_names,
            }

            # 生产环境：每10次转录后进行内存清理
            if self.transcription_count % 10 == 0:
                self._cleanup_memory()
                logger.info(f"已完成 {self.transcription_count} 次转录，执行内存清理")

            logger.info(f"转录完成，最终文本: {final_text[:100]}...")
            return result

        except Exception as e:
            error_msg = f"音频转录失败: {str(e)}"
            logger.error(error_msg)
            logger.error(traceback.format_exc())
            return {"success": False, "error": error_msg, "type": "transcription_error"}

    def _get_audio_duration(self, audio_path):
        """获取音频时长"""
        try:
            import librosa

            duration = librosa.get_duration(filename=audio_path)
            self.total_audio_duration += duration  # 累计音频时长
            return duration
        except Exception as e:
            logger.debug(f"获取音频时长失败: {str(e)}")
            return 0.0

    def _cleanup_memory(self):
        """生产环境内存清理"""
        try:
            import gc

            # 执行垃圾回收
            gc.collect()
            
            # 如果使用GPU，清理CUDA缓存
            if self.device and self.device.startswith("cuda"):
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                        # 获取当前显存使用情况
                        allocated = torch.cuda.memory_allocated() / (1024 ** 2)  # MB
                        reserved = torch.cuda.memory_reserved() / (1024 ** 2)  # MB
                        logger.info(
                            f"CUDA缓存已清理，当前显存 - 已分配: {allocated:.2f}MB, 已保留: {reserved:.2f}MB"
                        )
                except Exception as cuda_err:
                    logger.warning(f"CUDA缓存清理失败: {str(cuda_err)}")
            
            logger.info("内存清理完成")
        except Exception as e:
            logger.warning(f"内存清理失败: {str(e)}")


def _build_cli_parser():
    parser = argparse.ArgumentParser(
        description="FunASR 离线音频转写 CLI（基于 funasr_server.py）"
    )
    parser.add_argument(
        "--audio",
        "-a",
        required=True,
        help="需要转写的音频文件路径，支持 funasr 支持的格式",
    )
    parser.add_argument(
        "--no-vad",
        action="store_true",
        help="禁用 FunASR VAD 处理（默认启用）",
    )
    parser.add_argument(
        "--no-punc",
        action="store_true",
        help="禁用 FunASR 标点恢复（默认启用）",
    )
    parser.add_argument(
        "--language",
        "-l",
        help="识别语言代码，例如 zh、en、auto 等，默认使用服务器内置配置",
    )
    parser.add_argument(
        "--hotword",
        help="识别时使用的热词字符串",
    )
    parser.add_argument(
        "--batch-size-s",
        type=float,
        help="动态 batch 总时长（秒），默认 60",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="使用缩进格式输出 JSON 结果",
    )
    return parser


def main():
    parser = _build_cli_parser()
    args = parser.parse_args()

    global logger
    logger = setup_logging(enable_console=True)

    server = FunASRServer()
    init_result = server.initialize()
    success = init_result.get("success", False)

    indent = 2 if args.pretty else None

    if not success:
        print(json.dumps(init_result, ensure_ascii=False, indent=indent))
        raise SystemExit(1)

    options = {}
    if args.no_vad:
        options["use_vad"] = False
    if args.no_punc:
        options["use_punc"] = False
    if args.language:
        options["language"] = args.language
    if args.hotword:
        options["hotword"] = args.hotword
    if args.batch_size_s is not None:
        options["batch_size_s"] = args.batch_size_s

    result = server.transcribe_audio(args.audio, options=options)
    print(json.dumps(result, ensure_ascii=False, indent=indent))

    if not result.get("success", False):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
