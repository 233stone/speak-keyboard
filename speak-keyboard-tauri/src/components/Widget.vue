<template>
  <div class="widget-container">
    <!-- 自定义标题栏 -->
    <TitleBar title="语音键盘" :showIcon="false" windowLabel="widget" />
    
    <!-- 主内容区域 -->
    <div class="widget-content">
      <!-- 麦克风按钮 -->
      <button 
        @pointerenter="handlePointerEnter"
        @click="toggleRecording" 
        :class="['mic-button', { 'recording': isRecording }]"
        type="button"
      >
        <div v-if="isRecording" class="pulse-ring"></div>
        <i class="fas fa-microphone mic-icon"></i>
      </button>

      <!-- 状态文本 -->
      <p class="status-text">{{ displayText }}</p>

      <!-- 统计信息 -->
      <div class="stats-info">
        <i class="fas fa-stopwatch"></i>
        <span>今日已节省: {{ timeSaved }}</span>
      </div>

      <!-- 功能按钮 -->
      <div class="action-buttons">
        <button @click="openSettings" class="action-btn" type="button" title="设置">
          <i class="fas fa-cog"></i>
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, computed } from 'vue';
import { invoke } from '@tauri-apps/api/core';
import { getCurrentWindow } from '@tauri-apps/api/window';
import { listen } from '@tauri-apps/api/event';
import TitleBar from './TitleBar.vue';

const isRecording = ref(false);
const isProcessing = ref(false);
const currentHotkey = ref('F2');
const timeSaved = ref('0 分钟');

const unlistenList = [];

const displayText = computed(() => {
  if (isRecording.value) return `正在聆听（${currentHotkey.value}）`;
  if (isProcessing.value) return '处理中...';
  return `按 ${currentHotkey.value} 或点击开始`;
});

// 切换录音状态（状态以后端事件为准，这里只发送指令）
async function toggleRecording() {
  try {
    const stateBefore = await invoke('get_recording_state');
    console.log('[widget] 调用 toggle_recording 前状态 is_recording=', stateBefore);
    await invoke('toggle_recording');
    const stateAfter = await invoke('get_recording_state');
    console.log('[widget] 调用 toggle_recording 后本地状态(可能预切换) is_recording=', stateAfter);
  } catch (error) {
    console.error('发送录音指令失败:', error);
  }
}

function handlePointerEnter() {
  // 仍保留函数占位，避免模板报错
}

// 打开设置窗口
async function openSettings() {
  console.log('点击齿轮按钮，准备打开设置窗口');
  try {
    await invoke('show_window', { label: 'settings' });
    console.log('show_window 调用成功');
  } catch (error) {
    console.error('打开设置失败:', error);
  }
}


// 统计：加载与订阅
function formatMinutes(sec) {
  const minutes = Math.round((Number(sec) || 0) / 60);
  return `${minutes} 分钟`;
}

async function loadUsageStats() {
  try {
    const snap = await invoke('get_usage_stats');
    if (snap && typeof snap.today_sec === 'number') {
      timeSaved.value = formatMinutes(snap.today_sec);
    }
  } catch (e) {
    console.warn('[widget] 加载统计失败:', e);
  }
}

