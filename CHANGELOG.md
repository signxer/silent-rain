# 更新日志

## 2.3.5
- fix(startup): 恢复欢迎页入场动画和页面切换所需的 QGraphicsOpacityEffect 导入，修复启动时报 NameError

## 2.3.4
- fix(dashboard): worker 轮播改用图形效果位移，避免动画移动布局控件导致当前任务文字错位

## 2.3.3
- fix(ui): 表格学习进度条改为抗锯齿胶囊绘制，极小百分比也保持圆头渐变
- polish(dashboard): worker 当前任务轮播间隔延长至 6.5 秒，并加入纵向滚动淡入淡出

## 2.3.2
- polish(dashboard): 当前任务卡按固定节奏轮播正在工作的 worker，并增加淡入淡出过渡
- fix(ui): 极小进度值仍保持进度条两端圆角

## 2.3.1
- fix(network-study): 将播放列表中的多个视频拆成独立任务加入共享课程池，支持不同 worker 并行处理
- fix(network-study): 空闲 worker 等待正在展开的播放列表，避免因初始课程不足而提前退出
- fix(network-study): 为目录视频单独保存完成断点，并校验子视频路由

## 2.3.0
- polish(ui): 为侧边栏左上角品牌图标增加克制的柔和投影，保持品牌文字无阴影

## 2.2.10
- fix(network-study): 课程列表翻页兼容可见分页控件与 SPA 路由，初始课程不足时继续补充课程池，尽量启动全部配置的 worker
- fix(network-study): 完成课程后刷新队列与学时状态，并展示 worker 的检查、取课和等待状态
- fix(dashboard): 当前任务标题固定单行并省略超长内容，避免卡片高度跳动
- fix(dashboard): 学习进度百分比字号与列宽协调，防止数字裁切

## 2.2.9
- fix(ui): 学习目标设置页统一为居中软盘图标「保存」按钮，移除跳过操作
- fix(ui): 退出确认弹窗取消按钮统一样式并清除误导性的焦点框
- fix(network-study): 增加课程详情/播放页识别、学习入口等待与视频探测诊断，兼容更多入口状态
- fix(network-study): 记录带时间戳且过滤 URL 参数的诊断日志，便于排查浏览器与 worker 异常

## 2.2.8
- fix(ui): 增加退出确认弹窗底部留白并统一按钮尺寸
- fix(dashboard): 加宽学习进度列，保留课程名称可读空间
- fix(network-study): 课程列表加载或翻页暂时失败时重试并延后课程，避免 worker 队列过早结束
- fix(network-study): 课程卡片可用时直达详情或播放页，限制列表回退并发

## 2.2.7
- fix(网络自学): 兼容课程列表直接进入播放页、详情页学习入口及新窗口播放；总进度 100% 时跳过，不再反复刷新详情页
- fix(更新): 修复 Windows 更新启动路径错误与旧版 EXE 替换失败，改进学习任务关闭和异常提示
- test: Windows、macOS 构建前运行课程入口与更新回归测试

## 2.2.6
- release: v2.2.6 sync goal progress indicators
- chore: VERSION 2.2.5
- docs: CHANGELOG 2.2.5
# 更新日志

## 2.2.6
- fix(dashboard): 学习目标条形进度与圆形进度统一按目标学时计算
- fix(dashboard): 避免当前课程进度覆盖整体学习目标进度

## 2.2.5
- release: v2.2.5 visible study waves
- chore: VERSION 2.2.4
- docs: CHANGELOG 2.2.4
- chore: VERSION 2.2.3
- docs: CHANGELOG 2.2.3
# 更新日志

## 2.2.5
- fix(motion): 增强学习背景与当前任务卡的横向流动波浪，让动效更易察觉
- fix(motion): 将波浪循环缩短至约 6 秒，并继续遵循“减少动效”设置

## 2.2.4
- release: v2.2.4 motion and icon fixes
- chore: VERSION 2.2.2
- docs: CHANGELOG 2.2.2
# 更新日志

## 2.2.4
- fix(build): 从新版品牌 PNG 重新生成多尺寸 Windows ICO，并在 CI 构建前自动同步
- feat(motion): 增加可中断的进度条平滑过渡与学习背景流体波浪动效
- feat(motion): 波浪动效同步覆盖当前任务卡，并遵循“减少动效”设置

## 2.2.3
- fix(dashboard): 修复学习目标进度环动画未更新导致始终显示 0%
- fix(ui): 隐藏重复弹窗标题并压缩按钮区间距，改善确认窗口布局
- fix(table): 缩窄进度与预计列、扩大课程列，并压缩表头高度
- docs: 重写 README，补充 Logo、安装、配置、隐私和开发说明

## 2.2.2
- release: v2.2.2 unified dialog styling
- chore: VERSION 2.2.0
- docs: CHANGELOG 2.2.0
# 更新日志

## 2.2.2
- fix(ui): 统一退出确认、停止学习、标签选择、版本更新等弹窗的卡片、按钮和深浅主题样式

