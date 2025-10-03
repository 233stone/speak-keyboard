<script setup>
import { onMounted, ref } from "vue";
import { getCurrentWindow } from "@tauri-apps/api/window";
import Widget from "./components/Widget.vue";
import Settings from "./components/Settings.vue";

const currentWindow = ref(null);
const windowLabel = ref("");

onMounted(async () => {
  currentWindow.value = getCurrentWindow();
  windowLabel.value = currentWindow.value.label;
  console.log("当前窗口:", windowLabel.value);
});
</script>

<template>
  <div class="app-container">
    <!-- 悬浮窗 Widget -->
    <Widget v-if="windowLabel === 'widget'" />
    
    <!-- 设置窗口 -->
    <Settings v-if="windowLabel === 'settings'" />
  </div>
</template>

<style scoped>
.app-container {
  width: 100%;
  height: 100%;
  overflow: hidden;
  display: flex;
  align-items: stretch;
  justify-content: stretch;
}
</style>