// 监听事件：全局快捷键 + 桥接事件
onMounted(async () => {
  console.log('[widget] Widget组件已挂载，开始初始化');
  
  try {
    const info = await invoke('get_recording_hotkey');
    console.log('[widget] 获取到初始快捷键:', info);
    if (info && info.current) {
      currentHotkey.value = info.current;
    }
  } catch (err) {
    console.warn('[widget] 读取录音快捷键失败，保持默认 F2:', err);
    currentHotkey.value = 'F2';
  }

  console.log('[widget] 当前快捷键设置为:', currentHotkey.value);

  // 初始加载统计
  await loadUsageStats();

  // 全局快捷键（F2）
  console.log('[widget] 注册 global-shortcut-pressed 监听器');
  const offGlobalShortcut = await listen('global-shortcut-pressed', async (event) => {
    console.log('[widget] 收到全局快捷键事件:', event.payload);
    if (typeof event.payload === 'string') {
      currentHotkey.value = event.payload;
    }
    // 注意：快捷键已经在 Rust 端的 handle_recording_hotkey 中处理并发送了命令
    // 这里不需要再调用 toggle_recording，避免重复发送命令
    // 只需要获取当前状态用于UI更新
    const currentState = await invoke('get_recording_state');
    console.log('[widget] 快捷键触发后状态 is_recording=', currentState);
  });
  unlistenList.push(offGlobalShortcut);

  // 录音快捷键更新（设置页触发）
  console.log('[widget] 注册 recording-hotkey-updated 监听器');
  const offHotkeyUpdated = await listen('recording-hotkey-updated', (event) => {
    console.log('[widget] ========== 收到 recording-hotkey-updated 事件 ==========');
    console.log('[widget] 完整事件对象:', JSON.stringify(event, null, 2));
    console.log('[widget] event.payload 类型:', typeof event.payload, '值:', event.payload);
    const hk = typeof event.payload === 'string' ? event.payload : '';
    console.log('[widget] 解析后的快捷键:', hk);
    if (hk) {
      console.log('[widget] 准备更新 currentHotkey，旧值:', currentHotkey.value, '新值:', hk);
      currentHotkey.value = hk;
      console.log('[widget] currentHotkey已更新为:', currentHotkey.value);
      console.log('[widget] displayText应该变为:', `按 ${hk} 或点击开始`);
    } else {
      console.warn('[widget] 快捷键为空，未更新');
    }
    console.log('[widget] ========== 事件处理完成 ==========');
  });
  console.log('[widget] recording-hotkey-updated 监听器注册完成');
  unlistenList.push(offHotkeyUpdated);

  // 桥接事件（Python 侧）
  const offBridgeEvent = await listen('bridge-event', (event) => {
    try {
      const payload = event.payload || {};
      const ev = payload.event;
      console.log('[widget] 收到 bridge-event:', ev, payload);
      // 录音状态事件
      if (ev === 'recording_state') {
        const flag = !!payload.is_recording;
        isRecording.value = flag;
        console.log('[widget] 更新前端 isRecording=', flag);
        const stats = payload.stats || {};
        const pending = Number(stats.pending || 0);
        const transcribing = !!stats.is_transcribing;
        isProcessing.value = !flag && (pending > 0 || transcribing);
      }

      // 转写成功
      if (ev === 'transcription_result') {
        console.log('转写完成:', payload.text);
        isProcessing.value = false;
      }

      // 错误类事件
      if (ev === 'transcription_error' || ev === 'recording_error' || ev === 'bridge_error') {
        console.error('发生错误事件:', payload);
        isProcessing.value = false;
        isRecording.value = false;
      }

      // 桥接就绪
      if (ev === 'bridge_ready') {
        console.log('桥接进程已就绪');
        isRecording.value = false;
        isProcessing.value = false;
      }
    } catch (e) {
      console.error('处理桥接事件失败:', e, event);
    }
  });
  unlistenList.push(offBridgeEvent);

  // 统计更新事件
  const offStatsUpdated = await listen('stats-updated', (event) => {
    try {
      const payload = event.payload || {};
      if (typeof payload.today_sec === 'number') {
        timeSaved.value = formatMinutes(payload.today_sec);
      }
    } catch (e) {
      console.warn('[widget] 处理 stats-updated 失败:', e);
    }
  });
  unlistenList.push(offStatsUpdated);
});

onBeforeUnmount(() => {
  while (unlistenList.length) {
    const off = unlistenList.pop();
    try {
      off && off();
    } catch (err) {
      console.warn('[widget] 卸载监听器失败:', err);
    }
  }
});
</script>

<style scoped>
.widget-container {
  width: 100%;
  height: 100%;
  background-color: white;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.widget-content {
  flex: 1;
  padding: 12px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 0;
  overflow: hidden;
}


.status-text {
  font-size: 13px;
  color: #4b5563;
  text-align: center;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
}

.stats-info {
  font-size: 11px;
  color: #6b7280;
  display: flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
}

.action-buttons {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.action-btn {
  padding: 8px 16px;
  background-color: #f3f4f6;
  color: #4b5563;
  border: none;
  border-radius: 6px;
  font-size: 14px;
  cursor: pointer;
  transition: all 0.2s;
}

.action-btn:hover {
  background-color: #e5e7eb;
  color: #1f2937;
}

.action-btn:active {
  transform: scale(0.95);
}
</style>