## 2.2.1
- fix(ui): 重新排列侧栏设置顺序，统一学习方式页边距并隐藏侧栏设置中的向导步骤条
- fix(ui): 外观设置改为主题和动效变更即时保存，修复跟随系统时深浅主题混杂
- fix(ui): 训练营任务补充预计时间计算，并调整进度与预计列宽
- fix(exit): 关闭窗口时取消卡住的 worker asyncio 主任务，确保 Playwright 和学习线程能够退出

# 更新日志

## 2.2.0
- feat(ui): 使用 QFluentWidgets 官方 NavigationInterface，侧栏拆分账号、学习、运行、考试和外观设置
- feat(ui): 学习目标/手动学习根据当前模式动态显示，统一菜单宽度、图标基线和品牌图标展示
- fix(ui): 修复深色主题侧栏图标颜色和主题下拉列表背景异常
- fix(update): 更新前等待 worker、Playwright 浏览器和会话安全关闭，避免更新后首次启动无法学习

## 2.1.0
- feat(ui): 新增青黛润物设计系统，支持跟随系统、浅色和深色主题
- feat(ui): 首次使用增加步骤导航，学习中启用统一侧栏工作台
- feat(仪表盘): 当前任务卡、worker 进度条、运行时长、停止操作和可折叠日志
- feat(交互): 配置、目标、手动 URL 增加摘要、校验、空状态和减少动效选项
- refactor(ui): 统一页面标题、卡片、状态标签、按钮反馈和跨平台窗口布局

## 2.0.0
- fix(学习流程): 失败课程不再被误报为阶段完成，课程池动态补充会纳入总数并按稳定课程键断点续学
- fix(并发): 心跳超时会协作取消当前播放后再重试，采集页按实际页面数限流，避免页面竞争和重复学习
- fix(进度): 仅以平台进度或明确完成标志确认视频完成，进度文件改为原子写入，降低误判和损坏风险
- fix(资源): 统一清理浏览器页面、上下文和事件循环后台任务，GUI 退出等待未结束任务
- fix(稳定性): 课程列表加载支持瞬时重试，CLI/GUI worker 数量限制为 1-20，失败状态和课程链接匹配更准确

## 1.9.10
- perf(训练营): 作业页与不可播视频不再白等，worker 占用从分钟级降到秒级
- chore: VERSION 1.9.9
- docs: CHANGELOG 1.9.9
# 更新日志

## 1.9.9
- fix(训练营): 图书「开始阅读」被弹窗遮罩拦住点不到，改用清遮罩+JS 兜底点击
- chore: VERSION 1.9.8
- docs: CHANGELOG 1.9.8
# 更新日志

## 1.9.8
- feat(考试): 未通过时弹窗询问是否重考，倒计时不操作则默认不重考
- chore: VERSION 1.9.7
- docs: CHANGELOG 1.9.7
# 更新日志

## 1.9.7
- feat(考试): 支持认证考试与模拟自测（含 /shamexam 路由）
- chore: VERSION 1.9.6
- docs: CHANGELOG 1.9.6
# 更新日志

## 1.9.6
- fix(训练营): 弹窗遮罩挡住「完成学习」导致点击超时、课程误判未完成
- chore: VERSION 1.9.5
- docs: CHANGELOG 1.9.5
# 更新日志

## 1.9.5
- feat(训练营): 图书/图文自动完成、作业跳过；已通过的考试不再重考
- chore: VERSION 1.9.4
- docs: CHANGELOG 1.9.4
# 更新日志

## 1.9.4
- fix(训练营): 视频与考试同页时先看视频再考试，修复「只考试没看视频」
- chore: VERSION 1.9.3
- docs: CHANGELOG 1.9.3
- fix(考试): 快速作答未真正关闭思考模式，测试连接在深度思考下随机失败
- chore: VERSION 1.9.2
- docs: CHANGELOG 1.9.2
# 更新日志

## 1.9.3
- fix(考试): 快速作答未真正关闭思考模式，测试连接在深度思考下随机失败
# 更新日志

## 1.9.1
- fix(训练营): 目录项区分标题页与分组标题，修复分组页被判「未完成」
- chore: VERSION 1.9.0
- docs: CHANGELOG 1.9.0
# 更新日志

## 1.9.0
- feat(考试): DeepSeek 自动答题 + 训练营视频组件按 DOM 发现播放器
- chore: VERSION 1.8.1
- docs: CHANGELOG 1.8.1
# 更新日志

## 1.8.1
- chore: VERSION 1.8.1
- fix(训练营): 保留播放器上报进度
- chore: VERSION 1.8.0
- docs: CHANGELOG 1.8.0
# 更新日志

## 1.8.0
- feat(训练营): 支持自定义播放器学习流程
- chore: VERSION 1.7.9
- docs: CHANGELOG 1.7.9
# 更新日志

## 1.7.9
- fix(网络自学): worker共享课程池并按需补充课程
- chore: VERSION 1.7.8
- docs: CHANGELOG 1.7.8
# 更新日志

## 1.7.8
- chore: VERSION 1.7.8
- chore: 欢迎页副标题、URL提示、脚本注释、日志文件名去品牌化
- chore: VERSION 1.7.7
- docs: CHANGELOG 1.7.7
- chore: VERSION 1.7.6
- docs: CHANGELOG 1.7.6
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
