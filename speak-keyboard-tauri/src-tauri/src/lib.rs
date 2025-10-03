use tauri::{Manager, Emitter, menu::{Menu, MenuItem}, tray::{TrayIconBuilder, TrayIconEvent}, State};
use tauri::path::BaseDirectory;
use tauri_plugin_global_shortcut::{GlobalShortcutExt, Shortcut, ShortcutState};
use tauri_plugin_autostart::ManagerExt;
// 进程与异步IO
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use std::ffi::OsString;
use std::time::{Duration, Instant};
use std::sync::atomic::{AtomicBool, Ordering};
use tokio::process::{Command, ChildStdin};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use serde_json::Value;
use serde::{Deserialize, Serialize};
use chrono::Local;
use indexmap::IndexMap;
use std::fs;
use std::io::{Read, Write};
use std::str::FromStr;

const DEFAULT_RECORDING_HOTKEY: &str = "F2";

#[derive(Debug, Clone, Serialize, Deserialize)]
struct UiSettings {
    #[serde(default = "default_recording_hotkey")] 
    recording_hotkey: String,
}

fn default_recording_hotkey() -> String {
    DEFAULT_RECORDING_HOTKEY.to_string()
}

impl Default for UiSettings {
    fn default() -> Self {
        UiSettings {
            recording_hotkey: default_recording_hotkey(),
        }
    }
}

fn resolve_ui_settings_path() -> PathBuf {
    resolve_tauri_config_path("ui_settings.json")
}

fn load_ui_settings() -> UiSettings {
    let path = resolve_ui_settings_path();
    if !path.exists() {
        return UiSettings::default();
    }

    match fs::read_to_string(&path) {
        Ok(content) => serde_json::from_str(&content).unwrap_or_default(),
        Err(err) => {
            println!("读取 ui_settings.json 失败: {}，使用默认值", err);
            UiSettings::default()
        }
    }
}

fn save_ui_settings(settings: &UiSettings) -> Result<(), String> {
    let path = resolve_ui_settings_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("创建配置目录失败: {}", e))?;
    }

    let data = serde_json::to_string_pretty(settings).map_err(|e| format!("序列化配置失败: {}", e))?;
    fs::write(&path, data).map_err(|e| format!("写入 ui_settings.json 失败: {}", e))
}

// 录音状态管理
#[derive(Default)]
struct AppState {
    // 录音状态（由桥接事件驱动）
    is_recording: std::sync::Mutex<bool>,
    // 全局快捷键按下态（用于边沿检测）
    hotkey_down: std::sync::Mutex<bool>,
    // 去抖动：最近一次切换时间
    last_toggle: std::sync::Mutex<Option<Instant>>,
    // 当前已注册的录音快捷键（序列化字符串，如 "F2"）
    recording_hotkey: Mutex<String>,
    // 使用统计文件锁，避免并发读写冲突
    usage_lock: std::sync::Mutex<()>,
}

// 桥接进程状态（保存 stdin 句柄供命令写入）
struct BridgeState {
    stdin: Arc<tokio::sync::Mutex<Option<ChildStdin>>>,
    should_restart: Arc<AtomicBool>,
}

impl Drop for BridgeState {
    fn drop(&mut self) {
        let stdin_arc = Arc::clone(&self.stdin);
        tauri::async_runtime::block_on(async move {
            let mut guard = stdin_arc.lock().await;
            if let Some(mut stdin) = guard.take() {
                println!("[tauri] Drop: 发送 shutdown 指令给桥接进程");
                let payload = serde_json::json!({"cmd": "shutdown"}).to_string() + "\n";
                if let Err(err) = stdin.write_all(payload.as_bytes()).await {
                    println!("[tauri] Drop: shutdown 写入失败: {}", err);
                    return;
                }
                if let Err(err) = stdin.flush().await {
                    println!("[tauri] Drop: shutdown 刷新失败: {}", err);
                    return;
                }
                tokio::time::sleep(Duration::from_millis(200)).await;
            } else {
                println!("[tauri] Drop: stdin 已为空，跳过 shutdown 指令");
            }
        });
    }
}

// -----------------------------
// 全局快捷键：读写配置 + 注册/注销
// -----------------------------

fn parse_hotkey(hotkey: &str) -> Result<Shortcut, String> {
    Shortcut::from_str(hotkey)
        .map_err(|e| format!("无法解析快捷键 {}: {}", hotkey, e))
}

fn register_recording_hotkey(app: &tauri::AppHandle, hotkey: &str) -> Result<(), String> {
    // 先注销所有之前注册的快捷键
    let gs = app.global_shortcut();
    gs.unregister_all()
        .map_err(|e| format!("注销旧快捷键失败: {}", e))?;

    let shortcut = parse_hotkey(hotkey)?;
    let hotkey_string = format!("{}", shortcut);
    let handler_hotkey = hotkey_string.clone();

    gs.on_shortcut(shortcut, move |app_handle, _shortcut, event| {
        match event.state {
            ShortcutState::Pressed => {
                let handle_for_task = app_handle.clone();
                let handle_for_error = app_handle.clone();
                let hotkey_for_task = handler_hotkey.clone();
                tauri::async_runtime::spawn(async move {
                    if let Err(err) = handle_recording_hotkey(handle_for_task.clone(), hotkey_for_task.clone()).await {
                        println!("处理快捷键 {} 失败: {}", hotkey_for_task, err);
                        let app_state = handle_for_error.state::<AppState>();
                        let mut down = app_state.hotkey_down.lock().unwrap();
                        *down = false;
                    }
                });
            }
            ShortcutState::Released => {
                let app_state = app_handle.state::<AppState>();
                let mut down = app_state.hotkey_down.lock().unwrap();
                *down = false;
            }
        }
    })
    .map_err(|e| format!("注册快捷键失败: {}", e))?;

    // 更新 AppState 中的记录
    let app_state = app.state::<AppState>();
    {
        let mut guard = app_state.recording_hotkey.lock().unwrap();
        *guard = hotkey_string.clone();
    }

    Ok(())
}

