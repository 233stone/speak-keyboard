"""基于 JSON 行的桥接，将 TranscriptionWorker 以标准输入/输出暴露给外部调用方。

This module keeps the existing recording/transcription pipeline intact but
drives it through a simple stdin/stdout protocol. Each incoming line on stdin
must be a JSON object with a mandatory "cmd" field. Responses and events are
emitted as JSON objects (one per line) on stdout.

Supported commands::

    {"cmd": "start"}
    {"cmd": "stop"}
    {"cmd": "stats"}
    {"cmd": "shutdown"}

Any unrecognised command results in an ``invalid_command`` event. All events
include a ``timestamp`` (seconds since epoch) and a ``event`` field to help the
caller dispatch them appropriately.

The bridge applies the configured post-processing, optional dataset capture and
continues to inject text to the focused window just like the CLI entry point.

Run this script from the project root with Python 3.9+::

    python -u -m app.bridge [--config path] [--save-dataset]

stdout/stderr are deliberately separated: stdout carries JSON events while
stderr receives human-readable logs (via the logging module).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from typing import Any, Dict, Optional

# 提前在导入 app 之前分流 stdout：
# - 保存原始 stdout 用于事件输出
# - 将 stdout 重定向到 stderr，避免第三方库将日志写入事件通道
_EVENT_OUT = sys.stdout
try:
    sys.stdout = sys.stderr
except Exception:
    pass

if __package__ in (None, ""):
    # 允许以 "python app/bridge.py" 运行：注入项目根目录
    import os
    import pathlib

    sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

from app import TranscriptionResult, TranscriptionWorker, load_config, type_text
from app.plugins.dataset_recorder import wrap_result_handler


logger = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    """Configure root logging level without adding duplicate handlers."""

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not root_logger.handlers:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root_logger.addHandler(console_handler)


def _now_ts() -> float:
    return time.time()


class BridgeApp:
    """Manage a TranscriptionWorker and expose it via stdin/stdout commands."""

    def __init__(
        self,
        config: Dict[str, Any],
        config_path: Optional[str],
        save_dataset: bool,
        dataset_dir: str,
    ) -> None:
        self._stdout_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._is_recording = False
        self._shutdown = False

        self.config = config
        self.worker = TranscriptionWorker(config_path=config_path, on_result=None)

        # 事件输出通道：
        # - 保存原始 stdout 用于 JSON 事件
        # - 将 sys.stdout 重定向到 stderr，避免第三方库把日志写到事件通道
        self._event_out = _EVENT_OUT

        output_cfg = self.worker.config.get("output", {})
        output_method = output_cfg.get("method", "auto")
        append_newline = output_cfg.get("append_newline", False)

        handler = self._build_result_handler(output_method, append_newline)
        if save_dataset:
            handler = wrap_result_handler(handler, self.worker, dataset_dir)

        self.worker.on_result = handler
        self._save_dataset = save_dataset
        self._dataset_dir = dataset_dir

    # ------------------------------------------------------------------
    # Event helpers
    def emit_event(self, event: str, **data: Any) -> None:
        payload = {
            "event": event,
            "timestamp": _now_ts(),
            **data,
        }
        line = json.dumps(payload, ensure_ascii=False)
        with self._stdout_lock:
            try:
                # 直接写入原始 stdout，确保仅事件走此通道
                self._event_out.write(line + "\n")
                self._event_out.flush()
            except OSError as exc:
                # 管道可能已关闭（例如父进程退出或关闭句柄），忽略即可
                logger.debug("写入事件失败（可能是管道已关闭）: %s", exc)
            except Exception as exc:  # noqa: BLE001
                logger.debug("写入事件异常: %s", exc)

    def _emit_stats(self, event: str = "stats") -> None:
        self.emit_event(event, stats=self.worker.transcription_stats)

    def _build_result_handler(self, output_method: str, append_newline: bool):
        def _handle_result(result: TranscriptionResult) -> None:
            stats = self.worker.transcription_stats
            if result.error:
                logger.error("转写失败: %s", result.error)
                self.emit_event(
                    "transcription_error",
                    error=result.error,
                    stats=stats,
                )
                return

            try:
                type_text(result.text, append_newline=append_newline, method=output_method)
            except Exception as exc:  # noqa: BLE001
                logger.error("输出文本失败: %s", exc, exc_info=True)
                self.emit_event(
                    "output_error",
                    error=str(exc),
                    stats=stats,
                )

            self.emit_event(
                "transcription_result",
                text=result.text,
                raw_text=result.raw_text,
                duration=result.duration,
                inference_latency=result.inference_latency,
                confidence=result.confidence,
                corrections=getattr(result, "corrections", 0),
                stats=stats,
            )

        return _handle_result

    # ------------------------------------------------------------------
    # Command handlers
    def handle_start(self) -> None:
        logger.info("[bridge] 收到 start 命令，准备启动录音")
        with self._state_lock:
            if self._is_recording:
                logger.debug("[bridge] 收到 start 命令，但已经在录音，跳过")
                self.emit_event(
                    "recording_state",
                    is_recording=True,
                    skipped="already_recording",
                    stats=self.worker.transcription_stats,
                )
                return

        try:
            self.worker.start()
        except Exception as exc:  # noqa: BLE001
            logger.error("[bridge] 启动录音失败: %s", exc, exc_info=True)
            self.emit_event("recording_error", message=str(exc))
            return

        with self._state_lock:
            self._is_recording = True

        self.emit_event(
            "recording_state",
            is_recording=True,
            stats=self.worker.transcription_stats,
        )
        logger.info("[bridge] 已进入录音状态 is_recording=True")

    def handle_stop(self) -> None:
        logger.info("[bridge] 收到 stop 命令，准备停止录音并提交转写")
        try:
            self.worker.stop()
        except Exception as exc:  # noqa: BLE001
            logger.error("[bridge] 停止录音失败: %s", exc, exc_info=True)
            self.emit_event("recording_error", message=str(exc))
            # 仍旧刷新状态

        with self._state_lock:
            self._is_recording = False

        self.emit_event(
            "recording_state",
            is_recording=False,
            stats=self.worker.transcription_stats,
        )
        logger.info("[bridge] 已退出录音状态 is_recording=False")

    def handle_stats(self) -> None:
        self._emit_stats()

    def handle_shutdown(self) -> None:
        self.emit_event("shutdown_requested")
        self.shutdown()

    # ------------------------------------------------------------------
    def shutdown(self) -> None:
        logger.info("[bridge] shutdown 开始，准备清理资源")
        with self._state_lock:
            if self._shutdown:
                logger.info("[bridge] shutdown 已执行过，跳过重复清理")
                return
            self._shutdown = True

        try:
            self.worker.stop()
        except Exception as exc:  # noqa: BLE001
            logger.debug("停止录音时发生错误（忽略）: %s", exc)

        try:
            self.worker.cleanup()
        except Exception as exc:  # noqa: BLE001
            logger.debug("清理工作线程时发生错误（忽略）: %s", exc)

        logger.info("[bridge] shutdown 资源清理完成，准备发送事件")
        self.emit_event("bridge_shutdown", stats=self.worker.transcription_stats)

    # ------------------------------------------------------------------
    def run(self) -> None:
        logger.info("[bridge] 桥接进程就绪，开始监听 stdin 命令")
        self.emit_event(
            "bridge_ready",
            save_dataset=self._save_dataset,
            dataset_dir=self._dataset_dir,
            stats=self.worker.transcription_stats,
        )

        try:
            while not self._shutdown:
                try:
                    raw_line = sys.stdin.readline()
                except Exception as exc:  # noqa: BLE001
                    logger.error("[bridge] 读取命令失败: %s", exc, exc_info=True)
                    self.emit_event(
                        "bridge_error",
                        message=str(exc),
                        stage="stdin_read",
                    )
                    time.sleep(0.5)
                    continue

                if raw_line == "":
                    logger.warning("[bridge] 命令输入流返回 EOF，立即退出 run 循环")
                    break

                line = raw_line.strip()
                if not line:
                    continue

                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.warning("无法解析命令: %s", exc)
                    self.emit_event(
                        "invalid_command",
                        message="invalid_json",
                        raw=line,
                    )
                    continue

                cmd = str(payload.get("cmd", "")).lower().strip()
                if not cmd:
                    self.emit_event(
                        "invalid_command",
                        message="missing_cmd",
                        raw=line,
                    )
                    continue

                logger.info("[bridge] 收到命令: %s", cmd)
                try:
                    if cmd == "start":
                        self.handle_start()
                    elif cmd == "stop":
                        self.handle_stop()
                    elif cmd == "stats":
                        self.handle_stats()
                    elif cmd == "shutdown":
                        self.handle_shutdown()
                        break
                    else:
                        logger.debug("收到未知命令: %s", cmd)
                        self.emit_event(
                            "invalid_command",
                            message="unknown_cmd",
                            cmd=cmd,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.error("[bridge] 处理命令 %s 时异常: %s", cmd, exc, exc_info=True)
                    self.emit_event(
                        "bridge_error",
                        message=str(exc),
                        cmd=cmd,
                        stage="handle_cmd",
                    )
        except Exception as exc:  # noqa: BLE001
            logger.error("[bridge] run 循环发生未捕获异常: %s", exc, exc_info=True)
            self.emit_event(
                "bridge_error",
                message=str(exc),
                stage="run_loop",
            )
        finally:
            if self._shutdown:
                logger.info("[bridge] run 循环因 shutdown 指令退出")
            else:
                logger.warning("[bridge] run 循环异常退出，执行清理并向前端告警")
                self.emit_event("bridge_error", message="run_loop_exit", stage="loop_exit")
            # 结束时尽量清理资源；如果事件通道不可用则静默退出
            try:
                self.shutdown()
            except Exception as exc:  # noqa: BLE001
                logger.debug("退出清理时发生异常: %s", exc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bridge TranscriptionWorker via stdin/stdout")
    parser.add_argument("--config", help="Path to config JSON")
    parser.add_argument("--save-dataset", action="store_true", help="Persist audio/text pairs")
    parser.add_argument("--dataset-dir", default="dataset", help="Dataset output directory")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    _configure_logging(config["logging"].get("level", "INFO"))

    app = BridgeApp(
        config=config,
        config_path=args.config,
        save_dataset=args.save_dataset,
        dataset_dir=args.dataset_dir,
    )

    try:
        app.run()
    except KeyboardInterrupt:
        logger.info("接收到 KeyboardInterrupt，准备退出桥接进程")
        app.shutdown()
    except Exception as exc:  # noqa: BLE001
        logger.exception("桥接进程发生致命错误: %s", exc)
        app.emit_event("bridge_error", message=str(exc))
        app.shutdown()


if __name__ == "__main__":
    main()


