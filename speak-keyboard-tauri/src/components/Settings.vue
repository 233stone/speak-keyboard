<template>
  <div class="settings-container">
    <!-- 自定义标题栏 -->
    <TitleBar title="设置" :showIcon="true" windowLabel="settings" />
    
    <!-- 标签页导航 -->
    <div class="tabs-nav" @click="onTabsClick">
      <button 
        v-for="tab in tabs" 
        :key="tab.id"
        @click="selectTab(tab.id)"
        :class="['tab-button', { 'active': activeTab === tab.id }]"
      >
        {{ tab.name }}
      </button>
    </div>

    <!-- 标签页内容 -->
    <div class="tabs-content">
      <!-- 替换词典 -->
      <div v-if="activeTab === 'dictionary'" class="tab-panel">
        <p class="tab-description">当语音识别出现固定错误时，可在此处添加替换规则，系统将自动修正。</p>
        
        <div class="dict-options">
          <label class="switch">
            <input v-model="caseInsensitive" type="checkbox">
            <span class="slider"></span>
          </label>
          <span class="option-label">忽略大小写匹配</span>
        </div>
        
        <div class="dictionary-header">
          <div class="col-5">错误词 (识别结果)</div>
          <div class="col-1 center"><i class="fas fa-arrow-right"></i></div>
          <div class="col-5">正确词 (替换为)</div>
          <div class="col-1"></div>
        </div>

        <div class="dictionary-list">
          <div v-for="(item, index) in dictionary" :key="index" class="dictionary-item">
            <input 
              v-model="item.wrong" 
              type="text" 
              placeholder="如：因位"
              class="dict-input col-5"
            >
            <div class="col-1 center arrow"><i class="fas fa-arrow-right"></i></div>
            <input 
              v-model="item.correct" 
              type="text" 
              placeholder="如：因为"
              class="dict-input col-5"
            >
            <button @click="removeWord(index)" class="delete-button col-1">
              <i class="fas fa-trash-alt"></i>
            </button>
          </div>
        </div>

        <button @click="addWord" class="add-button">
          <i class="fas fa-plus"></i> 添加新规则
        </button>
        
        <button @click="saveDictionary" class="add-button" style="margin-top: 8px;">
          <i class="fas fa-save"></i> 保存
        </button>
      </div>

      <!-- 通用设置 -->
      <div v-if="activeTab === 'general'" class="tab-panel">
        <div class="setting-section">
          <div class="setting-item">
            <label class="setting-label">录音快捷键</label>
            <p class="setting-description">设置开始/结束录音的全局快捷键。</p>
            <button class="shortcut-display" type="button" @click="startCapture" :class="{ recording: capturing }">
              <span v-if="capturing">{{ captureHint }}</span>
              <span v-else>{{ currentHotkey }}</span>
            </button>
            <p v-if="errorMsg" class="shortcut-error">{{ errorMsg }}</p>
          </div>

          <div class="setting-item">
            <label class="setting-label">开机自启动</label>
            <p class="setting-description">默认关闭。按需开启以便开机后自动运行。</p>
            <label class="switch">
              <input v-model="autoStart" type="checkbox" @change="onToggleAutostart">
              <span class="slider"></span>
            </label>
          </div>
        </div>
      </div>

      <!-- 统计信息 -->
      <div v-if="activeTab === 'stats'" class="tab-panel">
        <div class="stats-card">
          <p class="stats-title">您已通过语音键盘节省了大约</p>
          <p class="stats-time">{{ stats.totalFormatted }}</p>
          <p class="stats-description">根据相关研究语音输入效率相较打字平均快约3.2倍</p>
        </div>

        <div class="stats-grid">
          <div class="stats-box">
            <p class="stats-number">{{ stats.totalChars }}</p>
            <p class="stats-label">已转录字数</p>
          </div>
          <div class="stats-box">
            <p class="stats-number">{{ stats.corrections }}</p>
            <p class="stats-label">自动修正次数</p>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, watch, nextTick } from 'vue';
import { invoke } from '@tauri-apps/api/core';
import { emit } from '@tauri-apps/api/event';
import { getCurrentWindow } from '@tauri-apps/api/window';
import TitleBar from './TitleBar.vue';