async fn handle_recording_hotkey(app: tauri::AppHandle, shortcut: String) -> Result<(), String> {
    let app_state = app.state::<AppState>();

    // 边沿检测：只在从未按下 -> 按下 的边沿触发
    {
        let mut down = app_state.hotkey_down.lock().unwrap();
        if *down {
            return Ok(());
        }
        *down = true;
    }

    // 去抖：200ms 内忽略重复触发
    let debounce = {
        let mut last = app_state.last_toggle.lock().unwrap();
        let now = Instant::now();
        let within = if let Some(prev) = *last {
            now.duration_since(prev) < Duration::from_millis(200)
        } else {
            false
        };
        *last = Some(now);
        within
    };

    if debounce {
        let mut down = app_state.hotkey_down.lock().unwrap();
        *down = false;
        return Ok(());
    }

    println!("全局快捷键 {} 被按下", shortcut);

    let currently = { *app_state.is_recording.lock().unwrap() };
    let cmd_name = if currently { "stop" } else { "start" }.to_string();
    println!("[tauri] 快捷键路径：当前 is_recording={}，准备发送 {} 指令", currently, cmd_name);

    let bridge_state = app.state::<BridgeState>();
    {
        let mut guard = bridge_state.stdin.lock().await;
        if let Some(stdin) = guard.as_mut() {
            let payload = serde_json::json!({"cmd": cmd_name}).to_string() + "\n";
            stdin
                .write_all(payload.as_bytes())
                .await
                .map_err(|e| format!("[tauri] 快捷键路径写入 {} 失败: {}", cmd_name, e))?;
            stdin
                .flush()
                .await
                .map_err(|e| format!("[tauri] 快捷键路径刷新 {} 失败: {}", cmd_name, e))?;
            println!("[tauri] 快捷键路径已发送 {} 指令", cmd_name);
        } else {
            let mut down = app_state.hotkey_down.lock().unwrap();
            *down = false;
            return Err(format!("[tauri] 快捷键路径发送 {} 失败：stdin 不可用", cmd_name));
        }
    }

    if let Some(window) = app.get_webview_window("widget") {
        let _ = window.emit("global-shortcut-pressed", shortcut.clone());
    }

    {
        let mut down = app_state.hotkey_down.lock().unwrap();
        *down = false;
    }

    Ok(())
}

// 自启动：获取当前状态
#[tauri::command]
fn get_autostart_enabled(app: tauri::AppHandle) -> Result<bool, String> {
    Ok(app.autolaunch().is_enabled().map_err(|e| e.to_string())?)
}

// 自启动：设置状态
#[tauri::command]
fn set_autostart_enabled(app: tauri::AppHandle, enabled: bool) -> Result<bool, String> {
    let api = app.autolaunch();
    if enabled {
        api.enable().map_err(|e| e.to_string())?;
    } else {
        api.disable().map_err(|e| e.to_string())?;
    }
    Ok(true)
}

// -----------------------------
// 配置：替换词典 读/写（postprocess.json）
// -----------------------------

#[derive(Serialize, Deserialize, Debug, Clone)]
struct PostprocessConfig {
    #[serde(default = "default_case_insensitive")]
    case_insensitive: bool,
    #[serde(default)]
    replace_map: IndexMap<String, String>,
}

fn default_case_insensitive() -> bool { true }

#[derive(Deserialize, Debug)]
struct SavePostprocessPayload {
    #[serde(default)]
    case_insensitive: Option<bool>,
    // 允许任意 JSON 值，后续统一字符串化
    #[serde(default)]
    replace_map: IndexMap<String, Value>,
}

