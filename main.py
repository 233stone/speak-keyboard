"""Command-line entry for the speak-keyboard prototype."""

from __future__ import annotations

import argparse
import logging
import keyboard

from app import HotkeyManager, TranscriptionResult, TranscriptionWorker, load_config, type_text


logger = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Speak Keyboard prototype")
    parser.add_argument("--config", help="Path to config JSON")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single transcription cycle for debugging",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    _configure_logging(config["logging"].get("level", "INFO"))

    output_cfg = config.get("output", {})
    output_method = output_cfg.get("method", "auto")
    append_newline = output_cfg.get("append_newline", False)

    worker = TranscriptionWorker(
        config_path=args.config,
        on_result=_make_result_handler(output_method, append_newline),
    )
    hotkeys = HotkeyManager()

    toggle_combo = config["hotkeys"].get("toggle", "f2")
    hotkeys.register(toggle_combo, lambda: _toggle(worker))

    try:
        logger.info("Speak Keyboard 启动完成，按 %s 开始/停止录音", toggle_combo)
        if args.once:
            _toggle(worker)
            input("按 Enter 停止并退出...")
            _toggle(worker)
        else:
            keyboard.wait()
    except KeyboardInterrupt:
        logger.info("用户中断，正在退出...")
    finally:
        worker.stop()
        worker.cleanup()
        hotkeys.cleanup()


def _make_result_handler(output_method: str, append_newline: bool):
    def _handle_result(result: TranscriptionResult) -> None:
        if result.error:
            logger.error("转写失败: %s", result.error)
            return

        logger.info(
            "转写成功: %s (推理 %.2fs)",
            result.text,
            result.inference_latency,
        )
        type_text(
            result.text,
            append_newline=append_newline,
            method=output_method,
        )

    return _handle_result


def _toggle(worker: TranscriptionWorker) -> None:
    if worker.is_running:
        worker.stop()
    else:
        worker.start()


if __name__ == "__main__":
    main()

