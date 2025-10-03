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
import os
import sys
import warnings

# 过滤掉 jieba 的 pkg_resources 弃用警告
warnings.filterwarnings("ignore", category=UserWarning, module="jieba._compat")

# 在导入任何深度学习库之前设置环境变量
os.environ.setdefault("OMP_NUM_THREADS", "8")  # ONNX 推理并行线程数，可提升速度
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
            # 清理模型引用（ONNX 的 InferenceSession 会在对象销毁时自动释放）
            if self.asr_model is not None:
                logger.debug("释放 ASR 模型")
                self.asr_model = None
            
            if self.vad_model is not None:
                logger.debug("释放 VAD 模型")
                self.vad_model = None
            
            if self.punc_model is not None:
                logger.debug("释放标点模型")
                self.punc_model = None
            
            # 执行最后一次内存清理（包括 gc.collect 强制回收）
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

        # try:
        #     import torch
        #
        #     if torch.cuda.is_available():
        #         cuda_device = "cuda:0"
        #         logger.info("检测到可用GPU，使用设备: %s", cuda_device)
        #         return cuda_device
        # except Exception as e:
        #     logger.debug("检测GPU失败，原因: %s", str(e))

        return "cpu"

    def _load_asr_model(self):
        """加载ASR模型"""
        try:
            model_name_lower = str(self.model_names["asr"]).lower()
            
            # 如果是 ONNX 模型，使用 funasr_onnx 专用加载器
            if "onnx" in model_name_lower:
                from funasr_onnx.paraformer_bin import Paraformer

                logger.info("开始加载ASR ONNX模型: %s", self.model_names["asr"])

                # 统一通过 modelscope 指定修订版本下载到本地路径，避免 funasr_onnx 触发导出分支
                try:
                    from modelscope.hub.snapshot_download import snapshot_download
                    model_dir = snapshot_download(
                        self.model_names["asr"],
                        revision=self.model_revision,
                    )
                except Exception as e:
                    logger.error("下载 ASR ONNX 模型失败: %s", e)
                    return False

                # 基本完整性校验，优先使用量化模型
                quant_file = os.path.join(model_dir, "model_quant.onnx")
                base_file = os.path.join(model_dir, "model.onnx")
                use_quantize = False
                if os.path.exists(quant_file):
                    use_quantize = True
                elif not os.path.exists(base_file):
                    logger.error("ASR 模型目录缺少 model.onnx: %s", model_dir)
                    return False

                device_id = -1  # CPU
                if self.device and "cuda" in self.device:
                    try:
                        device_id = int(self.device.split(":")[-1])
                    except Exception:
                        device_id = 0
                
                # 性能优化参数
                num_threads = int(os.environ.get("OMP_NUM_THREADS", "8"))

                self.asr_model = Paraformer(
                    str(model_dir),
                    batch_size=1,
                    device_id=device_id,
                    quantize=use_quantize,
                    intra_op_num_threads=num_threads,  # 线程并行加速
                )
                logger.info("ASR ONNX模型加载完成")
                return True
            else:
                logger.error("仅支持 ONNX 模型加载，当前模型名称: %s", self.model_names["asr"]) 
                return False
                
        except Exception as e:
            logger.error(f"ASR模型加载失败: {str(e)}")
            logger.debug(traceback.format_exc())
            self.asr_model = None
            return False

    def _load_vad_model(self):
        """加载VAD模型（使用 funasr_onnx 专用加载器）"""
        try:
            from funasr_onnx.vad_bin import Fsmn_vad

            logger.info("开始加载VAD ONNX模型: %s", self.model_names["vad"])

            # 统一通过 modelscope 指定修订版本下载到本地路径
            try:
                from modelscope.hub.snapshot_download import snapshot_download
                model_dir = snapshot_download(
                    self.model_names["vad"],
                    revision=self.model_revision,
                )
            except Exception as e:
                logger.error("下载 VAD ONNX 模型失败: %s", e)
                return False

            quant_file = os.path.join(model_dir, "model_quant.onnx")
            base_file = os.path.join(model_dir, "model.onnx")
            use_quantize = False
            if os.path.exists(quant_file):
                use_quantize = True
            elif not os.path.exists(base_file):
                logger.error("VAD 模型目录缺少 model.onnx: %s", model_dir)
                return False

            device_id = -1  # CPU
            if self.device and "cuda" in self.device:
                try:
                    device_id = int(self.device.split(":")[-1])
                except Exception:
                    device_id = 0
            
            num_threads = int(os.environ.get("OMP_NUM_THREADS", "8"))

            self.vad_model = Fsmn_vad(
                str(model_dir),
                batch_size=1,
                device_id=device_id,
                quantize=use_quantize,
                intra_op_num_threads=num_threads,
            )
            logger.info("VAD ONNX模型加载完成")
            return True
        except Exception as e:
            logger.error(f"VAD模型加载失败: {str(e)}")
            logger.debug(traceback.format_exc())
            self.vad_model = None
            return False

    def _load_punc_model(self):
        """加载标点恢复模型（使用 funasr_onnx 专用加载器）"""
        try:
            from funasr_onnx.punc_bin import CT_Transformer

            logger.info("开始加载标点恢复 ONNX模型: %s", self.model_names["punc"])

            # 统一通过 modelscope 指定修订版本下载到本地路径
            try:
                from modelscope.hub.snapshot_download import snapshot_download
                model_dir = snapshot_download(
                    self.model_names["punc"],
                    revision=self.model_revision,
                )
            except Exception as e:
                logger.error("下载 标点 ONNX 模型失败: %s", e)
                return False

            quant_file = os.path.join(model_dir, "model_quant.onnx")
            base_file = os.path.join(model_dir, "model.onnx")
            use_quantize = False
            if os.path.exists(quant_file):
                use_quantize = True
            elif not os.path.exists(base_file):
                logger.error("标点模型目录缺少 model.onnx: %s", model_dir)
                return False

            device_id = -1  # CPU
            if self.device and "cuda" in self.device:
                try:
                    device_id = int(self.device.split(":")[-1])
                except Exception:
                    device_id = 0
            
            num_threads = int(os.environ.get("OMP_NUM_THREADS", "8"))

            self.punc_model = CT_Transformer(
                str(model_dir),
                batch_size=1,
                device_id=device_id,
                quantize=use_quantize,
                intra_op_num_threads=num_threads,
            )
            logger.info("标点恢复 ONNX模型加载完成")
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

            # 预导入 funasr_onnx 子模块，避免多线程导入导致的 ModuleLock 死锁
            try:
                import importlib
                pre_modules = (
                    "funasr_onnx.utils.utils",
                    "funasr_onnx.utils.frontend",
                    "funasr_onnx.paraformer_bin",
                    "funasr_onnx.vad_bin",
                    "funasr_onnx.punc_bin",
                )
                for m in pre_modules:
                    importlib.import_module(m)
                logger.info("funasr_onnx 模块预导入完成")
            except Exception as pre_e:
                logger.warning("funasr_onnx 预导入失败: %s", str(pre_e))

            # 创建加载结果存储
            results = {}

            def load_model_thread(model_name, load_func):
                """模型加载线程包装函数"""
                thread_start = time.time()
                results[model_name] = load_func()
                thread_time = time.time() - thread_start
                logger.info(f"{model_name}模型加载线程耗时: {thread_time:.2f}秒")

            # 根据开关决定是否加载 VAD / PUNC（默认启用）
            load_vad = os.environ.get("FUNASR_USE_VAD", "true").lower() not in ("0", "false", "no")
            load_punc = os.environ.get("FUNASR_USE_PUNC", "true").lower() not in ("0", "false", "no")

            # 创建并启动线程（ASR 必须，VAD/PUNC 可选）
            threads = [
                threading.Thread(
                    target=load_model_thread,
                    args=("asr", self._load_asr_model),
                    daemon=True,
                )
            ]
            if load_vad:
                threads.append(
                    threading.Thread(
                        target=load_model_thread,
                        args=("vad", self._load_vad_model),
                        daemon=True,
                    )
                )
            if load_punc:
                threads.append(
                    threading.Thread(
                        target=load_model_thread,
                        args=("punc", self._load_punc_model),
                        daemon=True,
                    )
                )

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
                # 默认启用 VAD / PUNC，可在外部通过选项或环境变量关闭
                "use_vad": os.environ.get("FUNASR_USE_VAD", "true").lower() not in ("0", "false", "no"),
                "use_punc": os.environ.get("FUNASR_USE_PUNC", "true").lower() not in ("0", "false", "no"),
                "language": "zh",
            }

            if options:
                default_options.update(options)

            # 执行语音识别（VAD 处理）
            if default_options["use_vad"] and self.vad_model:
                # funasr_onnx.Fsmn_vad 直接调用，返回 segments [[start_ms, end_ms], ...]
                vad_result = self.vad_model(audio_path)
                logger.info("VAD处理完成，检测到 %s 个语音段", len(vad_result[0]) if vad_result else 0)
            elif default_options["use_vad"] and not self.vad_model:
                logger.warning("use_vad=True 但VAD模型未加载，跳过VAD处理")

            # 执行ASR识别（根据模型类型使用不同接口）
            if hasattr(self.asr_model, "generate"):
                # PyTorch 模型使用 generate 方法
                asr_result = self.asr_model.generate(
                    input=audio_path,
                    batch_size_s=default_options["batch_size_s"],
                    hotword=default_options["hotword"],
                    cache={},
                )
            else:
                # ONNX 模型直接调用（funasr_onnx.Paraformer）
                asr_result = self.asr_model([audio_path])

            # 提取识别文本（兼容 PyTorch 和 ONNX 两种格式）
            if isinstance(asr_result, list) and len(asr_result) > 0:
                first_item = asr_result[0]
                # PyTorch 格式: [{"text": "..."}]
                if isinstance(first_item, dict) and "text" in first_item:
                    raw_text = first_item["text"]
                # ONNX 格式: [{"preds": (text_string, token_list)}]
                elif isinstance(first_item, dict) and "preds" in first_item:
                    preds = first_item["preds"]
                    if isinstance(preds, tuple) and len(preds) > 0:
                        raw_text = str(preds[0])
                    else:
                        raw_text = str(preds)
                else:
                    raw_text = str(first_item)
            else:
                raw_text = str(asr_result)

            logger.info(f"ASR识别完成，原始文本: {raw_text[:100]}...")

            # 使用标点恢复（ONNX 的 CT_Transformer 直接调用）
            final_text = raw_text
            if default_options["use_punc"] and self.punc_model and raw_text.strip():
                try:
                    # funasr_onnx.CT_Transformer 返回 (text_with_punc, punc_list)
                    punc_result = self.punc_model(raw_text)
                    if isinstance(punc_result, tuple) and len(punc_result) > 0:
                        final_text = str(punc_result[0])
                    else:
                        final_text = str(punc_result)
                    logger.info("标点恢复完成")
                except Exception as e:
                    logger.warning(f"标点恢复失败，使用原始文本: {str(e)}")

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
                "model_type": (
                    "onnx" if "onnx" in str(self.model_names.get("asr", "")).lower() else "pytorch"
                ),
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

            duration = librosa.get_duration(path=audio_path)
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
            # if self.device and self.device.startswith("cuda"):
            #     try:
            #         import torch
            #         if torch.cuda.is_available():
            #             torch.cuda.empty_cache()
            #             # 获取当前显存使用情况
            #             allocated = torch.cuda.memory_allocated() / (1024 ** 2)  # MB
            #             reserved = torch.cuda.memory_reserved() / (1024 ** 2)  # MB
            #             logger.info(
            #                 f"CUDA缓存已清理，当前显存 - 已分配: {allocated:.2f}MB, 已保留: {reserved:.2f}MB"
            #             )
            #     except Exception as cuda_err:
            #         logger.warning(f"CUDA缓存清理失败: {str(cuda_err)}")
            
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
