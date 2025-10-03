# Rust 环境配置指南 (Windows)

## 问题诊断

如果遇到 `cargo: 无法将"cargo"项识别` 错误，说明：
1. ✅ Rustup 已安装
2. ❌ 环境变量还未生效，或者toolchain未安装

## 解决方案

### 方案1：重启PowerShell（最简单）

**关闭当前的PowerShell/终端窗口，然后重新打开**

重新打开后，运行：
```powershell
cargo --version
```

如果显示版本号（如 `cargo 1.xx.x`），说明成功！直接跳到**验证安装**部分。

---

### 方案2：手动运行Rustup初始化

如果方案1不行，在PowerShell中运行：

```powershell
# 1. 检查rustup是否存在
rustup --version

# 2. 如果rustup能运行，安装stable工具链
rustup install stable
rustup default stable

# 3. 添加cargo到当前会话PATH
$env:Path += ";$env:USERPROFILE\.cargo\bin"

# 4. 验证
cargo --version
```

---

### 方案3：完全重新安装Rust（彻底解决）

如果以上都不行，按照以下步骤：

#### 1. 下载Rustup安装器

访问官网下载：
- 🌐 官方地址: https://rustup.rs/
- 🌐 中国镜像: https://mirrors.tuna.tsinghua.edu.cn/help/rustup/

或者直接下载：
https://static.rust-lang.org/rustup/dist/x86_64-pc-windows-msvc/rustup-init.exe

#### 2. 运行安装器

双击 `rustup-init.exe`，然后：
1. 看到提示后，输入 `1` 然后按回车（选择默认安装）
2. 等待下载和安装完成
3. 看到 "Rust is installed now. Great!" 说明成功

#### 3. 重启终端

**关闭所有PowerShell/命令提示符/VS Code窗口，重新打开**

#### 4. 验证安装

```powershell
rustc --version
cargo --version
```

应该看到类似输出：
```
rustc 1.75.0 (82e1608df 2023-12-21)
cargo 1.75.0 (1d8b05cdd 2023-11-20)
```

---

## 安装C++构建工具（必需！）

Tauri在Windows上还需要**Microsoft C++ Build Tools**。

### 检查是否已安装

运行：
```powershell
cl.exe
```

如果提示找不到命令，需要安装。

### 安装方法

#### 方法A：Visual Studio Build Tools（推荐）

1. 下载：https://visualstudio.microsoft.com/zh-hans/visual-cpp-build-tools/
2. 运行安装器
3. 选择 **"Desktop development with C++"**（使用C++的桌面开发）
4. 点击安装（约6GB）

#### 方法B：完整Visual Studio

如果你已经安装了Visual Studio 2019/2022，确保安装了 "C++桌面开发" 工作负荷。

---

## 验证完整环境

运行以下所有命令确认环境就绪：

```powershell
# 1. 检查Rust
rustc --version
cargo --version

# 2. 检查Node.js
node --version
npm --version

# 3. 尝试运行Tauri
cd speak-keyboard-tauri  # 如果还没在项目目录
npm run tauri dev
```

---

## 加速Rust下载（可选，推荐）

如果下载很慢，配置国内镜像：

### 1. Rustup镜像

创建或编辑文件 `%USERPROFILE%\.cargo\config.toml`：

```toml
[source.crates-io]
replace-with = 'tuna'

[source.tuna]
registry = "https://mirrors.tuna.tsinghua.edu.cn/git/crates.io-index.git"

[registries.crates-io]
protocol = "sparse"
```

### 2. 环境变量（临时）

在PowerShell中设置：
```powershell
$env:RUSTUP_DIST_SERVER = "https://mirrors.tuna.tsinghua.edu.cn/rustup"
$env:RUSTUP_UPDATE_ROOT = "https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup"
```

---

## 常见问题

### Q: 安装后还是提示 "program not found"？
**A**: 
1. 确保重启了终端
2. 确保重启了VS Code（如果在VS Code终端中运行）
3. 可能需要重启电脑

### Q: 编译时出现 "linker 'link.exe' not found"？
**A**: 没有安装C++ Build Tools，参考上面的安装步骤

### Q: 首次编译很慢？
**A**: 正常现象，Rust会编译所有依赖（可能需要5-15分钟）。之后增量编译会很快。

### Q: 安装空间不够？
**A**: 
- Rust工具链约 2GB
- VS Build Tools约 6GB
- 项目编译缓存约 2-3GB
- **总计需要约 10GB 空间**

---

## 快速检查脚本

将以下内容保存为 `check-env.ps1`，然后运行：

```powershell
# check-env.ps1
Write-Host "=== 检查Tauri开发环境 ===" -ForegroundColor Cyan

Write-Host "`n[1] 检查 Rust..." -ForegroundColor Yellow
try {
    $rustc = cargo --version
    Write-Host "  ✓ $rustc" -ForegroundColor Green
} catch {
    Write-Host "  ✗ Cargo 未找到" -ForegroundColor Red
}

Write-Host "`n[2] 检查 Node.js..." -ForegroundColor Yellow
try {
    $node = node --version
    Write-Host "  ✓ Node $node" -ForegroundColor Green
} catch {
    Write-Host "  ✗ Node.js 未找到" -ForegroundColor Red
}

Write-Host "`n[3] 检查 C++ 构建工具..." -ForegroundColor Yellow
try {
    $env:Path += ";C:\Program Files\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC\*\bin\Hostx64\x64"
    $env:Path += ";C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\VC\Tools\MSVC\*\bin\Hostx64\x64"
    cl.exe /? > $null 2>&1
    Write-Host "  ✓ C++ Build Tools 已安装" -ForegroundColor Green
} catch {
    Write-Host "  ✗ C++ Build Tools 未找到" -ForegroundColor Red
}

Write-Host "`n=== 检查完成 ===" -ForegroundColor Cyan
```

运行：
```powershell
powershell -ExecutionPolicy Bypass -File check-env.ps1
```

---

## 下一步

环境配置完成后，运行：

```bash
npm run tauri dev
```

应该能看到应用启动了！🎉

