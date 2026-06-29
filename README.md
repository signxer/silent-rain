# 润物细无声 CCBU-Auto

> 随风潜入夜，润物细无声。—— 杜甫《春夜喜雨》

建行大学自动学习工具，支持 GUI 和命令行两种模式。

## 功能

- **自动登录** — 记住账号密码，下次自动填充
- **双模式学习** — 自动模式（按学时目标学习）/ 手动模式（指定专题班 URL）
- **双目标支持** — 集中培训、网络自学独立设置，支持「总学时」和「差额补修」两种计算方式
- **标签筛选** — 按标签筛选专题班，支持多选，记住上次选择
- **多线程并发** — 可配置 1-20 个工作线程同时学习
- **无头模式** — 后台运行，不显示浏览器界面
- **断点续学** — 记住学习进度和页码，下次自动从上次位置继续
- **Fluent Design 界面** — 基于 QFluentWidgets，支持深色/浅色主题自适应

## 安装

```bash
git clone https://github.com/signxer/CCBU-Auto.git
cd CCBU-Auto
pip install -r requirements.txt
playwright install chromium
```

Mac 用户也可运行 `./setup.sh` 一键安装。

### 下载打包版本

从 [Releases](https://github.com/signxer/CCBU-Auto/releases) 下载对应平台的可执行文件。

**首次运行前**需要先安装 Playwright 浏览器引擎：

```bash
pip3 install playwright
playwright install chromium
```

## 使用

### GUI 界面（推荐）

```bash
python3 gui.py
```

启动后按界面引导操作：配置 → 登录 → 选择模式 → 设置目标 → 开始学习。

### 命令行

```bash
python3 main.py start
python3 main.py start --headless --workers 5
python3 main.py hours
```

## 界面预览

```
┌─ 润物细无声 CCBU-Auto ──────────────────────────────────┐
│                                                         │
│  ┌─ 培训学时 ────┐ ┌─ 学习目标 ────┐                   │
│  │ 集中: 82.5 学时│ │ 集中总193学时 │   ┌─ 日志 ────┐  │
│  │ 网络: 6.0 学时│ │    ╭─────╮    │   │ [10:23] .. │  │
│  │ 更新: 09:40  │ │    │42.7%│    │   │ [10:24] .. │  │
│  └───────────────┘ │    ╰─────╯    │   │ [10:25] .. │  │
│                     └──────────────┘   └────────────┘  │
│  ┌─ 学习进度 ──────────────────────────┐               │
│  │ 课程              │ 进度 │ 预计 │ 状态│               │
│  │ 正确用人导向...    │ 90%  │ 9m   │学习中│               │
│  │ 正确政绩观...      │ 45%  │ 8m   │学习中│               │
│  └─────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────┘
```

## 配置文件

| 文件 | 说明 |
|------|------|
| `ccbu_config.json` | 运行配置（线程数、无头模式、学习目标） |
| `ccbu_credentials.json` | 账号密码 |
| `ccbu_progress.json` | 学习进度和已完成专题班 |
| `ccbu_session.json` | 浏览器会话状态 |
| `ccbu_tags.json` | 标签筛选状态 |

## 打包

GitHub Actions 自动打包 Windows EXE：

```bash
# 手动打包
pyinstaller -F -w --icon=icon.ico --add-data="icon.png;." --name=CCBU-Auto-GUI gui.py
```

## 许可证

MIT License
