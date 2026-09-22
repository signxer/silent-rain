<p align="center">
  <img src="icon.png" width="112" height="112" alt="Moisten Logo">
</p>

<h1 align="center">润物 Moisten</h1>

<p align="center">
  安静、可靠的在线学习自动化工作台
</p>

<p align="center">
  <a href="https://github.com/signxer/silent-rain/releases"><img src="https://img.shields.io/github/v/release/signxer/silent-rain?style=flat-square&color=2b83f6" alt="Latest Release"></a>
  <a href="https://github.com/signxer/silent-rain/blob/main/LICENSE"><img src="https://img.shields.io/github/license/signxer/silent-rain?style=flat-square&color=18b8b4" alt="License"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.9%2B-3776ab?style=flat-square" alt="Python 3.9+"></a>
  <a href="https://playwright.dev/python/"><img src="https://img.shields.io/badge/browser-Playwright-2ead33?style=flat-square" alt="Playwright"></a>
</p>

<p align="center">
  <em>把重复的学习流程交给工具，把时间留给更重要的事情。</em>
</p>

---

## ✨ 项目简介

Moisten 是一个面向在线课程与内部培训场景的桌面自动化工具。它提供图形界面和命令行两种使用方式，用于管理课程目标、浏览器会话、学习进度和运行日志。

项目强调三个体验：

- **低打扰**：任务在后台运行，进度、状态和异常集中展示。
- **可恢复**：自动保存配置、会话和进度，支持从中断位置继续。
- **可控制**：自动模式与手动模式并存，学习过程可以随时暂停、停止或调整。

## 🌊 核心能力

| 能力 | 说明 |
| --- | --- |
| 双模式工作流 | 按目标自动安排学习，或直接输入课程地址进行精确学习 |
| 课程组件处理 | 支持视频、音频、图文、图书、外链和常见测验组件 |
| 断点续学 | 记录课程进度、页面位置和已完成内容，重启后可继续 |
| 多任务并发 | 支持配置 1–20 个工作线程，并提供实时进度反馈 |
| 无头运行 | 可在后台运行浏览器，适合长时间任务 |
| 标签筛选 | 按课程标签筛选内容，并记住上次选择 |
| 可选 AI 辅助 | 对兼容的 AI 接口进行连接测试，用于处理特定问答流程 |
| 桌面工作台 | 亮色/深色主题、统一弹窗、进度卡片、日志面板和状态提示 |
| 安全停止 | 变更配置或关闭窗口时，尽量在安全检查点停止当前任务 |

## 🖥️ 界面预览

Moisten 使用蓝青渐变、轻量卡片和清晰的信息层级，适配浅色与深色主题。

<p align="center">
  <img src="icon.png" width="72" alt="Moisten icon">
</p>

## 📦 获取与安装

### 下载打包版本

前往 [Moisten 下载页](https://signxer.github.io/Moisten/) 获取 Windows 或 macOS 版本。

打包版本无需额外安装 Python 或浏览器运行环境，下载后即可启动。下载页默认使用 gh-proxy 加速公开 Release 文件，网络不可用时可改用直连地址。

### 从源码运行

环境要求：

- Python 3.9 或更高版本
- macOS、Windows 或 Linux
- 使用内置浏览器模式时，需要 Playwright 浏览器运行文件

```bash
git clone https://github.com/signxer/silent-rain.git
cd silent-rain
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

macOS/Linux 也可以使用一键脚本：

```bash
./setup.sh
```

## 🚀 使用方式

### 图形界面

```bash
python3 gui.py
```

推荐流程：

1. 设置浏览器模式、线程数和主题。
2. 登录课程平台并保存会话。
3. 选择自动模式或手动模式。
4. 设置学习目标，或粘贴课程地址。
5. 开始任务，在仪表盘查看进度和日志。

### 命令行

```bash
# 启动任务
python3 main.py start

# 后台运行并使用 5 个工作线程
python3 main.py start --headless --workers 5

# 查看当前累计学时
python3 main.py hours
```

命令行参数未指定时，会读取图形界面保存的配置。

## ⚙️ 配置说明

常用设置包括：

- **浏览器模式**：使用系统浏览器，或使用 Playwright 管理的内置浏览器。
- **线程数量**：根据设备性能和网络情况设置并发数量。
- **学习目标**：支持总目标和差额目标两种方式。
- **手动课程**：每行输入一个课程、专题或训练页面地址。
- **AI 辅助**：仅在明确需要时开启，并在设置页配置兼容接口的访问密钥。
- **外观与动效**：支持浅色、深色和跟随系统，以及减少动效选项。

## 🔐 数据与隐私

- 账号凭据优先保存到系统钥匙串。
- 运行配置、会话状态和学习进度保存在本机用户数据目录。
- 访问密钥不会写入 README、日志或源代码；提交代码前请确认没有把本地配置文件加入 Git。
- 项目不会把学习进度主动上传到第三方服务。
- 使用 AI 辅助功能时，请根据服务提供方的条款自行判断是否适合提交相关内容。

本地数据目录：

| 平台 | 目录 |
| --- | --- |
| macOS | `~/Library/Application Support/Moisten` |
| Windows | `%APPDATA%\\Moisten` |
| Linux | `~/.config/Moisten` |

## 🧩 项目结构

```text
silent-rain/
├── gui.py          # PySide6/QFluentWidgets 图形界面
├── main.py         # 自动化引擎与命令行入口
├── ui_theme.py     # 主题、弹窗、导航和共享视觉组件
├── icon.png        # 应用品牌图标
├── icon.ico        # Windows 应用图标
├── setup.sh        # macOS/Linux 安装脚本
├── requirements.txt
├── VERSION
└── CHANGELOG.md
```

## 🛠️ 开发与构建

运行静态检查：

```bash
PYTHONPYCACHEPREFIX=/tmp/moisten-pycache python3 -m py_compile gui.py ui_theme.py main.py
git diff --check
```

本地构建示例：

```bash
pyinstaller -F -w \
  --icon=icon.ico \
  --add-data="icon.png:." \
  --add-data="VERSION:." \
  --name=Moisten gui.py
```

发布版本由 Git 标签驱动。提交前请同步更新 `VERSION` 与 `CHANGELOG.md`，并确认浅色/深色主题、弹窗、下载更新流程均可用。

## 📝 贡献指南

欢迎通过 Issue 或 Pull Request 提交改进建议。提交前请：

1. 保持功能描述与实际行为一致。
2. 为新增配置提供默认值和兼容旧配置的处理。
3. 运行编译检查与 `git diff --check`。
4. 不提交账号、访问密钥、会话文件或本地运行数据。

## 📄 许可证

本项目采用 [MIT License](LICENSE) 开源。