const activeTab = ref('dictionary');
const autoStart = ref(false);
const currentHotkey = ref('F2');
const capturing = ref(false);
const captureHint = ref('按下新的快捷键，Esc 取消');
const errorMsg = ref('');
let keydownHandler = null;

function resetCapture() {
  capturing.value = false;
  captureHint.value = '按下新的快捷键，Esc 取消';
  if (keydownHandler) {
    window.removeEventListener('keydown', keydownHandler, true);
    keydownHandler = null;
  }
}

function startCapture() {
  if (capturing.value) return;
  errorMsg.value = '';
  capturing.value = true;
  captureHint.value = '请按下新的快捷键...';

  keydownHandler = async (event) => {
    event.preventDefault();
    event.stopPropagation();

    if (event.key === 'Escape') {
      resetCapture();
      return;
    }

    const keys = [];
    if (event.ctrlKey || event.metaKey) keys.push('Ctrl');
    if (event.altKey) keys.push('Alt');
    if (event.shiftKey) keys.push('Shift');

    const mainKey = event.key.length === 1 ? event.key.toUpperCase() : event.key;
    if (!['Control', 'Shift', 'Alt', 'Meta'].includes(event.key)) {
      keys.push(mainKey);
    }

    const hotkeyStr = keys.join('+');
    if (!hotkeyStr || ['Control', 'Shift', 'Alt', 'Meta'].includes(hotkeyStr)) {
      errorMsg.value = '快捷键需要包含具体按键，例如 Ctrl+Shift+K';
      return;
    }

    try {
      await invoke('set_recording_hotkey', { payload: { hotkey: hotkeyStr } });
      currentHotkey.value = hotkeyStr;
      console.log('[settings] 快捷键设置成功，后端应该会发送事件到widget');
      resetCapture();
    } catch (e) {
      console.error('设置快捷键失败:', e);
      errorMsg.value = typeof e === 'string' ? e : '设置快捷键失败，请重试';
    }
  };

  window.addEventListener('keydown', keydownHandler, true);
}

onBeforeUnmount(() => {
  resetCapture();
  // 移除 beforeunload 监听，避免重复注册
  window.removeEventListener('beforeunload', resetCapture);
});

window.addEventListener('beforeunload', resetCapture);

const tabs = [
  { id: 'dictionary', name: '替换词典' },
  { id: 'general', name: '通用设置' },
  { id: 'stats', name: '统计信息' }
];

// 标签切换（带简单性能埋点）
function selectTab(id) {
  const t0 = performance.now();
  activeTab.value = id;
  const t1 = performance.now();
  nextTick(() => {
    const t2 = performance.now();
    requestAnimationFrame(() => {
      const t3 = performance.now();
      requestAnimationFrame(() => {
        const t4 = performance.now();
        const commitCost = Math.round(t1 - t0);
        const mountCost = Math.round(t2 - t1);
        const firstFrame = Math.round(t3 - t2);
        const secondFrame = Math.round(t4 - t3);
        const total = Math.round(t4 - t0);
        console.log(`[perf] 切到 ${id} | 提交:${commitCost}ms, nextTick:${mountCost}ms, RAF1:${firstFrame}ms, RAF2:${secondFrame}ms, 总:${total}ms`);
      });
    });
  });
}

// 离开通用设置时，自动清理快捷键捕获，避免残留监听
watch(activeTab, (newVal, oldVal) => {
  if (oldVal === 'general' && capturing.value) {
    resetCapture();
  }
  if (newVal === 'stats') {
    loadUsageStats();
  }
});

// 标签栏点击埋点：记录容器尺寸与点击位置，判断是否点到空白
function onTabsClick(event) {
  try {
    const target = event.target;
    const container = event.currentTarget;
    const rect = container.getBoundingClientRect();
    const x = Math.round(event.clientX - rect.left);
    const y = Math.round(event.clientY - rect.top);
    const withinButton = target && target.closest && !!target.closest('.tab-button');
    console.log('[tabs] 点击位置', { x, y, width: Math.round(rect.width), height: Math.round(rect.height), withinButton });
  } catch (e) {
    // 降级防护
  }
}

// 替换词典数据（从后端加载）
const dictionary = ref([]);
const caseInsensitive = ref(true);

