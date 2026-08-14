# 更新日志

## 1.7.2
- fix(自动更新): 下载完整性校验（大小+可执行文件头）+重试，阻止损坏文件替换exe导致重启Failed to start python interpreter；替换前备份旧版
# 更新日志

## 1.7.0
- fix(ci): pyinstaller改回单行命令修复Windows构建失败；macos-13已淘汰改回macos-latest
- feat: Chromium下载GUI进度等待框；默认系统Chrome+5线程；修复_download_chromium误插init内导致编译错误
- fix: 不打包Chromium改为运行时按需下载；修复init重复启动驱动泄漏
- docs: 更新README/依赖上限/启动器依赖映射/gitignore
- ci: 打包内置Chromium、修复Info.plist、发布同步失败可见、移除签名/公证
- fix: 全面修复与优化（登录态保存/心跳恢复/跨页定位/线程安全/变更配置即停止旧任务/手动自动模式互斥等）
- chore: VERSION 1.6.4
# 更新日志

## 1.4.9
- 用户密码加密存储（XOR + base64）

## 1.4.8
- 启动时自动检查更新
- 新版本弹窗提示并跳转下载页

## 1.4.7
- 课程池空时正确判断目标是否达成
- 构建产物自动同步到 Moisten 仓库

## 1.4.6
- 版本号从 VERSION 文件自动读取
- 构建时自动更新仓库 VERSION 文件

## 1.4.5
- Windows 构建修复

## 1.4.4
- 配置文件名 ccbu → moisten
- 删除仓库内旧配置文件

## 1.4.3
- macOS DPI 缩放修复
- 学时查询独立页面，不污染主页面

## 1.4.2
- 标签筛选流程优化
- 采集重试逻辑改进

## 1.4.1
- 润物 Moisten 品牌更新
- QFluentWidgets Fluent Design 界面

## 1.4.0
- 双模式：自动学习 + 手动指定 URL
- 标签多选框 + 记住上次选择
- 启动画面
