# 润物 Moisten

> 随风潜入夜，润物细无声。—— 杜甫《春夜喜雨》

自动学习工具，支持 GUI 和命令行两种模式。

## 功能

- **自动登录** — 记住账号密码（系统钥匙串存储），下次自动填充
- **双模式学习** — 自动模式（按学时目标学习）/ 手动模式（指定专题班或课程 URL）
- **双目标支持** — 集中培训、网络自学独立设置，支持「总学时」和「差额补修」两种计算方式
- **网络自学走课程列表** — 网络自学从 `u.ccb.com/course/#/list/1` 直接找课程，不走专题班
- **标签筛选** — 按标签筛选专题班，支持多选，记住上次选择
- **多线程并发** — 可配置 1-20 个工作线程同时学习
- **无头模式** — 后台运行，不显示浏览器界面
- **断点续学** — 记住学习进度和页码，下次自动从上次位置继续
- **Fluent Design 界面** — 基于 QFluentWidgets，支持深色/浅色主题自适应

## 安装（源码运行）

```bash
git clone https://github.com/signxer/silent-rain.git
cd silent-rain
pip install -r requirements.txt
python -m playwright install chromium
```

Mac 用户也可运行 `./setup.sh` 一键安装。

## 下载打包版本

从 [Releases](https://github.com/signxer/silent-rain/releases) 下载对应平台的可执行文件（发布仓库：https://github.com/signxer/Moisten）。

**打包版本不内置浏览器**：首次使用「内置 Chromium」模式时，应用会自动下载 Chromium 到系统缓存目录（约 200MB，仅一次）；也可以改用系统已安装的 Chrome 浏览器，无需下载。

**macOS 用户**：由于未进行 Apple 开发者签名，首次打开可能提示"已损坏"，需要在终端执行：

```bash
xattr -cr /path/to/Moisten.app
```

或右键 → 打开 → 仍然打开。当前构建为 Apple Silicon（M1/M2/M3/M4）版本，Intel Mac 暂不支持。

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

## 数据文件

打包版本数据存于用户数据目录（macOS：`~/Library/Application Support/Moisten`，Windows：`%APPDATA%\Moisten`），源码运行则存于项目目录：

| 文件 | 说明 |
|------|------|
| `moisten_config.json` | 运行配置（线程数、无头模式、学习目标） |
| `moisten_credentials.json` | 账号（密码存系统钥匙串，此文件仅存混淆兜底） |
| `moisten_progress.json` | 学习进度和已完成专题班 |
| `moisten_session.json` | 浏览器会话状态 |
| `moisten_tags.json` | 标签筛选状态 |

## 打包

GitHub Actions 在推送 `v*` 标签时自动打包 Windows EXE + macOS DMG 并同步发布到 Moisten 仓库：

```bash
# 手动打包（需先 playwright install chromium 并配置浏览器路径）
pyinstaller -F -w --icon=icon.ico --add-data="icon.png;." --add-data="VERSION;." --name=Moisten gui.py
```

## 许可证

MIT License