// 统计数据
const stats = ref({
  totalFormatted: '0时0分',
  totalChars: 0,
  corrections: 0
});

function formatHoursMinutes(sec) {
  const totalMinutes = Math.max(0, Math.round((Number(sec) || 0) / 60));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return `${hours}时${minutes}分`;
}

async function loadUsageStats() {
  try {
    const snap = await invoke('get_usage_stats');
    stats.value.totalFormatted = formatHoursMinutes(snap && snap.total_sec);
    stats.value.totalChars = (snap && snap.total_chars) || 0;
    stats.value.corrections = (snap && snap.total_corrections) || 0;
  } catch (e) {
    console.warn('[settings] 加载统计失败:', e);
  }
}

// 添加词条
function addWord() {
  dictionary.value.push({ wrong: '', correct: '' });
}

// 删除词条
function removeWord(index) {
  dictionary.value.splice(index, 1);
}

// 关闭窗口
async function closeWindow() {
  console.log('关闭设置窗口');
  try {
    await invoke('hide_window', { label: 'settings' });
    console.log('设置窗口已隐藏');
  } catch (error) {
    console.error('隐藏设置窗口失败:', error);
  }
}

// 保存词典到后端（写入 config/postprocess.json）
async function saveDictionary() {
  try {
    const map = {};
    for (const item of dictionary.value) {
      const k = (item.wrong || '').trim();
      const v = (item.correct || '').trim();
      if (!k || !v) continue;
      map[k] = v;
    }

    await invoke('save_postprocess_config', {
      payload: {
        case_insensitive: !!caseInsensitive.value,
        replace_map: map,
      }
    });

    // 保存后回读一次，确保与后端规范化结果一致
    await loadDictionary();
  } catch (err) {
    console.error('保存词典失败:', err);
    // 保持页面状态即可，用户可重试
  }
}

// 从后端加载词典
async function loadDictionary() {
  try {
    const cfg = await invoke('get_postprocess_config');
    const ci = !!(cfg && cfg.case_insensitive);
    const map = (cfg && cfg.replace_map) ? cfg.replace_map : {};
    caseInsensitive.value = ci;
    const list = Object.entries(map).map(([k, v]) => ({ wrong: k, correct: String(v ?? '') }));
    dictionary.value = list.length > 0 ? list : [{ wrong: '', correct: '' }];
  } catch (err) {
    console.error('加载词典失败:', err);
    dictionary.value = [{ wrong: '', correct: '' }];
  }
}

// 加载设置
onMounted(async () => {
  console.log('设置窗口组件已挂载');
  // 监控长任务（PerformanceObserver）辅助定位卡顿
  try {
    if ('PerformanceObserver' in window) {
      const observer = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          if (entry.duration > 50) {
            console.log('[perf] LongTask', Math.round(entry.duration), 'ms');
          }
        }
      });
      observer.observe({ type: 'longtask', buffered: true });
    }
  } catch (_) {}
  await loadDictionary();
  try {
    const hotkeyInfo = await invoke('get_recording_hotkey');
    if (hotkeyInfo && hotkeyInfo.current) {
      currentHotkey.value = hotkeyInfo.current;
    }
  } catch (err) {
    console.warn('读取录音快捷键失败，使用默认 F2:', err);
    currentHotkey.value = 'F2';
  }
  // 读取自启动状态（默认 false）
  try {
    const enabled = await invoke('get_autostart_enabled');
    autoStart.value = !!enabled;
  } catch (e) {
    console.warn('读取自启动状态失败，保持默认关闭:', e);
    autoStart.value = false;
  }
  if (activeTab.value === 'stats') {
    await loadUsageStats();
  }
});

async function onToggleAutostart() {
  const target = !!autoStart.value;
  try {
    await invoke('set_autostart_enabled', { enabled: target });
  } catch (e) {
    console.error('设置自启动失败，将回滚UI:', e);
    autoStart.value = !target;
  }
}
</script>