fn find_project_root_for_config() -> PathBuf {
    let mut dir = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    for _ in 0..5 {
        if dir.join("app").join("bridge.py").exists() {
            return dir;
        }
        if !dir.pop() { break; }
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

fn resolve_tauri_config_path(file_name: &str) -> PathBuf {
    let root = find_project_root_for_config();
    root.join("speak-keyboard-tauri").join("config").join(file_name)
}

fn resolve_postprocess_path() -> PathBuf {
    let root = find_project_root_for_config();
    root.join("config").join("postprocess.json")
}

fn read_postprocess_config_from_disk() -> Result<PostprocessConfig, String> {
    let path = resolve_postprocess_path();
    if !path.exists() {
        return Ok(PostprocessConfig { case_insensitive: true, replace_map: IndexMap::new() });
    }
    let mut file = fs::File::open(&path).map_err(|e| format!("无法打开配置文件: {}", e))?;
    let mut buf = String::new();
    file.read_to_string(&mut buf).map_err(|e| format!("读取配置失败: {}", e))?;

    // 优先严格反序列化为 PostprocessConfig（可保留键的插入顺序）
    match serde_json::from_str::<PostprocessConfig>(&buf) {
        Ok(cfg) => return Ok(cfg),
        Err(_e) => {
            // 兼容旧格式或非字符串值：宽松解析再清洗
            let val: Value = serde_json::from_str(&buf).map_err(|e| format!("配置 JSON 解析失败: {}", e))?;
            let case_insensitive = val.get("case_insensitive").and_then(|v| v.as_bool()).unwrap_or(true);
            let mut map: IndexMap<String, String> = IndexMap::new();
            if let Some(obj) = val.get("replace_map").and_then(|v| v.as_object()) {
                for (k, v) in obj.iter() {
                    let s = match v {
                        Value::String(s) => s.clone(),
                        other => other.to_string(),
                    };
                    map.insert(k.clone(), s);
                }
            }
            return Ok(PostprocessConfig { case_insensitive, replace_map: map });
        }
    }
}

fn validate_and_clean_payload(payload: SavePostprocessPayload) -> Result<PostprocessConfig, String> {
    let mut cleaned: IndexMap<String, String> = IndexMap::new();
    // 用于 case_insensitive 去重：规范化键(lowercase) -> 原始键
    let mut norm_to_key: IndexMap<String, String> = IndexMap::new();
    let case_insensitive = payload.case_insensitive.unwrap_or(true);

    // 清洗：trim、转字符串、去空、长度限制（<=16）
    let mut errors: Vec<String> = Vec::new();
    for (k, v) in payload.replace_map.into_iter() {
        let key_trim = k.trim();
        let val_str = match v {
            Value::String(s) => s,
            other => other.to_string(),
        };
        let val_trimmed = val_str.trim();

        if key_trim.is_empty() || val_trimmed.is_empty() { continue; }
        if key_trim.chars().count() > 16 { errors.push(format!("键过长(>16): {}", key_trim)); continue; }
        if val_trimmed.chars().count() > 16 { errors.push(format!("值过长(>16): {}", val_trimmed)); continue; }

        let val_final = val_trimmed.to_string();
        // 大小写不敏感：用 lower 做去重，但保存原始大小写键
        let store_key = key_trim.to_string();
        if case_insensitive {
            let key_norm = key_trim.to_lowercase();
            if let Some(old_key) = norm_to_key.get(&key_norm).cloned() {
                let _ = cleaned.shift_remove(&old_key);
            }
            norm_to_key.insert(key_norm, store_key.clone());
            cleaned.insert(store_key, val_final);
        } else {
            if cleaned.contains_key(&store_key) {
                let _ = cleaned.shift_remove(&store_key);
            }
            cleaned.insert(store_key, val_final);
        }
    }

    if !errors.is_empty() {
        return Err(errors.join("; "));
    }

    // 条目数限制：最多200条
    if cleaned.len() > 200 {
        return Err(format!("替换词典超出上限：{} 条（最多 200 条）", cleaned.len()));
    }

    // 如果不区分大小写，为了与 Python 侧匹配，需要把保存的键恢复为原样大小写。
    // 这里选择将键以当前 cleaned 的键直接保存（已转为 lower），Python 侧基于 re.IGNORECASE 进行匹配，大小写无关。

    Ok(PostprocessConfig { case_insensitive, replace_map: cleaned })
}

fn write_postprocess_config_to_disk(cfg: &PostprocessConfig) -> Result<(), String> {
    let path = resolve_postprocess_path();
    if let Some(dir) = path.parent() { fs::create_dir_all(dir).map_err(|e| format!("创建配置目录失败: {}", e))?; }

    let tmp_path = path.with_extension("json.tmp");
    let data = serde_json::to_string_pretty(cfg).map_err(|e| format!("序列化配置失败: {}", e))?;
    {
        let mut f = fs::File::create(&tmp_path).map_err(|e| format!("创建临时文件失败: {}", e))?;
        f.write_all(data.as_bytes()).map_err(|e| format!("写入临时文件失败: {}", e))?;
        f.sync_all().ok();
    }
    // 尝试原子替换
    match fs::rename(&tmp_path, &path) {
        Ok(_) => Ok(()),
        Err(_e) => {
            // Windows 上若目标存在可能失败：先删除再重命名
            let _ = fs::remove_file(&path);
            fs::rename(&tmp_path, &path).map_err(|e| format!("替换配置文件失败: {}", e))
        }
    }
}

// 读取配置
#[tauri::command]
fn get_postprocess_config() -> Result<PostprocessConfig, String> {
    read_postprocess_config_from_disk()
}

// 保存配置
#[tauri::command]
fn save_postprocess_config(payload: SavePostprocessPayload) -> Result<bool, String> {
    let cfg = validate_and_clean_payload(payload)?;
    write_postprocess_config_to_disk(&cfg)?;
    Ok(true)
}

// -----------------------------
// 使用统计：读/写（usage_stats.json）
// -----------------------------

#[derive(Serialize, Deserialize, Debug, Clone)]
struct UsageTotals {
    #[serde(default)]
    time_saved_sec: f64,
    #[serde(default)]
    total_chars: u64,
    #[serde(default)]
    corrections: u64,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
struct UsageToday {
    date: String,
    #[serde(default)]
    time_saved_sec: f64,
    #[serde(default)]
    total_chars: u64,
    #[serde(default)]
    corrections: u64,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
struct UsageStatsFile {
    totals: UsageTotals,
    today: UsageToday,
}

fn resolve_usage_stats_path() -> PathBuf {
    let root = find_project_root_for_config();
    root.join("speak-keyboard-tauri").join("config").join("usage_stats.json")
}

fn current_date_string() -> String {
    Local::now().format("%Y-%m-%d").to_string()
}

fn default_usage_stats() -> UsageStatsFile {
    UsageStatsFile {
        totals: UsageTotals { time_saved_sec: 0.0, total_chars: 0, corrections: 0 },
        today: UsageToday { date: current_date_string(), time_saved_sec: 0.0, total_chars: 0, corrections: 0 },
    }
}

fn read_usage_stats_from_disk() -> Result<UsageStatsFile, String> {
    let path = resolve_usage_stats_path();
    if !path.exists() {
        return Ok(default_usage_stats());
    }
    match fs::read_to_string(&path) {
        Ok(content) => match serde_json::from_str::<UsageStatsFile>(&content) {
            Ok(mut stats) => {
                // 基本健壮性：字段可能缺失
                if stats.today.date.trim().is_empty() {
                    stats.today.date = current_date_string();
                }
                Ok(stats)
            }
            Err(e) => {
                println!("读取 usage_stats.json 解析失败: {}，将使用默认值", e);
                Ok(default_usage_stats())
            }
        },
        Err(e) => {
            println!("读取 usage_stats.json 失败: {}，将使用默认值", e);
            Ok(default_usage_stats())
        }
    }
}

fn write_usage_stats_to_disk(stats: &UsageStatsFile) -> Result<(), String> {
    let path = resolve_usage_stats_path();
    if let Some(dir) = path.parent() { fs::create_dir_all(dir).map_err(|e| format!("创建配置目录失败: {}", e))?; }
    let tmp_path = path.with_extension("json.tmp");
    let data = serde_json::to_string_pretty(stats).map_err(|e| format!("序列化统计失败: {}", e))?;
    {
        let mut f = fs::File::create(&tmp_path).map_err(|e| format!("创建临时文件失败: {}", e))?;
        f.write_all(data.as_bytes()).map_err(|e| format!("写入临时文件失败: {}", e))?;
        f.sync_all().ok();
    }
    match fs::rename(&tmp_path, &path) {
        Ok(_) => Ok(()),
        Err(_) => {
            let _ = fs::remove_file(&path);
            fs::rename(&tmp_path, &path).map_err(|e| format!("替换统计文件失败: {}", e))
        }
    }
}

fn rollover_today_if_needed(stats: &mut UsageStatsFile) -> bool {
    let today = current_date_string();
    if stats.today.date != today {
        stats.today.date = today;
        stats.today.time_saved_sec = 0.0;
        stats.today.total_chars = 0;
        stats.today.corrections = 0;
        true
    } else {
        false
    }
}

#[derive(Serialize)]
struct UsageStatsSnapshot {
    today_sec: f64,
    total_sec: f64,
    today_chars: u64,
    total_chars: u64,
    today_corrections: u64,
    total_corrections: u64,
}

fn accumulate_saved_time(app: &tauri::AppHandle, saved_sec: f64) -> Result<UsageStatsSnapshot, String> {
    let state = app.state::<AppState>();
    let _guard = state.usage_lock.lock().map_err(|e| format!("获取统计锁失败: {}", e))?;

    let mut stats = read_usage_stats_from_disk()?;
    let _changed = rollover_today_if_needed(&mut stats);
    let inc = if saved_sec.is_finite() && saved_sec > 0.0 { saved_sec } else { 0.0 };
    stats.today.time_saved_sec += inc;
    stats.totals.time_saved_sec += inc;
    write_usage_stats_to_disk(&stats)?;
    Ok(UsageStatsSnapshot {
        today_sec: stats.today.time_saved_sec,
        total_sec: stats.totals.time_saved_sec,
        today_chars: stats.today.total_chars,
        total_chars: stats.totals.total_chars,
        today_corrections: stats.today.corrections,
        total_corrections: stats.totals.corrections,
    })
}

fn accumulate_chars_and_corrections(app: &tauri::AppHandle, add_chars: u64, add_corrections: u64) -> Result<UsageStatsSnapshot, String> {
    let state = app.state::<AppState>();
    let _guard = state.usage_lock.lock().map_err(|e| format!("获取统计锁失败: {}", e))?;

    let mut stats = read_usage_stats_from_disk()?;
    let _changed = rollover_today_if_needed(&mut stats);
    if add_chars > 0 {
        stats.today.total_chars = stats.today.total_chars.saturating_add(add_chars);
        stats.totals.total_chars = stats.totals.total_chars.saturating_add(add_chars);
    }
    if add_corrections > 0 {
        stats.today.corrections = stats.today.corrections.saturating_add(add_corrections);
        stats.totals.corrections = stats.totals.corrections.saturating_add(add_corrections);
    }
    write_usage_stats_to_disk(&stats)?;
    Ok(UsageStatsSnapshot {
        today_sec: stats.today.time_saved_sec,
        total_sec: stats.totals.time_saved_sec,
        today_chars: stats.today.total_chars,
        total_chars: stats.totals.total_chars,
        today_corrections: stats.today.corrections,
        total_corrections: stats.totals.corrections,
    })
}

#[tauri::command]
fn get_usage_stats(app: tauri::AppHandle) -> Result<UsageStatsSnapshot, String> {
    let state = app.state::<AppState>();
    let _guard = state.usage_lock.lock().map_err(|e| format!("获取统计锁失败: {}", e))?;

    let mut stats = read_usage_stats_from_disk()?;
    let changed = rollover_today_if_needed(&mut stats);
    if changed { write_usage_stats_to_disk(&stats)?; }
    Ok(UsageStatsSnapshot {
        today_sec: stats.today.time_saved_sec,
        total_sec: stats.totals.time_saved_sec,
        today_chars: stats.today.total_chars,
        total_chars: stats.totals.total_chars,
        today_corrections: stats.today.corrections,
        total_corrections: stats.totals.corrections,
    })
}

// Tauri命令：开始录音
#[tauri::command]
async fn start_recording(_state: tauri::State<'_, AppState>, bridge: tauri::State<'_, BridgeState>) -> Result<(), String> {
    // 将 start 指令写入桥接进程
    let stdin_arc = bridge.stdin.clone();
    let mut guard = stdin_arc.lock().await;
    if let Some(stdin) = guard.as_mut() {
        println!("[tauri] 准备发送 start 指令到桥接进程");
        let payload = serde_json::json!({"cmd": "start"}).to_string() + "\n";
        if let Err(e) = stdin.write_all(payload.as_bytes()).await { println!("[tauri] 写入 start 指令失败: {}", e); return Err(format!("写入桥接进程失败: {}", e)); }
        if let Err(e) = stdin.flush().await { println!("[tauri] 刷新 start 指令失败: {}", e); return Err(format!("刷新写入失败: {}", e)); }
        println!("[tauri] start 指令已写入，等待桥接事件更新状态");
        // 状态将由事件回传更新
        Ok(())
    } else {
        Err("桥接进程未就绪，无法开始录音".to_string())
    }
}

// Tauri命令：停止录音
#[tauri::command]
async fn stop_recording(_state: tauri::State<'_, AppState>, bridge: tauri::State<'_, BridgeState>) -> Result<String, String> {
    // 将 stop 指令写入桥接进程
    let stdin_arc = bridge.stdin.clone();
    let mut guard = stdin_arc.lock().await;
    if let Some(stdin) = guard.as_mut() {
        println!("[tauri] 准备发送 stop 指令到桥接进程");
        let payload = serde_json::json!({"cmd": "stop"}).to_string() + "\n";
        if let Err(e) = stdin.write_all(payload.as_bytes()).await { println!("[tauri] 写入 stop 指令失败: {}", e); return Err(format!("写入桥接进程失败: {}", e)); }
        if let Err(e) = stdin.flush().await { println!("[tauri] 刷新 stop 指令失败: {}", e); return Err(format!("刷新写入失败: {}", e)); }
        println!("[tauri] stop 指令已写入，等待桥接事件更新状态");
        // 返回简单确认字符串，实际结果通过事件回传
        Ok("ok".to_string())
    } else {
        Err("桥接进程未就绪，无法停止录音".to_string())
    }
}

// 切换录音：以后端状态为准，避免前端状态不同步导致无法停止
#[tauri::command]
async fn toggle_recording(state: tauri::State<'_, AppState>, bridge: tauri::State<'_, BridgeState>) -> Result<(), String> {
    let currently_recording = { *state.is_recording.lock().unwrap() };
    println!("[tauri] toggle_recording 调用：当前 is_recording={}，将发送{}", currently_recording, if currently_recording { "stop" } else { "start" });
    if currently_recording {
        let stdin_arc = bridge.stdin.clone();
        let mut guard = stdin_arc.lock().await;
        if let Some(stdin) = guard.as_mut() {
            let payload = serde_json::json!({"cmd": "stop"}).to_string() + "\n";
            if let Err(e) = stdin.write_all(payload.as_bytes()).await { println!("[tauri] toggle_recording: 写入 stop 失败: {}", e); return Err(format!("写入桥接进程失败: {}", e)); }
            if let Err(e) = stdin.flush().await { println!("[tauri] toggle_recording: 刷新 stop 失败: {}", e); return Err(format!("刷新写入失败: {}", e)); }
            println!("[tauri] toggle_recording: stop 指令已写入（本地预切换为 false，最终以事件为准）");
            // 预先切换为 false，最终以事件为准
            {
                let mut rec = state.is_recording.lock().unwrap();
                *rec = false;
            }
            Ok(())
        } else {
            println!("[tauri] toggle_recording: stdin 不可用，无法发送 stop");
            Err("桥接进程未就绪，无法停止录音".to_string())
        }
    } else {
        let stdin_arc = bridge.stdin.clone();
        let mut guard = stdin_arc.lock().await;
        if let Some(stdin) = guard.as_mut() {
            let payload = serde_json::json!({"cmd": "start"}).to_string() + "\n";
            if let Err(e) = stdin.write_all(payload.as_bytes()).await { println!("[tauri] toggle_recording: 写入 start 失败: {}", e); return Err(format!("写入桥接进程失败: {}", e)); }
            if let Err(e) = stdin.flush().await { println!("[tauri] toggle_recording: 刷新 start 失败: {}", e); return Err(format!("刷新写入失败: {}", e)); }
            println!("[tauri] toggle_recording: start 指令已写入（本地预切换为 true，最终以事件为准）");
            // 预先切换为 true，最终以事件为准
            {
                let mut rec = state.is_recording.lock().unwrap();
                *rec = true;
            }
            Ok(())
        } else {
            println!("[tauri] toggle_recording: stdin 不可用，无法发送 start");
            Err("桥接进程未就绪，无法开始录音".to_string())
        }
    }
}

// Tauri命令：获取录音状态
#[tauri::command]
fn get_recording_state(state: tauri::State<'_, AppState>) -> bool {
    *state.is_recording.lock().unwrap()
}

#[derive(Clone, Serialize)]
struct RecordingHotkeyInfo {
    current: String,
}

#[derive(Deserialize)]
struct SetRecordingHotkeyPayload {
    hotkey: String,
}

#[tauri::command]
fn get_recording_hotkey(app: tauri::AppHandle, state: State<'_, AppState>) -> Result<RecordingHotkeyInfo, String> {
    let current = {
        let guard = state.recording_hotkey.lock().map_err(|e| format!("获取当前快捷键失败: {}", e))?;
        guard.clone()
    };

    if current.is_empty() {
        return Ok(RecordingHotkeyInfo {
            current: DEFAULT_RECORDING_HOTKEY.to_string(),
        });
    }

    // 确保已注册（处理第一次启动时未注册的情况）
    if let Err(err) = register_recording_hotkey(&app, &current) {
        println!("当前快捷键注册失败 {}，将尝试回退默认值: {}", current, err);
        register_recording_hotkey(&app, DEFAULT_RECORDING_HOTKEY)?;
        return Ok(RecordingHotkeyInfo {
            current: DEFAULT_RECORDING_HOTKEY.to_string(),
        });
    }

    Ok(RecordingHotkeyInfo { current })
}

#[tauri::command]
fn set_recording_hotkey(app: tauri::AppHandle, payload: SetRecordingHotkeyPayload, state: State<'_, AppState>) -> Result<bool, String> {
    let new_hotkey = payload.hotkey.trim();
    if new_hotkey.is_empty() {
        return Err("快捷键不能为空".to_string());
    }

    // 简单校验：不能只包含修饰键
    let upper = new_hotkey.to_ascii_uppercase();
    let is_only_modifier = matches!(upper.as_str(), "CTRL" | "SHIFT" | "ALT" | "META" | "COMMAND" | "CONTROL");
    if is_only_modifier {
        return Err("快捷键必须包含具体按键，例如 Ctrl+Shift+K".to_string());
    }

    register_recording_hotkey(&app, new_hotkey)?;

    // 写入配置
    let mut settings = load_ui_settings();
    settings.recording_hotkey = new_hotkey.to_string();
    save_ui_settings(&settings)?;

    // 更新状态
    {
        let mut guard = state.recording_hotkey.lock().map_err(|e| format!("更新快捷键状态失败: {}", e))?;
        *guard = new_hotkey.to_string();
    }

    // 广播给前端：快捷键已更新（优先发到 widget 窗口，找不到则广播全局）
    println!("[rust] 准备发送 recording-hotkey-updated 事件，新快捷键: {}", new_hotkey);
    if let Some(win) = app.get_webview_window("widget") {
        println!("[rust] 找到widget窗口，向其发送事件");
        match win.emit("recording-hotkey-updated", new_hotkey.to_string()) {
            Ok(_) => println!("[rust] 事件发送成功"),
            Err(e) => println!("[rust] 事件发送失败: {}", e),
        }
    } else {
        println!("[rust] 未找到widget窗口，尝试全局广播");
        match app.emit("recording-hotkey-updated", new_hotkey.to_string()) {
            Ok(_) => println!("[rust] 全局事件发送成功"),
            Err(e) => println!("[rust] 全局事件发送失败: {}", e),
        }
    }

    Ok(true)
}

// 保留占位：后续若需要在其他命令中获取当前热键可恢复此函数

fn init_recording_hotkey(app: &tauri::AppHandle, state: &State<'_, AppState>) {
    let settings = load_ui_settings();
    let hotkey = if settings.recording_hotkey.trim().is_empty() {
        DEFAULT_RECORDING_HOTKEY.to_string()
    } else {
        settings.recording_hotkey.clone()
    };

    if let Err(err) = register_recording_hotkey(app, &hotkey) {
        println!("初始化快捷键 {} 失败，将回退为默认值: {}", hotkey, err);
        if let Err(e) = register_recording_hotkey(app, DEFAULT_RECORDING_HOTKEY) {
            println!("注册默认快捷键失败: {}", e);
        }
    }

    if let Ok(mut guard) = state.recording_hotkey.lock() {
        *guard = hotkey;
    }
}

// Tauri命令：显示/隐藏窗口
#[tauri::command]
fn toggle_window_visibility(app: tauri::AppHandle, label: &str) -> Result<(), String> {
    if let Some(window) = app.get_webview_window(label) {
        if window.is_visible().unwrap_or(false) {
            window.hide().map_err(|e| e.to_string())?;
        } else {
            window.show().map_err(|e| e.to_string())?;
            window.set_focus().map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

// Tauri命令：隐藏窗口
#[tauri::command]
fn hide_window(app: tauri::AppHandle, label: &str) -> Result<(), String> {
    println!("隐藏窗口: {}", label);
    if let Some(window) = app.get_webview_window(label) {
        window.hide().map_err(|e| e.to_string())?;
        println!("窗口已隐藏");
    }
    Ok(())
}

// Tauri命令：显示窗口
#[tauri::command]
fn show_window(app: tauri::AppHandle, label: &str) -> Result<(), String> {
    println!("尝试显示窗口: {}", label);
    if let Some(window) = app.get_webview_window(label) {
        println!("窗口存在，当前可见性: {:?}", window.is_visible());
        if label == "widget" {
            // 恢复悬浮窗显示时的任务栏策略：可见时不在任务栏
            let _ = window.set_skip_taskbar(true);
        }
        window.show().map_err(|e| e.to_string())?;
        window.set_focus().map_err(|e| e.to_string())?;
        window.unminimize().map_err(|e| e.to_string())?;
        println!("窗口显示完成");
    } else {
        println!("窗口不存在: {}", label);
    }
    Ok(())
}

// Tauri命令：最小化窗口（widget 改为隐藏，settings 正常最小化）
#[tauri::command]
fn minimize_window(app: tauri::AppHandle, label: &str) -> Result<(), String> {
    if let Some(window) = app.get_webview_window(label) {
        if label == "widget" {
            // 允许悬浮窗最小化到任务栏：临时关闭 skipTaskbar，再最小化
            let _ = window.set_skip_taskbar(false);
            window.minimize().map_err(|e| e.to_string())?;
        } else {
            window.minimize().map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_autostart::init(tauri_plugin_autostart::MacosLauncher::LaunchAgent, Some(vec!["--flag1", "--flag2"])))
        .manage(AppState::default())
        .manage(BridgeState {
            stdin: Arc::new(tokio::sync::Mutex::new(None)),
            should_restart: Arc::new(AtomicBool::new(true)),
        })
        .setup(|app| {
            {
                let state = app.state::<AppState>();
                init_recording_hotkey(&app.app_handle(), &state);
            }
            // 启动 Python 桥接进程（自动探测项目根目录）
            fn find_project_root() -> Option<PathBuf> {
                let mut dir = std::env::current_dir().ok()?;
                for _ in 0..5 {
                    if dir.join("app").join("bridge.py").exists() {
                        return Some(dir);
                    }
                    if !dir.pop() { break; }
                }
                None
            }

            // 优先查找随安装包一起分发的 onedir 可执行文件（通过 Tauri 资源路径解析，安装/开发环境均兼容）
            fn find_packaged_bridge_executable(app: &tauri::AppHandle) -> Option<PathBuf> {
                #[cfg(windows)]
                let exe_name = "bridge.exe";
                #[cfg(not(windows))]
                let exe_name = "bridge";

                let rel_candidates = [
                    format!("bin/bridge/{}", exe_name),
                    format!("bridge/{}", exe_name),
                    format!("{}", exe_name),
                ];

                for rel in rel_candidates.iter() {
                    if let Ok(path) = app.path().resolve(rel, BaseDirectory::Resource) {
                        if path.exists() && path.is_file() {
                            return Some(path);
                        }
                    }
                }
                None
            }

            // 选择 Python 解释器（优先 .venv/venv/env，其次环境变量 SK_PYTHON，最后回退到系统 python）
            fn find_python_executable(project_root: &std::path::Path) -> OsString {
                if let Ok(val) = std::env::var("SK_PYTHON") {
                    if !val.trim().is_empty() {
                        return OsString::from(val);
                    }
                }
                #[cfg(windows)]
                let candidates = [
                    project_root.join(".venv").join("Scripts").join("python.exe"),
                    project_root.join("venv").join("Scripts").join("python.exe"),
                    project_root.join("env").join("Scripts").join("python.exe"),
                ];
                #[cfg(not(windows))]
                let candidates = [
                    project_root.join(".venv").join("bin").join("python3"),
                    project_root.join("venv").join("bin").join("python3"),
                    project_root.join("env").join("bin").join("python3"),
                ];
                for p in candidates.iter() {
                    if p.exists() && p.is_file() {
                        return OsString::from(p.as_os_str());
                    }
                }
                // 回退
                OsString::from("python")
            }

            let project_root = find_project_root().unwrap_or_else(|| std::env::current_dir().unwrap());
            println!("准备启动桥接进程，项目根目录: {:?}", project_root);

            let py = find_python_executable(&project_root);
            println!("将使用 Python 解释器（回退路径）: {:?}", py);

            let app_handle = app.handle().clone();
            let app_state = app.state::<AppState>();
            init_recording_hotkey(&app_handle, &app_state);

            // 循环守护：子进程退出后自动重启（带简单退避）
            let restart_flag = app.state::<BridgeState>().should_restart.clone();
            tauri::async_runtime::spawn(async move {
                let mut attempts: u32 = 0;
                loop {
                    if !restart_flag.load(Ordering::SeqCst) {
                        println!("[tauri] 收到停止重启信号，结束桥接守护循环");
                        break;
                    }
                    attempts += 1;
                    println!("[tauri] 尝试启动桥接进程（尝试次数 {}）", attempts);

                    // 优先使用随 Tauri 安装包分发的 onedir 可执行文件
                    let mut cmd = if let Some(bridge_exe) = find_packaged_bridge_executable(&app_handle) {
                        println!("[tauri] 检测到打包的 bridge 可执行文件: {:?}", bridge_exe);
                        let mut c = Command::new(&bridge_exe);
                        if let Some(dir) = bridge_exe.parent() {
                            c.current_dir(dir);
                        }
                        // 设置环境变量标识bridge模式
                        c.env("SK_BRIDGE_MODE", "1");
                        c
                    } else {
                        println!("[tauri] 未检测到打包的 bridge，可回退到 Python 启动 app.bridge");
                        let mut c = Command::new(&py);
                        c.arg("-u").arg("-m").arg("app.bridge");
                        // 可按需添加 --config / --save-dataset / --dataset-dir
                        // c.arg("--save-dataset");
                        // c.arg("--dataset-dir").arg("dataset");
                        c.current_dir(&project_root);
                        // 设置环境变量标识bridge模式
                        c.env("SK_BRIDGE_MODE", "1");
                        c
                    };

                    // Windows: 使用DETACHED_PROCESS避免性能问题
                    // CREATE_NO_WINDOW会导致进程以后台优先级运行，严重影响ONNX推理性能
                    #[cfg(windows)]
                    {
                        use std::os::windows::process::CommandExt;
                        // DETACHED_PROCESS: 子进程独立运行，不继承控制台
                        // CREATE_NEW_PROCESS_GROUP: 新进程组，避免Ctrl+C传播
                        const DETACHED_PROCESS: u32 = 0x00000008;
                        const CREATE_NEW_PROCESS_GROUP: u32 = 0x00000200;
                        cmd.creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP);
                    }

                    // 通过管道捕获 stdout（事件）与 stderr（日志），避免冒出控制台
                    cmd.stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());

                    match cmd.spawn() {
                        Ok(mut child) => {
                            println!("[tauri] 桥接进程已启动 (pid=?)，绑定stdin与事件通道");
                            // 绑定 stdin
                            {
                                let stdin_arc = {
                                    let bridge_state = app_handle.state::<BridgeState>();
                                    bridge_state.stdin.clone()
                                };
                                let mut guard = stdin_arc.lock().await;
                                *guard = child.stdin.take();
                            }

                            // 读取 stdout，逐行解析并转发事件
                            if let Some(stdout) = child.stdout.take() {
                                let mut reader = BufReader::new(stdout);
                                let mut buf: Vec<u8> = Vec::with_capacity(4096);
                                loop {
                                    buf.clear();
                                    match reader.read_until(b'\n', &mut buf).await {
                                        Ok(0) => { // EOF
                                            println!("[tauri] 桥接事件通道到达 EOF");
                                            break;
                                        }
                                        Ok(_n) => {
                                            let line = String::from_utf8_lossy(&buf);
                                            let line = line.trim();
                                            if line.is_empty() { continue; }
                                            match serde_json::from_str::<Value>(line) {
                                                Ok(val) => {
                                                    // 同步录音状态 + 统计累加
                                                    if let Some(event_name) = val.get("event").and_then(|v| v.as_str()) {
                                                        if event_name == "recording_state" {
                                                            if let Some(flag) = val.get("is_recording").and_then(|v| v.as_bool()) {
                                                                let app_state = app_handle.state::<AppState>();
                                                                let mut rec = app_state.is_recording.lock().unwrap();
                                                                *rec = flag;
                                                                println!("[tauri] 收到 recording_state 事件：is_recording={}", flag);
                                                            }
                                                        } else if event_name == "transcription_result" {
                                                            let mut changed = false;
                                                            // 节省时间
                                                            if let Some(duration) = val.get("duration").and_then(|v| v.as_f64()) {
                                                                let dur = if duration.is_sign_negative() { 0.0 } else { duration };
                                                                let saved = dur * 2.2_f64;
                                                                if let Ok(snapshot) = accumulate_saved_time(&app_handle, saved) {
                                                                    // 将最新快照先广播（后续还会覆盖一次，保持简单）
                                                                    let _ = app_handle.emit("stats-updated", serde_json::json!({
                                                                        "today_sec": snapshot.today_sec,
                                                                        "total_sec": snapshot.total_sec,
                                                                        "today_chars": snapshot.today_chars,
                                                                        "total_chars": snapshot.total_chars,
                                                                        "today_corrections": snapshot.today_corrections,
                                                                        "total_corrections": snapshot.total_corrections
                                                                    }));
                                                                    changed = true;
                                                                }
                                                            }

                                                            // 已转录字数与自动修正次数
                                                            // 仅统计 text（排除空白字符，但不排除标点）
                                                            let mut add_chars: u64 = 0;
                                                            if let Some(text) = val.get("text").and_then(|v| v.as_str()) {
                                                                let count = text.chars().filter(|c| !c.is_whitespace()).count() as u64;
                                                                add_chars = count;
                                                            }
                                                            let add_corr: u64 = val.get("corrections").and_then(|v| v.as_i64()).map(|v| if v < 0 { 0 } else { v as u64 }).unwrap_or(0);

                                                            if add_chars > 0 || add_corr > 0 {
                                                                if let Ok(snapshot) = accumulate_chars_and_corrections(&app_handle, add_chars, add_corr) {
                                                                    let _ = app_handle.emit("stats-updated", serde_json::json!({
                                                                        "today_sec": snapshot.today_sec,
                                                                        "total_sec": snapshot.total_sec,
                                                                        "today_chars": snapshot.today_chars,
                                                                        "total_chars": snapshot.total_chars,
                                                                        "today_corrections": snapshot.today_corrections,
                                                                        "total_corrections": snapshot.total_corrections
                                                                    }));
                                                                    changed = true;
                                                                }
                                                            }
                                                            if !changed {
                                                                // 至少广播一次原样数据，保持前端事件节奏一致
                                                                if let Ok(snapshot) = get_usage_stats(app_handle.clone()) {
                                                                    let _ = app_handle.emit("stats-updated", serde_json::json!({
                                                                        "today_sec": snapshot.today_sec,
                                                                        "total_sec": snapshot.total_sec,
                                                                        "today_chars": snapshot.today_chars,
                                                                        "total_chars": snapshot.total_chars,
                                                                        "today_corrections": snapshot.today_corrections,
                                                                        "total_corrections": snapshot.total_corrections
                                                                    }));
                                                                }
                                                            }
                                                        }
                                                    }
                                                    let _ = app_handle.emit("bridge-event", val);
                                                }
                                                Err(err) => {
                                                    println!("解析桥接输出失败: {} | 原始: {}", err, line);
                                                }
                                            }
                                        }
                                        Err(err) => {
                                            println!("[tauri] 读取桥接输出失败: {}，继续等待下一行", err);
                                            continue;
                                        }
                                    }
                                }
                            }

                            // 后台耗尽 stderr，避免阻塞（丢弃或按需打印）
                            if let Some(stderr) = child.stderr.take() {
                                tauri::async_runtime::spawn(async move {
                                    let mut reader = BufReader::new(stderr);
                                    let mut _buf: Vec<u8> = Vec::with_capacity(2048);
                                    loop {
                                        _buf.clear();
                                        match reader.read_until(b'\n', &mut _buf).await {
                                            Ok(0) => break, // EOF
                                            Ok(_) => {
                                                // 如需调试可 println!("[bridge stderr] {}", String::from_utf8_lossy(&_buf));
                                            }
                                            Err(_) => break,
                                        }
                                    }
                                });
                            }

                            // 等待子进程退出状态，打印退出码
                            match child.wait().await {
                                Ok(status) => {
                                    println!("[tauri] 桥接进程已退出，状态码: {:?}", status);
                                }
                                Err(e) => {
                                    println!("[tauri] 等待桥接进程退出失败: {}", e);
                                }
                            }

                            // 子进程退出：重置stdin、状态，并通知前端
                            {
                                let stdin_arc = {
                                    let bridge_state = app_handle.state::<BridgeState>();
                                    bridge_state.stdin.clone()
                                };
                                let mut guard = stdin_arc.lock().await;
                                *guard = None;
                            }
                            {
                                let app_state = app_handle.state::<AppState>();
                                let mut rec = app_state.is_recording.lock().unwrap();
                                *rec = false;
                            }
                            let _ = app_handle.emit("bridge-event", serde_json::json!({
                                "event": "bridge_shutdown",
                                "reason": "process_exit"
                            }));

                            if restart_flag.load(Ordering::SeqCst) {
                                println!("[tauri] 桥接进程已退出，准备重启...");
                            } else {
                                println!("[tauri] 桥接进程已退出，守护已停止");
                                break;
                            }
                        }
                        Err(err) => {
                            println!("启动桥接进程失败: {}", err);
                            let _ = app_handle.emit("bridge-event", serde_json::json!({
                                "event": "bridge_error",
                                "message": format!("启动失败: {}", err)
                            }));
                        }
                    }

                    // 简单退避（最多 30s）
                    if restart_flag.load(Ordering::SeqCst) {
                        let delay_secs: u64 = std::cmp::min(30, 2 * (attempts as u64));
                        println!("[tauri] {} 秒后重试启动桥接进程...", delay_secs);
                        tokio::time::sleep(Duration::from_secs(delay_secs)).await;
                    } else {
                        println!("[tauri] 守护循环收到停止指令，终止退出");
                        break;
                    }
                }
            });
            // 阻止设置窗口关闭时被销毁，改为隐藏
            if let Some(settings_window) = app.get_webview_window("settings") {
                let window_clone = settings_window.clone();
                settings_window.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        println!("设置窗口关闭请求，隐藏而不是销毁");
                        api.prevent_close();
                        let _ = window_clone.hide();
                    }
                });
            }

            // 设置系统托盘
            let quit_i = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
            let show_i = MenuItem::with_id(app, "show", "显示主窗口", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_i, &quit_i])?;
            
            let should_restart_flag = app.state::<BridgeState>().should_restart.clone();

            let should_restart_flag_clone = should_restart_flag.clone();

            let _tray = TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(move |app, event| match event.id.as_ref() {
                    "quit" => {
                        should_restart_flag_clone.store(false, Ordering::SeqCst);
                        let bridge_state = app.state::<BridgeState>();
                        let stdin_arc = bridge_state.stdin.clone();
                        let app_handle = app.clone();
                        tauri::async_runtime::spawn(async move {
                            println!("[tauri] 托盘退出：尝试发送 shutdown 指令给桥接进程");
                            let mut guard = stdin_arc.lock().await;
                            if let Some(stdin) = guard.as_mut() {
                                let payload = serde_json::json!({"cmd": "shutdown"}).to_string() + "\n";
                                if let Err(err) = stdin.write_all(payload.as_bytes()).await {
                                    println!("[tauri] 托盘退出写入 shutdown 失败: {}", err);
                                } else if let Err(err) = stdin.flush().await {
                                    println!("[tauri] 托盘退出刷新 shutdown 失败: {}", err);
                                } else {
                                    println!("[tauri] 托盘退出已发送 shutdown 指令");
                                }
                                *guard = None;
                            } else {
                                println!("[tauri] 托盘退出时 stdin 不可用，跳过 shutdown");
                            }
                            // 等待 500ms 以便桥接完成清理
                            tokio::time::sleep(Duration::from_millis(500)).await;
                            app_handle.exit(0);
                        });
                    }
                    "show" => {
                        if let Some(window) = app.get_webview_window("widget") {
                    let _ = window.set_skip_taskbar(true);
                            let _ = window.show();
                            let _ = window.set_focus();
                            let _ = window.unminimize();
                        }
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click { button: tauri::tray::MouseButton::Left, .. } = event {
                        let app = tray.app_handle();
                        if let Some(window) = app.get_webview_window("widget") {
                    let _ = window.set_skip_taskbar(true);
                            let _ = window.show();
                            let _ = window.set_focus();
                            let _ = window.unminimize();
                        }
                    }
                })
                .build(app)?;

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            start_recording,
            stop_recording,
            toggle_recording,
            get_recording_state,
            get_postprocess_config,
            save_postprocess_config,
            get_usage_stats,
            toggle_window_visibility,
            show_window,
            hide_window,
            minimize_window,
            get_autostart_enabled,
            set_autostart_enabled,
            get_recording_hotkey,
            set_recording_hotkey
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
