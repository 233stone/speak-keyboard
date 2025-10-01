"""Simple energy-based VAD for incremental segmentation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

import numpy as np


logger = logging.getLogger(__name__)


@dataclass
class VadConfig:
    start_threshold: float
    stop_threshold: float
    min_speech_samples: int
    min_silence_samples: int
    pad_samples: int


class EnergyVad:
    """Basic energy-driven VAD used until Silero integration is added."""

    def __init__(self, config: VadConfig) -> None:
        self.config = config
        self._speech_frames: List[np.ndarray] = []
        self._pre_speech_buffer = np.zeros((0,), dtype=np.int16)
        self._in_speech = False
        self._silence_counter = 0

    def reset(self) -> None:
        logger.debug("VAD 状态重置")
        self._speech_frames.clear()
        self._pre_speech_buffer = np.zeros((0,), dtype=np.int16)
        self._in_speech = False
        self._silence_counter = 0

    def accept_waveform(self, samples: np.ndarray) -> List[np.ndarray]:
        if samples.dtype != np.int16:
            raise ValueError("Energy VAD expects int16 samples")

        segments: List[np.ndarray] = []
        energy = float(np.mean(np.abs(samples)) / 32768.0)
        logger.debug("VAD 帧能量=%.4f", energy)

        if self._in_speech:
            self._speech_frames.append(samples)
            if energy <= self.config.stop_threshold:
                self._silence_counter += len(samples)
            else:
                self._silence_counter = 0

            if self._silence_counter >= self.config.min_silence_samples:
                segment = np.concatenate(self._speech_frames, axis=0)
                if len(segment) >= self.config.min_speech_samples:
                    segments.append(segment.copy())
                    logger.debug("VAD 输出语音段，长度=%s 样本", len(segment))
                else:
                    logger.debug("语音段过短，丢弃 (%s 样本)", len(segment))
                self._reset_speech_state()

        else:
            self._prepend_buffer(samples)
            if energy >= self.config.start_threshold:
                self._start_speech()
                self._speech_frames.append(self._pre_speech_buffer.copy())
                self._speech_frames.append(samples)
                self._silence_counter = 0

        return [seg for seg in segments if seg.size > 0]

    def _prepend_buffer(self, samples: np.ndarray) -> None:
        self._pre_speech_buffer = np.concatenate([self._pre_speech_buffer, samples])
        if len(self._pre_speech_buffer) > self.config.pad_samples:
            self._pre_speech_buffer = self._pre_speech_buffer[-self.config.pad_samples :]

    def _start_speech(self) -> None:
        logger.debug("VAD 检测到语音开始")
        self._in_speech = True
        self._speech_frames = []

    def _reset_speech_state(self) -> None:
        self._in_speech = False
        self._speech_frames = []
        self._silence_counter = 0