<style scoped>
.settings-container {
  width: 100%;
  height: 100%;
  background-color: white;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.tabs-nav {
  padding: 8px 12px;
  border-bottom: 1px solid #e5e7eb;
  display: flex;
  align-items: center;
  gap: 8px;
}

.tab-button {
  padding: 6px 10px;
  border-radius: 6px;
  font-weight: 500;
  color: #4b5563;
  transition: background-color 0.2s, color 0.2s;
}

.tab-button:hover {
  background-color: #f3f4f6;
}

.tab-button.active {
  background-color: #3b82f6;
  color: white;
}

.tabs-content {
  flex-grow: 1;
  padding: 24px;
  overflow-y: auto;
}

.tab-panel {
  animation: fadeIn 0.3s;
}

@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

.tab-description {
  font-size: 14px;
  color: #6b7280;
  margin-bottom: 16px;
}

.dict-options {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.option-label {
  font-size: 13px;
  color: #4b5563;
}

.dictionary-header {
  display: grid;
  grid-template-columns: 5fr 1fr 5fr 1fr;
  gap: 8px;
  margin-bottom: 16px;
  font-weight: 600;
  color: #374151;
  font-size: 14px;
}

.dictionary-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 16px;
}

.dictionary-item {
  display: grid;
  grid-template-columns: 5fr 1fr 5fr 1fr;
  gap: 8px;
  align-items: center;
  font-size: 14px;
}

.dict-input {
  padding: 8px 12px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  background-color: #f9fafb;
}

.col-1 { grid-column: span 1; }
.col-5 { grid-column: span 1; }
.center { text-align: center; }
.arrow { color: #9ca3af; }

.delete-button {
  color: #ef4444;
  transition: color 0.2s;
}

.delete-button:hover {
  color: #dc2626;
}

.add-button {
  width: 100%;
  margin-top: 16px;
  padding: 10px 16px;
  background-color: #3b82f6;
  color: white;
  border-radius: 8px;
  font-weight: 500;
  transition: background-color 0.2s;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}

.add-button:hover {
  background-color: #2563eb;
}

.setting-section {
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.setting-item {
  display: flex;
  flex-direction: column;
}

.setting-label {
  display: block;
  font-size: 16px;
  font-weight: 500;
  color: #374151;
  margin-bottom: 8px;
}

.setting-description {
  font-size: 14px;
  color: #6b7280;
  margin-bottom: 12px;
}

.shortcut-display {
  width: 200px;
  height: 40px;
  background-color: #e5e7eb;
  border-radius: 6px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  font-family: monospace;
  color: #1f2937;
  cursor: pointer;
  transition: background-color 0.2s;
  border: none;
}

.shortcut-display.recording {
  background-color: #fef3c7;
  color: #b45309;
}

.shortcut-display:hover {
  background-color: #d1d5db;
}

.shortcut-error {
  margin-top: 6px;
  font-size: 13px;
  color: #b91c1c;
}

.switch {
  position: relative;
  display: inline-block;
  width: 44px;
  height: 24px;
}

.switch input {
  opacity: 0;
  width: 0;
  height: 0;
}

.slider {
  position: absolute;
  cursor: pointer;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background-color: #d1d5db;
  transition: 0.3s;
  border-radius: 24px;
}

.slider:before {
  position: absolute;
  content: "";
  height: 20px;
  width: 20px;
  left: 2px;
  bottom: 2px;
  background-color: white;
  transition: 0.3s;
  border-radius: 50%;
}

input:checked + .slider {
  background-color: #3b82f6;
}

input:checked + .slider:before {
  transform: translateX(20px);
}

.stats-card {
  background-color: #eef2ff;
  border: 1px solid #c7d2fe;
  border-radius: 8px;
  padding: 24px;
  text-align: center;
  margin-bottom: 24px;
}

.stats-title {
  font-size: 18px;
  color: #3730a3;
  margin-bottom: 8px;
}

.stats-time {
  font-size: 48px;
  font-weight: 700;
  color: #4f46e5;
  margin-bottom: 16px;
}

.stats-unit {
  font-size: 32px;
  font-weight: 500;
}

.stats-description {
  font-size: 14px;
  color: #6366f1;
}

.stats-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  text-align: center;
}

.stats-box {
  background-color: #f3f4f6;
  padding: 16px;
  border-radius: 8px;
}

.stats-number {
  font-size: 32px;
  font-weight: 700;
  color: #1f2937;
  margin-bottom: 8px;
}

.stats-label {
  font-size: 14px;
  color: #6b7280;
}
</style>
