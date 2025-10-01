"""Session-based transcription worker using FunASR once per recording."""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .audio_capture import AudioCapture
from .config import ensure_logging_dir, load_config
from funasr_server import FunASRServer


logger = logging.getLogger(__name__)


@dataclass
class TranscriptionResult:
    text: str
    raw_text: str
    duration: float
    inference_latency: float
    confidence: float
    error: Optional[str] = None


class TranscriptionWorker:
    """Capture full session audio and transcribe once when stopped."""

    def __init__(
        self,
        config_path: Optional[str] = None,
        on_result: Optional[Callable[[TranscriptionResult], None]] = None,
    ) -> None:
        self.config = load_config(config_path)
        self.on_result = on_result
        self.log_dir = ensure_logging_dir(self.config)
        self.last_segment_path: Optional[Path] = None

        audio_cfg = self.config["audio"]
        self.audio = AudioCapture(
            sample_rate=audio_cfg["sample_rate"],
            block_ms=audio_cfg["block_ms"],
            device=audio_cfg.get("device"),
        )

        self.fun_server = FunASRServer()
        init_result = self.fun_server.initialize()
        if not init_result.get("success"):
            raise RuntimeError(f"FunASR 初始化失败: {init_result}")

        self._running = threading.Event()
        self._recording = threading.Event()
        self._stop_requested = threading.Event()
        self._capture_thread: Optional[threading.Thread] = None
        self._audio_cfg = audio_cfg
        self._buffer: list[np.ndarray] = []
        self._buffer_lock = threading.Lock()

    def __del__(self) -> None:
        """析构函数，确保资源被清理"""
        try:
            self.cleanup()
        except Exception as exc:
            logger.debug("析构函数清理时出错: %s", exc)

    def cleanup(self) -> None:
        """清理所有资源，包括缓冲区和音频设备"""
        logger.debug("开始清理 TranscriptionWorker 资源")
        try:
            # 停止录音
            if self._running.is_set():
                self.stop()
            
            # 清理缓冲区
            with self._buffer_lock:
                self._buffer.clear()
            
            # 停止音频捕获
            if hasattr(self, 'audio'):
                self.audio.stop()
                
            logger.debug("TranscriptionWorker 资源清理完成")
        except Exception as exc:
            logger.error("清理资源时出错: %s", exc)

    def start(self) -> None:
        if self._running.is_set():
            logger.debug("Transcription worker 已在运行，忽略重复启动")
            return

        logger.info("Transcription worker starting")
        self._running.set()
        self._stop_requested.clear()
        with self._buffer_lock:
            self._buffer.clear()
        self.audio.start()
        self._recording.set()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()

    def stop(self) -> None:
        if not self._running.is_set():
            logger.debug("Transcription worker 未运行，忽略 stop")
            return

        logger.info("Transcription worker stopping")
        self._stop_requested.set()
        self._running.clear()
        self._recording.clear()
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=5)
        self._capture_thread = None

        self.audio.stop()
        combined = self._combine_buffer()
        self.audio.flush()

        if combined is None or combined.size == 0:
            logger.warning("未捕获到任何音频样本，跳过转写")
            return

        self._transcribe_once(combined)

    def _capture_loop(self) -> None:
        queue_obj = self.audio.queue
        while self._recording.is_set():
            try:
                frame = queue_obj.get(timeout=0.2)
            except Exception:
                if not self._recording.is_set():
                    break
                continue

            try:
                with self._buffer_lock:
                    if isinstance(frame, np.ndarray):
                        self._buffer.append(frame)
                    else:
                        self._buffer.append(np.frombuffer(frame, dtype=np.int16))
            except Exception as exc:
                logger.error("处理音频帧时出错: %s", exc)

        with self._buffer_lock:
            frame_count = len(self._buffer)
        logger.debug("capture loop exiting, collected %s frames", frame_count)

    def _combine_buffer(self) -> Optional[np.ndarray]:
        with self._buffer_lock:
            if not self._buffer:
                return None
            try:
                combined = np.concatenate(self._buffer, axis=0)
                logger.info("会话录音合并完成，总样本数=%s", combined.size)
                self._buffer.clear()
                return combined
            except Exception as exc:
                logger.error("合并音频缓冲区时出错: %s", exc)
                self._buffer.clear()  # 即使出错也清理缓冲区
                return None

    def _write_temp_wav(self, samples: np.ndarray) -> str:
        import wave

        sample_rate = self._audio_cfg["sample_rate"]
        recent_path = Path(self.log_dir) / "recent.wav"
        os.makedirs(recent_path.parent, exist_ok=True)
        with wave.open(str(recent_path), "wb") as wf_recent:
            wf_recent.setnchannels(1)
            wf_recent.setsampwidth(2)
            wf_recent.setframerate(sample_rate)
            wf_recent.writeframes(samples.tobytes())
        self.last_segment_path = recent_path

        fd, path = tempfile.mkstemp(prefix="asr_session_", suffix=".wav")
        os.close(fd)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(samples.tobytes())

        return path

    def _transcribe_once(self, samples: np.ndarray) -> None:
        tmp_path = self._write_temp_wav(samples)
        start = time.time()
        try:
            asr_result = self.fun_server.transcribe_audio(
                tmp_path,
                options=self.config.get("asr"),
            )
        finally:
            inference_latency = time.time() - start
            try:
                os.remove(tmp_path)
            except OSError:
                logger.debug("删除临时文件失败: %s", tmp_path)

        if not asr_result.get("success"):
            result = TranscriptionResult(
                text="",
                raw_text="",
                duration=0.0,
                inference_latency=inference_latency,
                confidence=0.0,
                error=asr_result.get("error", "unknown"),
            )
        else:
            result = TranscriptionResult(
                text=asr_result.get("text", ""),
                raw_text=asr_result.get("raw_text", ""),
                duration=asr_result.get("duration", 0.0),
                inference_latency=inference_latency,
                confidence=asr_result.get("confidence", 0.0),
            )

        if self.on_result:
            try:
                self.on_result(result)
            except Exception as exc:  # noqa: BLE001
                logger.error("处理转写结果时出错: %s", exc)

    @property
    def is_running(self) -> bool:
        return self._running.is_set()


