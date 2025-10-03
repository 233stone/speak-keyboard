# Tauri + Vue 3

This template should help get you started developing with Tauri + Vue 3 in Vite. The template uses Vue 3 `<script setup>` SFCs, check out the [script setup docs](https://v3.vuejs.org/api/sfc-script-setup.html#sfc-script-setup) to learn more.

## Recommended IDE Setup

- [VS Code](https://code.visualstudio.com/) + [Volar](https://marketplace.visualstudio.com/items?itemName=Vue.volar) + [Tauri](https://marketplace.visualstudio.com/items?itemName=tauri-apps.tauri-vscode) + [rust-analyzer](https://marketplace.visualstudio.com/items?itemName=rust-lang.rust-analyzer)

## 自定义标题栏

应用禁用了原生窗口装饰（`decorations: false`），并使用自定义标题栏 `src/components/TitleBar.vue`。

- 外观：贴近 Windows 原生样式，高度 28px。
- 拖拽：使用 CSS `-webkit-app-region: drag`（按钮区域为 `no-drag`）。
- 按钮：仅保留最小化和关闭。

### 最小化/关闭行为

- Widget（悬浮窗）：
  - 最小化时通过后端命令 `minimize_window` 临时关闭任务栏隐藏（`set_skip_taskbar(false)`），从而最小化到任务栏。
  - 通过托盘或 `show_window('widget')` 恢复显示时，再次设置 `set_skip_taskbar(true)`，回到悬浮窗形态。
- Settings（设置窗）：
  - 直接最小化到任务栏。
- 关闭按钮：当前两个窗口均调用 `hide_window` 隐藏（便于快速恢复）。

### 在组件中使用

`Widget.vue` 与 `Settings.vue` 已集成：

- `Widget.vue`：`<TitleBar title="语音键盘" :showIcon="false" windowLabel="widget" />`
- `Settings.vue`：`<TitleBar title="设置" :showIcon="true" windowLabel="settings" />`

### 相关命令

前端通过 `@tauri-apps/api/core` 调用：

```js
import { invoke } from '@tauri-apps/api/core';

await invoke('show_window', { label: 'widget' });
await invoke('hide_window', { label: 'settings' });
await invoke('minimize_window', { label: 'widget' });
```