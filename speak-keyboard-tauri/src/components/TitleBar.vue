<template>
  <div class="title-bar">
    <div class="title-bar-title">
      <img v-if="showIcon" src="/tauri.svg" class="title-bar-icon" alt="icon" />
      <span>{{ title }}</span>
    </div>
    <div class="title-bar-controls">
      <button 
        class="title-bar-button minimize-button" 
        @click="minimizeWindow"
        title="最小化"
        type="button"
      >
        <svg width="10" height="10" viewBox="0 0 10 10">
          <path d="M 0,5 L 10,5" stroke="currentColor" stroke-width="1" />
        </svg>
      </button>
      <button 
        class="title-bar-button close-button" 
        @click="closeWindow"
        title="关闭"
        type="button"
      >
        <svg width="10" height="10" viewBox="0 0 10 10">
          <path d="M 0,0 L 10,10 M 10,0 L 0,10" stroke="currentColor" stroke-width="1" />
        </svg>
      </button>
    </div>
  </div>
</template>

<script setup>
import { WebviewWindow } from '@tauri-apps/api/webviewWindow';
import { invoke } from '@tauri-apps/api/core';

const props = defineProps({
  title: {
    type: String,
    default: '语音输入助手'
  },
  showIcon: {
    type: Boolean,
    default: true
  },
  windowLabel: {
    type: String,
    required: true
  }
});

async function minimizeWindow() {
  try {
    // 优先调用后端命令，确保不同窗口的策略一致
    await invoke('minimize_window', { label: props.windowLabel });
  } catch (error) {
    console.error('后端最小化失败，尝试前端最小化:', error);
    try {
      const window = WebviewWindow.getByLabel(props.windowLabel);
      if (window) {
        await window.minimize();
      }
    } catch (e) {
      console.error('前端最小化失败:', e);
    }
  }
}

async function closeWindow() {
  // 对于 widget 和 settings 窗口，都使用隐藏而不是关闭
  // 这样可以保持窗口状态，下次打开更快
  try {
    await invoke('hide_window', { label: props.windowLabel });
  } catch (error) {
    console.error('关闭窗口失败:', error);
  }
}
</script>

<style scoped>
.title-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 28px;
  background-color: #ffffff;
  border-bottom: 1px solid #e5e7eb;
  user-select: none;
  -webkit-user-select: none;
  -webkit-app-region: drag;
  flex-shrink: 0;
}

.title-bar-title {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-left: 12px;
  font-size: 12px;
  font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif;
  color: #000000;
  flex: 1;
  height: 100%;
}

.title-bar-icon {
  width: 16px;
  height: 16px;
  pointer-events: none;
}

.title-bar-controls {
  display: flex;
  height: 100%;
  -webkit-app-region: no-drag;
}

.title-bar-button {
  width: 46px;
  height: 28px;
  border: none;
  background-color: transparent;
  color: #000000;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: background-color 0.1s;
  padding: 0;
  outline: none;
  -webkit-app-region: no-drag;
}

.title-bar-button svg {
  pointer-events: none;
}

.minimize-button:hover {
  background-color: #f3f4f6;
}

.minimize-button:active {
  background-color: #e5e7eb;
}

.close-button:hover {
  background-color: #C42B1C;
  color: #ffffff;
}

.close-button:active {
  background-color: #A52813;
  color: #ffffff;
}
</style>

