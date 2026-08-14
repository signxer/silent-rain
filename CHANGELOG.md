# 更新日志

## 1.7.7
- chore: VERSION 1.7.7
- fix(自动更新): 更新后exe文件名保留命名风格、版本号换成新版本（Moisten-1.7.5→1.7.6），无版本号稳定名保持不变
# 更新日志

## 1.7.5
- chore: VERSION 1.7.5
- fix(ui): 卡片viewLayout为QHBoxLayout导致双层边距叠加+高度计算异常——边距只设卡片层、内层归零；文字/输入框/按钮显式高度杜绝裁切；内容贴分割线
- chore: VERSION 1.7.4
- docs: CHANGELOG 1.7.4
# 更新日志

## 1.7.4
- fix(手动模式): 手动页回填已保存URL（重启后可见可用）；_load_saved_config提前恢复模式与URL；chore: VERSION 1.7.4
- fix(ui): 移除无单位line-height导致的文字裁切；调大窗口默认/最小尺寸(1080x720/900x640)避免内容被挤压
- feat(自动更新): Windows改为同目录更新——新exe下载到安装目录直接启动，新实例启动时删除旧版并改回规范名，绕开临时目录+覆盖运行中exe的问题；macOS修复DMG误覆盖二进制的损坏逻辑（改为打开下载页）
- chore: VERSION 1.7.3
- docs: CHANGELOG 1.7.3
# 更新日志

## 1.7.3
- chore: VERSION 1.7.3
- ui: 修复模式选择卡片排版（内容贴分割线、按钮留底边距、卡片不拉伸）；浏览器设置行间距加大
- chore: VERSION 1.7.2
- docs: CHANGELOG 1.7.2
- fix(自动更新): 下载完整性校验（大小+可执行文件头）+重试，阻止损坏文件替换exe导致重启Failed to start python interpreter；替换前备份旧版
- chore: VERSION 1.7.1
- docs: CHANGELOG 1.7.1
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
