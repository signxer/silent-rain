#!/usr/bin/env python3
import asyncio
import json
import os
import platform
import queue
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from typing import List, Dict, Optional

# Windows需要ProactorEventLoop才能支持subprocess等
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# 跨平台快捷键
_SELECT_ALL = "Meta+a" if platform.system() != "Windows" else "Control+a"

# Windows ANSI转义码支持
if platform.system() == "Windows":
    try:
        import colorama
        colorama.init()
    except ImportError:
        pass

import click
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser, Page, BrowserContext
from rich.console import Console
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn
from rich.table import Table

load_dotenv()
console = Console()


class GoalReached(Exception):
    """学习目标已达成，通知上层清理退出"""
    pass


def _kill_playwright_chrome():
    """清理Playwright残留的Chrome进程（不影响用户自己的浏览器）"""
    import subprocess
    try:
        if sys.platform == "win32":
            # 用PowerShell只杀带--remote-debugging的Chrome（Playwright启动的）
            subprocess.run(
                ["powershell", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" "
                 "| Where-Object {$_.CommandLine -match '--remote-debugging'} "
                 "| Stop-Process -Force"],
                stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=5)
        else:
            subprocess.run(["pkill", "-f", "chrome-headless-shell"],
                          stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=5)
            subprocess.run(["pkill", "-f", "chromium.*--remote-debugging"],
                          stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=5)
    except:
        pass


# 存储文件路径（基于脚本/可执行文件所在目录，兼容PyInstaller打包）
if getattr(sys, 'frozen', False):
    _BASE_DIR = os.path.dirname(sys.executable)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STORAGE_STATE_PATH = os.path.join(_BASE_DIR, "moisten_session.json")
USER_CREDENTIALS_PATH = os.path.join(_BASE_DIR, "moisten_credentials.json")
TAGS_STATE_PATH = os.path.join(_BASE_DIR, "moisten_tags.json")
CONFIG_PATH = os.path.join(_BASE_DIR, "moisten_config.json")
PROGRESS_PATH = os.path.join(_BASE_DIR, "moisten_progress.json")


def safe_print(text, style=None):
    """安全的打印，避免Rich Markup错误"""
    try:
        if style:
            console.print(text, style=style)
        else:
            console.print(text)
    except Exception:
        # 如果Rich解析失败时，直接打印
        print(text)


DEBUG_LOG = "ccbu_debug.log"

def init_debug_log():
    # 清空调试日志
    try:
        with open(DEBUG_LOG, "w", encoding="utf-8") as f:
            f.write(f"=== CCBU-Auto Debug Log ===\n")
    except:
        pass

def debug(msg: str):
    # 写入调试日志，不显示在控制台
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"{msg}\n")
    except:
        pass

# === 常驻 stdin 读取线程 ===
# 单一线程读 stdin，通过 queue 分发给各 async_input 调用，
# 彻底避免多线程竞争 stdin 导致输入丢失。
_stdin_line_q = queue.Queue()


def _stdin_reader_thread():
    """常驻后台线程：持续读取 stdin 每一行，放入队列（保留原始输入）"""
    while True:
        try:
            line = input()
            _stdin_line_q.put(line.strip())
        except EOFError:
            break
        except Exception:
            break


# 仅CLI模式启动stdin读取线程，GUI导入时不应启动（避免与PyQt5竞争stdin）
if __name__ == "__main__" or "main" in sys.argv[0]:
    _stdin_thread = threading.Thread(target=_stdin_reader_thread, daemon=True)
    _stdin_thread.start()


async def async_input(prompt: str, default: str = "y", timeout: int = 5,
                       block: bool = False, raw: bool = False, password: bool = False) -> str:
    """带超时的输入，从常驻 stdin 队列取数据。

    Args:
        prompt: 提示文字
        default: 超时后的默认值
        timeout: 超时秒数
        block: True=无限等待（用于"按回车键继续"类场景）
        raw: True=不 strip/lowercase（用于用户名等需要保留原始输入的场景）
        password: True=输入后清除行（密码输入用）
    """
    if block:
        console.print(prompt, style="yellow", end="")
    else:
        console.print(f"{prompt}（{timeout}秒后自动: {default}）", style="yellow", end="")

    try:
        if block:
            line = await asyncio.get_event_loop().run_in_executor(
                None, _stdin_line_q.get)
        else:
            line = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _stdin_line_q.get(timeout=timeout))
        # 密码模式：清除刚输入的那一行
        if password:
            sys.stdout.write("\033[A\033[K")  # 上移一行并清除
            sys.stdout.flush()
        return line if raw else (line.strip().lower() if line else default)
    except queue.Empty:
        console.print(f"\n[超时，自动: {default}]", style="yellow")
        return default


class CCBULearner:
    def __init__(self, headless: bool = False, workers: int = 1, browser: str = "chromium"):
        self.headless = headless
        self.workers = workers
        self.browser_type = browser  # "chromium" or "chrome"
        self.playwright = None
        self.browser = None
        self.context = None
        self.pages: List[Page] = []
        self.study_hours = 0.0
        self.target_hours = 0.0
        self.tags_to_learn = []
        self.study_goal = 0.0  # 学习目标学时
        self.goal_type = 'central'  # 目标类型: central=集中培训 online=网络自学
        self.goal_reached = False
        self.user_data = {}

    async def init(self, log_callback=None, chrome_path=""):
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))

        # PyInstaller 打包后，让 Playwright 在可执行文件目录查找浏览器
        if getattr(sys, 'frozen', False):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"

        # 启动 Playwright
        try:
            self.playwright = await async_playwright().start()
        except Exception as e:
            err_msg = str(e)
            if "Connection closed" in err_msg or "driver" in err_msg.lower():
                _log("Playwright 驱动异常，请在终端运行：", "red")
                if sys.platform == "win32":
                    _log("  pip install playwright && python -m playwright install chromium", "yellow")
                else:
                    _log("  pip3 install playwright && python3 -m playwright install chromium", "yellow")
            raise

        # 检测内置 Chromium 是否可用（仅在使用内置 Chromium 时检测）
        if self.browser_type != "chrome":
            try:
                test_browser = await self.playwright.chromium.launch(headless=True)
                await test_browser.close()
            except Exception as e:
                err_msg = str(e)
                if "Executable doesn't exist" in err_msg or "Browser" in err_msg:
                    if getattr(sys, 'frozen', False):
                        _log("未找到 Chromium 浏览器，请在终端运行：", "red")
                        if sys.platform == "win32":
                            _log("  pip install playwright && python -m playwright install chromium", "yellow")
                        else:
                            _log("  pip3 install playwright && python3 -m playwright install chromium", "yellow")
                        raise RuntimeError("Chromium not installed")
                    else:
                        _log("首次运行，正在安装 Chromium 浏览器...", "blue")
                        import subprocess
                        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
                else:
                    raise

        # 清理Playwright残留的chromium进程（不影响用户自己的浏览器）
        _kill_playwright_chrome()

        self.playwright = await async_playwright().start()
        launch_opts = {"headless": self.headless}
        use_system_chrome = False

        if self.browser_type == "chrome":
            # 用户选择使用系统 Chrome
            if chrome_path and os.path.exists(chrome_path):
                # 用户手动指定了路径
                launch_opts["executablePath"] = chrome_path
                use_system_chrome = True
                _log(f"使用指定 Chrome: {chrome_path}", "green")
            elif sys.platform == "win32":
                # Windows 自动检测
                chrome_paths = [
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                ]
                for path in chrome_paths:
                    if os.path.exists(path):
                        launch_opts["channel"] = "chrome"
                        use_system_chrome = True
                        _log("使用系统 Chrome", "green")
                        break
                if not use_system_chrome:
                    try:
                        subprocess.run(["where", "chrome"], check=True, capture_output=True, timeout=3)
                        launch_opts["channel"] = "chrome"
                        use_system_chrome = True
                        _log("使用系统 Chrome (PATH)", "green")
                    except:
                        pass
            else:
                # macOS/Linux
                launch_opts["channel"] = "chrome"
                use_system_chrome = True
                _log("使用系统 Chrome", "green")

            if not use_system_chrome:
                _log("未找到系统 Chrome，改用内置 Chromium", "yellow")

        if not use_system_chrome:
            _log("使用内置 Chromium", "yellow")

        try:
            self.browser = await self.playwright.chromium.launch(**launch_opts)
        except Exception as e:
            if use_system_chrome:
                console.print("系统Chrome启动失败，改用内置Chromium", style="yellow")
                launch_opts.pop("channel", None)
                self.browser = await self.playwright.chromium.launch(**launch_opts)
            else:
                raise
        
        # 创建浏览器上下文（不硬编码user_agent，让Playwright自动匹配当前OS）
        context_opts = {
            "viewport": {"width": 1920, "height": 1080},
        }
        if os.path.exists(STORAGE_STATE_PATH):
            try:
                self.context = await self.browser.new_context(
                    storage_state=STORAGE_STATE_PATH, **context_opts
                )
                console.print("已加载保存的会话", style="green")
            except Exception as e:
                console.print("加载会话失败，创建新会话", style="yellow")
                self.context = await self.browser.new_context(**context_opts)
        else:
            self.context = await self.browser.new_context(**context_opts)

        for i in range(self.workers):
            page = await self.context.new_page()
            self.pages.append(page)

    async def close(self):
        # 先关闭所有页面和弹窗
        if self.context:
            try:
                for p in self.context.pages:
                    try:
                        await p.close()
                    except:
                        pass
                await self.context.close()
            except:
                pass
        
        # 保存会话状态
        try:
            if self.context:
                await self.context.storage_state(path=STORAGE_STATE_PATH)
                console.print("会话已保存", style="green")
        except:
            console.print("保存会话失败", style="yellow")
        
        # 关闭浏览器
        if self.browser:
            try:
                await self.browser.close()
            except:
                pass
        
        # 停止Playwright
        if self.playwright:
            try:
                await self.playwright.stop()
            except:
                pass
        
        # 强制结束Playwright残留进程
        _kill_playwright_chrome()

    async def check_login_status(self, page: Page) -> bool:
        """检查是否已登录 - 通过页面真实DOM状态检测"""
        try:
            console.print("正在检查登录状态...", style="blue")
            await page.goto("https://u.ccb.com/portal/#/study",
                            wait_until="domcontentloaded", timeout=30000)
            # SPA 可能需要额外时间渲染，等待关键元素出现
            await page.wait_for_timeout(5000)
            
            current_url = page.url
            debug(f"当前URL: {current_url}")
            
            # 1) 检查是否被重定向到统一登录页
            if "/sys/#/login" in current_url:
                console.print("被重定向到登录页面，判定未登录", style="yellow")
                return False
            
            # 2) 检查未登录标志元素（访客模式下页面特有）
            try:
                notlogin_tips = await page.locator(".cuWeb-swipe-web-info-notlogin-tips").count()
                notlogin_btn = await page.locator(".cuWeb-swipe-web-info-notlogin-btn").count()
                if notlogin_tips > 0 or notlogin_btn > 0:
                    console.print('发现未登录提示“登录跟进你的学习进度”，判定未登录', style='yellow')
                    return False
            except:
                pass
            
            # 3) 检查用户盒子：显示"登录"=未登录，显示用户名=已登录
            try:
                user_box_text = await page.locator(".ccb-user-box").inner_text(timeout=3000)
                if "登录" in user_box_text and len(user_box_text.strip()) < 10:
                    console.print("用户盒子显示「登录」，判定未登录", style="yellow")
                    return False
            except:
                pass
            
            # 4) 页面文本兜底判断
            page_text = await page.locator("body").inner_text(timeout=5000)
            if "立即登录" in page_text and "0学时" in page_text:
                console.print("页面显示「立即登录」且学时为0，判定未登录", style="yellow")
                return False
            
            # 5) 通过以上所有检查，再看是否有真实用户学习数据
            if "学时" in page_text and "学员" in page_text and "立即登录" not in page_text:
                console.print("检测到真实用户数据，判定已登录", style="green")
                return True
            
            console.print("未能确认登录状态，默认判定未登录", style="yellow")
            return False
        except Exception as e:
            console.print(f"检查登录状态失败: {e}", style="yellow")
            return False

    @staticmethod
    def _xor_crypt(data: str, key: int = 5277) -> str:
        """XOR + base64 加密/解密"""
        import base64
        key_bytes = str(key).encode()
        encrypted = bytes(b ^ key_bytes[i % len(key_bytes)] for i, b in enumerate(data.encode()))
        return base64.b64encode(encrypted).decode()

    @staticmethod
    def _xor_decrypt(token: str, key: int = 5277) -> str:
        """XOR + base64 解密"""
        import base64
        key_bytes = str(key).encode()
        decoded = base64.b64decode(token)
        decrypted = bytes(b ^ key_bytes[i % len(key_bytes)] for i, b in enumerate(decoded))
        return decrypted.decode()

    def load_user_credentials(self) -> Optional[Dict]:
        """加载保存的用户凭证"""
        if os.path.exists(USER_CREDENTIALS_PATH):
            try:
                with open(USER_CREDENTIALS_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                # 解密密码
                if 'password' in data and data['password']:
                    try:
                        data['password'] = self._xor_decrypt(data['password'])
                    except:
                        pass  # 兼容旧的明文密码
                return data
            except Exception as e:
                console.print("加载凭证失败", style="yellow")
        return None

    def save_user_credentials(self, username: str, password: str):
        """保存用户凭证（密码加密存储）"""
        try:
            encrypted_pw = self._xor_crypt(password) if password else ""
            with open(USER_CREDENTIALS_PATH, 'w', encoding='utf-8') as f:
                json.dump({"username": username, "password": encrypted_pw}, f, ensure_ascii=False, indent=2)
            console.print("凭证已保存", style="green")
        except Exception as e:
            console.print("保存凭证失败", style="yellow")

    def load_progress(self) -> dict:
        """加载学习进度（已完成的专题班ID集合）"""
        try:
            if os.path.exists(PROGRESS_PATH):
                with open(PROGRESS_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                return data
        except:
            pass
        return {"completed_ws_ids": [], "last_page": 1, "last_idx": 0}

    def save_progress(self, completed_ws_ids: set, last_page: int = 1, last_idx: int = 0):
        """保存学习进度"""
        try:
            with open(PROGRESS_PATH, 'w', encoding='utf-8') as f:
                json.dump({
                    "completed_ws_ids": list(completed_ws_ids),
                    "last_page": last_page,
                    "last_idx": last_idx,
                    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            debug(f"保存进度失败: {e}")

    def mark_workshop_completed(self, ws_id: str):
        """标记单个专题班完成，立即落盘"""
        progress = self.load_progress()
        completed = set(progress.get("completed_ws_ids", []))
        completed.add(ws_id)
        self.save_progress(completed,
                           progress.get("last_page", 1),
                           progress.get("last_idx", 0))

    async def login(self, page=None, username="", password="", auto_login=True, log_callback=None):
        """登录。GUI模式传入username/password/auto_login/log_callback。"""
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        if page is None:
            page = self.pages[0]

        # GUI模式：直接用传入的凭证登录
        if username:
            await self._do_login(page, username, password, auto_login, _log)
            return

        console.print("建行学习自动登录", style="bold blue")
        if await self.check_login_status(page):
            # 显示当前用户并询问是否切换
            try:
                _uname = ""
                try:
                    _uname = await page.locator(".ccb-user-box").inner_text(timeout=3000)
                except:
                    pass
                if not _uname:
                    try:
                        _uname = await page.evaluate("() => localStorage.getItem('userName') || ''")
                    except:
                        pass
                _uname = (_uname or "").strip()
                if _uname:
                    console.print(f"当前用户: {_uname}", style="green")
                    _switch = await async_input("是否切换用户？(y/n)", default="n", timeout=5)
                    if _switch in ('y', 'yes'):
                        try:
                            if os.path.exists(STORAGE_STATE_PATH):
                                os.remove(STORAGE_STATE_PATH)
                            await page.context.clear_cookies()
                            console.print("已清除会话，准备重新登录", style="yellow")
                        except:
                            pass
                    else:
                        console.print("✓ 继续使用当前会话", style="bold green")
                        return
                else:
                    console.print("✓ 检测到已登录状态，无需重新登录!", style="bold green")
                    return
            except:
                console.print("✓ 检测到已登录状态，无需重新登录!", style="bold green")
                return
        
        console.print("未检测到登录状态，需要登录", style="yellow")
        
        # 尝试加载已保存的凭证
        saved_credentials = self.load_user_credentials()
        use_saved = False
        
        if saved_credentials and 'username' in saved_credentials:
            choice = await async_input(f"发现已保存账号: {saved_credentials['username']}，是否使用？(y/n，默认y)", default="y", timeout=5)
            if choice != 'n' and choice != 'no':
                use_saved = True
        
        if use_saved and saved_credentials:
            username = saved_credentials['username']
            password = saved_credentials.get('password', '')
            if not password:
                password = await async_input("请输入密码", default="", timeout=300, block=True, raw=True, password=True)
        else:
            # 询问用户是自动登录还是手动登录
            choice = await async_input("是否使用自动登录？(y/n，默认y)", default="y", timeout=5)
            
            if choice == 'n' or choice == 'no':
                # 手动登录模式
                console.print("请在打开的浏览器中完成登录...", style="bold blue")
                await page.goto("https://u.ccb.com/portal/#/study")

                console.print("等待登录完成...", style="yellow")
                console.print("提示：登录成功后按回车键继续", style="green")

                await async_input("登录成功后按回车键继续", default="", timeout=600, block=True)

                console.print("✓ 登录成功!", style="bold green")
                return
            else:
                # 自动登录模式
                console.print()
                username = await async_input("请输入建行统一认证账号", default="", timeout=300, block=True, raw=True)
                # 密码也用async_input，避免和stdin线程冲突
                password = await async_input("请输入密码", default="", timeout=300, block=True, raw=True, password=True)
                
                if not username or not password:
                    console.print("用户名或密码不能为空，将使用手动登录模式", style="red")
                    await self.login()
                    return
        
        # 使用自动登录流程
        console.print("正在导航到登录页面...", style="blue")
        await page.goto("https://u.ccb.com/sys/#/login")
        await asyncio.sleep(3)
        
        try:
            # 输入用户名（先移除maxlength限制，再通过原生DOM API赋值）
            console.print("正在输入用户名...", style="blue")
            await page.evaluate(f"""() => {{
                const el = document.querySelector('input[placeholder*="账号"]');
                if (el) {{
                    el.removeAttribute('maxlength');
                    el.removeAttribute('maxLength');
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    setter.call(el, '{username}');
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
            }}""")
            # 验证输入是否正确
            actual_uname = await page.evaluate("""() => {
                const el = document.querySelector('input[placeholder*="账号"]');
                return el ? el.value : '';
            }""")
            console.print(f"  实际填入: [{actual_uname}]", style="blue")
            if len(actual_uname) < len(username):
                console.print("输入不完整，尝试逐字符键盘输入...", style="yellow")
                await page.keyboard.press(_SELECT_ALL)
                await page.keyboard.press("Backspace")
                await page.wait_for_timeout(300)
                await page.keyboard.type(username, delay=150)
                actual_uname = await page.evaluate("""() => {
                    const el = document.querySelector('input[placeholder*="账号"]');
                    return el ? el.value : '';
                }""")
                console.print(f"  键盘输入后: [{actual_uname}]", style="blue")
            await asyncio.sleep(0.5)
            
            # 输入密码
            console.print("正在输入密码...", style="blue")
            await page.evaluate(f"""() => {{
                const el = document.getElementById('inputPwd');
                if (el) {{
                    el.removeAttribute('maxlength');
                    el.removeAttribute('maxLength');
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    setter.call(el, '{password}');
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                }}
            }}""")
            await asyncio.sleep(0.5)
            
            # 点击登录按钮
            console.print("正在点击登录按钮...", style="blue")
            login_button = page.get_by_role("button", name="登录")
            
            # 等待登录按钮可点击
            try:
                await login_button.wait_for(state="enabled", timeout=10000)
            except:
                pass
            
            await login_button.click()
            
            # 等待登录成功后保存凭证
            if not use_saved:
                save_choice = await async_input("是否保存账号密码以便下次使用？(y/n，默认y)", default="y", timeout=5)
                if save_choice != 'n' and save_choice != 'no':
                    self.save_user_credentials(username, password)
            
            # 自动检测登录是否完成
            console.print("正在等待登录完成...", style="yellow")
            
            logged_in = False
            login_failed = False
            for i in range(60):  # 最多等待60秒
                await asyncio.sleep(1)
                try:
                    current_url = page.url
                    
                    # 如果还停留在登录页
                    if "/sys/#/login" in current_url:
                        # 检查是否有错误提示
                        try:
                            err = page.locator(".el-message--error, .el-form-item__error, [class*=error]")
                            err_text = await err.first.inner_text(timeout=1500)
                            if err_text:
                                console.print(f"[red]登录失败: {err_text.strip()}[/red]")
                                login_failed = True
                                break
                        except:
                            pass
                        if i >= 30:
                            console.print("[yellow]登录请求似乎未成功，尝试检查页面状态...[/yellow]")
                            login_failed = True
                            break
                        continue
                    
                    # URL不再是登录页 → 登录成功！导航到study页确认
                    console.print(f"检测到页面跳转: {current_url}", style="green")
                    console.print("正在导航到学习页面确认登录状态...", style="blue")
                    
                    # 导航到study页面做最终确认
                    await page.goto("https://u.ccb.com/portal/#/study",
                                    wait_until="domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(5000)
                    
                    # 用和check_login_status相同的逻辑做最终验证
                    final_url = page.url
                    if "/sys/#/login" in final_url:
                        console.print("被重定向回登录页，登录未成功", style="yellow")
                        await asyncio.sleep(3)
                        continue
                    
                    # 检查是否还有未登录标志
                    nt = await page.locator(".cuWeb-swipe-web-info-notlogin-tips").count()
                    nb = await page.locator(".cuWeb-swipe-web-info-notlogin-btn").count()
                    if nt == 0 and nb == 0:
                        try:
                            ubt = await page.locator(".ccb-user-box").inner_text(timeout=2000)
                            if "登录" not in ubt or len(ubt.strip()) >= 10:
                                logged_in = True
                                console.print("✓ 检测到登录成功!", style="bold green")
                                break
                        except:
                            pass
                        # 兜底：页面文字
                        pt = await page.locator("body").inner_text(timeout=3000)
                        if "立即登录" not in pt and ("学时" in pt or "课程" in pt):
                            logged_in = True
                            console.print("✓ 检测到登录成功!", style="bold green")
                            break
                    
                    # 如果到了这里还没确认，继续等待
                    console.print("尚未确认登录状态，继续等待...", style="yellow")
                except:
                    pass
            
            # 处理登录失败：重试
            if login_failed and not logged_in:
                console.print("[yellow]登录失败！[/yellow]")
                console.print("可能原因：账号/密码错误、网络问题或验证码", style="yellow")
                retry = await async_input("是否重新输入账号密码重试？(y/n，默认y)", default="y", timeout=5)
                if retry != 'n':
                    for attempt in range(3):
                        console.print(f"[bold blue]第 {attempt+1} 次重试[/bold blue]")
                        await page.goto("https://u.ccb.com/sys/#/login")
                        await asyncio.sleep(2)
                        
                        console.print("正在输入用户名...", style="blue")
                        await page.evaluate(f"""() => {{
                            const el = document.querySelector('input[placeholder*="账号"]');
                            if (el) {{
                                el.removeAttribute('maxlength');
                                el.removeAttribute('maxLength');
                                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                                setter.call(el, '{username}');
                                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            }}
                        }}""")
                        actual_uname = await page.evaluate("""() => {
                            const el = document.querySelector('input[placeholder*="账号"]');
                            return el ? el.value : '';
                        }""")
                        if len(actual_uname) < len(username):
                            console.print("输入不完整，用键盘补充...", style="yellow")
                            await page.keyboard.press(_SELECT_ALL)
                            await page.keyboard.press("Backspace")
                            await page.wait_for_timeout(300)
                            await page.keyboard.type(username, delay=150)
                        
                        await asyncio.sleep(0.5)
                        console.print("正在输入密码...", style="blue")
                        await page.evaluate(f"""() => {{
                            const el = document.getElementById('inputPwd');
                            if (el) {{
                                el.removeAttribute('maxlength');
                                el.removeAttribute('maxLength');
                                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                                setter.call(el, '{password}');
                                el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            }}
                        }}""")
                        
                        console.print("正在点击登录按钮...", style="blue")
                        lb = page.get_by_role("button", name="登录")
                        await lb.click()
                        
                        for j in range(30):
                            await asyncio.sleep(1)
                            cu = page.url
                            if "/sys/#/login" not in cu:
                                await page.wait_for_timeout(3000)
                                nt = await page.locator(".cuWeb-swipe-web-info-notlogin-tips").count()
                                if nt == 0:
                                    logged_in = True
                                    console.print("✓ 重试登录成功!", style="bold green")
                                    break
                                break
                            # 检查错误
                            try:
                                e = page.locator(".el-message--error, .el-form-item__error")
                                t = await e.first.inner_text(timeout=1500)
                                if t:
                                    console.print(f"[red]重试失败: {t.strip()}[/red]")
                                    break
                            except:
                                pass
                        if logged_in:
                            break
                    else:
                        console.print("[yellow]多次重试失败，将使用手动登录模式[/yellow]")
            
            if not logged_in:
                await async_input("请手动在浏览器中完成登录，然后按回车键继续", default="", timeout=600, block=True)
            
            console.print("✓ 登录流程完成!", style="bold green")

        except Exception as e:
            console.print("自动登录失败", style="red")
            console.print("将使用手动登录模式", style="yellow")
            await async_input("请在浏览器中完成登录后按回车键继续", default="", timeout=600, block=True)

    async def _do_login(self, page, username, password, auto_login, _log):
        """GUI模式登录：直接用传入的凭证提交表单。失败自动重试，页面未加载会刷新。"""
        if not auto_login:
            _log("请在浏览器中完成登录...", "blue")
            await page.goto("https://u.ccb.com/portal/#/study")
            _log("等待登录完成...", "yellow")
            # 等待URL不再是登录页
            for _ in range(120):
                await asyncio.sleep(1)
                if "/sys/#/login" not in page.url:
                    break
            _log("登录成功", "green")
            return

        login_url = "https://u.ccb.com/sys/#/login"
        max_retries = 5
        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    _log(f"正在重试登录({attempt}/{max_retries})...", "yellow")
                    await asyncio.sleep(3)

                # ── 1. 导航到登录页，确保页面加载 ──
                _log("正在导航到登录页面...", "blue")
                page_loaded = False
                for refresh in range(1, 4):  # 最多刷新3次
                    try:
                        await page.goto(login_url, timeout=15000)
                    except:
                        pass
                    await asyncio.sleep(3)

                    # 检查登录表单是否出现
                    has_form = await page.evaluate("""() => {
                        return !!(
                            document.querySelector('input[placeholder*="账号"]') ||
                            document.querySelector('input[placeholder*="用户"]') ||
                            document.querySelector('#inputPwd') ||
                            document.querySelector('input[type="password"]')
                        );
                    }""")
                    if has_form:
                        page_loaded = True
                        break
                    _log(f"登录页未加载完，刷新({refresh}/3)...", "yellow")

                if not page_loaded:
                    _log(f"登录页加载失败(尝试 {attempt}/{max_retries})", "red")
                    continue  # 下一次重试

                # ── 2. 输入用户名 ──
                _log("正在输入用户名...", "blue")
                username_filled = await page.evaluate(f"""() => {{
                    const el = document.querySelector('input[placeholder*="账号"]')
                            || document.querySelector('input[placeholder*="用户"]');
                    if (el) {{
                        el.removeAttribute('maxlength');
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        setter.call(el, '{username}');
                        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        return true;
                    }}
                    return false;
                }}""")
                if not username_filled:
                    _log(f"用户名输入框未找到(尝试 {attempt}/{max_retries})", "red")
                    continue
                await asyncio.sleep(0.5)

                # ── 3. 输入密码 ──
                _log("正在输入密码...", "blue")
                password_filled = await page.evaluate(f"""() => {{
                    const el = document.getElementById('inputPwd')
                            || document.querySelector('input[type="password"]');
                    if (el) {{
                        el.removeAttribute('maxlength');
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value').set;
                        setter.call(el, '{password}');
                        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        return true;
                    }}
                    return false;
                }}""")
                if not password_filled:
                    _log(f"密码输入框未找到(尝试 {attempt}/{max_retries})", "red")
                    continue
                await asyncio.sleep(0.5)

                # ── 4. 点击登录 ──
                _log("正在点击登录按钮...", "blue")
                login_button = page.get_by_role("button", name="登录")
                try:
                    await login_button.wait_for(state="visible", timeout=10000)
                except:
                    pass
                try:
                    await login_button.click(timeout=10000)
                except Exception:
                    try:
                        await login_button.click(force=True, timeout=10000)
                    except Exception:
                        _log(f"登录按钮点击失败(尝试 {attempt}/{max_retries})", "red")
                        continue

                # ── 5. 等待登录完成 ──
                _log("正在等待登录完成...", "yellow")
                logged_in = False
                for i in range(60):
                    await asyncio.sleep(1)
                    if "/sys/#/login" not in page.url:
                        try:
                            await page.goto("https://u.ccb.com/portal/#/study",
                                            wait_until="networkidle", timeout=15000)
                        except:
                            pass
                        await page.wait_for_timeout(3000)
                        if "/sys/#/login" not in page.url:
                            logged_in = True
                            break

                if logged_in:
                    _log("登录成功", "green")
                    return

                _log(f"登录超时(尝试 {attempt}/{max_retries})", "red")

            except Exception as e:
                _log(f"自动登录失败(尝试 {attempt}/{max_retries}): {e}", "red")

        raise Exception(f"登录失败，已重试 {max_retries} 次")

    async def get_workshops(self, page: Page) -> List[Dict]:
        """获取专题班列表 - 从.card结构提取所有专题班"""
        workshops = []
        try:
            console.print("正在获取专题班列表...", style="blue")
            
            # 等待专题班列表容器加载
            # await page.wait_for_selector(".workshop-content-list", timeout=10000)
            try:
                await page.wait_for_selector(".workshop-content-list", timeout=10000)
            except:
                pass
            
            # 方法1：从 workshop-content-list 中提取卡片
            # 注意：DOM 结构为 .workshop-content-list > ul > li.clearfix（中间有ul层）
            cards = await page.locator(".workshop-content-list li.clearfix").all()
            console.print(f"找到 {len(cards)} 个专题班卡片元素", style="green")
            
            for card in cards:
                try:
                    # 提取标题
                    title_el = card.locator(".workshop-list-content-title")
                    title_text = await title_el.inner_text(timeout=3000)
                    
                    # 提取课程数和学时
                    info_spans = await card.locator(".workshop-list-content span").all()
                    course_count = ""
                    study_hours = ""
                    for span in info_spans:
                        text = (await span.inner_text()).strip()
                        if "总课程" in text:
                            course_count = text
                        elif "学时" in text:
                            study_hours = text
                    
                    # 提取报名状态
                    enroll_status = ""
                    try:
                        status_el = card.locator(".border-ing, .border-end")
                        enroll_status = await status_el.inner_text(timeout=2000)
                    except:
                        pass

                    # 报名已结束，直接跳过
                    if "已结束" in enroll_status or "报名截止" in enroll_status:
                        continue

                    # 提取详情页链接
                    detail_link = ""
                    try:
                        link_el = card.locator("a").first
                        href = await link_el.get_attribute("href")
                        if href:
                            detail_link = href
                    except:
                        pass
                    
                    workshops.append({
                        "title": title_text.strip(),
                        "course_count": course_count,
                        "study_hours": study_hours,
                        "enroll_status": enroll_status.strip(),
                        "detail_link": detail_link,
                        "element": card
                    })
                except Exception as e2:
                    pass
            
            # 方法2：如果没有找到卡片，尝试从<a>标签提取（兜底）
            if not workshops:
                console.print("卡片提取未找到结果，改用链接匹配...", style="yellow")
                link_elements = await page.get_by_role("link").all()
                for link in link_elements:
                    try:
                        text = await link.inner_text()
                        if text and len(text.strip()) > 3:
                            text_clean = text.strip()[:100]
                            href = await link.get_attribute("href") or ""
                            workshops.append({
                                "title": text_clean,
                                "course_count": "",
                                "study_hours": "",
                                "enroll_status": "",
                                "detail_link": href,
                                "element": link
                            })
                    except:
                        pass
            
            console.print(f"共获取 {len(workshops)} 个专题班", style="green")
            
        except Exception as e:
            console.print("获取专题班列表失败", style="red")
            import traceback
            traceback.print_exc()
        
        return workshops

    async def go_to_next_page(self, page: Page) -> bool:
        """翻到下一页 - 检查按钮是否可用（含frame检测）"""
        try:
            console.print("正在查找下一页按钮...", style="blue")

            # 先等待分页元素出现
            try:
                await page.wait_for_selector("span.pagetext, .pager_manu, .pageinfo, .pagination", timeout=8000)
            except:
                debug("等待分页元素超时")

            # 收集所有可搜索的上下文（主页面 + 所有frame）
            search_contexts = [("main", page)]
            for frame in page.frames:
                if frame != page.main_frame:
                    search_contexts.append((f"frame:{frame.url[:60]}", frame))
            debug(f"搜索上下文: {len(search_contexts)} 个 ({', '.join(c[0] for c in search_contexts)})")

            # 分页按钮可能是中文"下一页"或英文"Next"
            NEXT_TEXTS = ["下一页", "Next"]

            # 方式1: 在分页区域找"下一页/Next"
            try:
                page_container = page.locator("div.pager_manu, .pageinfo, .pagination, [class*=page]:not(.pageheader):not(.homepage_layout)")
                container_count = await page_container.count()
                debug(f"分页容器: {container_count} 个")

                if container_count > 0:
                    for nxt in NEXT_TEXTS:
                        next_btn = page_container.first.locator(f"text={nxt}")
                        btn_count = await next_btn.count()
                        debug(f"方式1: 找到 {btn_count} 个'{nxt}'元素")
                        if btn_count > 0:
                            btn_class = await next_btn.first.get_attribute("class") or ""
                            debug(f"方式1: class=[{btn_class}]")
                            if "disable" not in btn_class:
                                await next_btn.first.click()
                                await page.wait_for_timeout(5000)
                                console.print("已翻到下一页", style="green")
                                return True
                            else:
                                console.print("下一页按钮不可用（disable），已到最后一页", style="yellow")
                                return False
            except Exception as e1:
                debug(f"方式1异常: {e1}")

            # 方式2: 查找span.pagetext的"下一页/Next"元素（在所有上下文中搜索）
            for ctx_name, ctx in search_contexts:
                for nxt in NEXT_TEXTS:
                    try:
                        next_spans = ctx.locator("span.pagetext").filter(has_text=nxt)
                        sc = await next_spans.count()
                        debug(f"方式2[{ctx_name}]: span.pagetext '{nxt}' 找到 {sc} 个")
                        if sc > 0:
                            cls = await next_spans.first.get_attribute("class") or ""
                            visible = await next_spans.first.is_visible()
                            debug(f"方式2[{ctx_name}]: class=[{cls}] visible={visible}")
                            if "disable" not in cls and visible:
                                await next_spans.first.click()
                                await page.wait_for_timeout(5000)
                                console.print("已翻到下一页", style="green")
                                return True
                    except Exception as e2:
                        debug(f"方式2[{ctx_name}]异常: {e2}")

            # 方式3: 直接查找可点击的"下一页/Next"元素（在所有上下文中搜索）
            for ctx_name, ctx in search_contexts:
                for nxt in NEXT_TEXTS:
                    try:
                        next_els = ctx.locator("a, button, span, li").filter(has_text=nxt)
                        count = await next_els.count()
                        debug(f"方式3[{ctx_name}]: 找到 {count} 个含'{nxt}'的元素")
                        for i in range(count):
                            el = next_els.nth(i)
                            cls_str = (await el.get_attribute("class")) or ""
                            is_disabled = await el.get_attribute("disabled")
                            has_disable = "disable" in cls_str
                            is_visible = await el.is_visible()
                            tag = await el.evaluate("el => el.tagName")
                            debug(f"  [{i}] tag={tag} class=[{cls_str}] visible={is_visible} disabled={is_disabled}")
                            if is_visible and not is_disabled and not has_disable:
                                await el.click()
                                await page.wait_for_timeout(5000)
                                console.print("已翻到下一页", style="green")
                                return True
                    except Exception as e3:
                        debug(f"方式3[{ctx_name}]异常: {e3}")

            # 方式4: 兜底 - dump页面中所有含"下一页/Next"的元素
            for nxt in NEXT_TEXTS:
                try:
                    page_els = page.locator("*").filter(has_text=nxt)
                    pc = await page_els.count()
                    debug(f"方式4(兜底): 页面中共 {pc} 个含'{nxt}'的元素")
                    for i in range(min(pc, 10)):
                        el = page_els.nth(i)
                        tag = await el.evaluate("el => el.tagName")
                        cls_str = (await el.get_attribute("class")) or ""
                        txt = (await el.inner_text())[:50]
                        debug(f"  [{i}] <{tag}> class=[{cls_str}] text=[{txt}]")
                except Exception as e4:
                    debug(f"方式4异常: {e4}")

            # 方式5: dump所有frame中的分页区域innerHTML
            for ctx_name, ctx in search_contexts:
                try:
                    pager_html = await ctx.evaluate("""() => {
                        const selectors = ['.pager_manu', '.pageinfo', '.pagination',
                            '[class*=pager]', '[class*=paging]', '[class*=page_num]'];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el) return sel + ': ' + el.innerHTML.substring(0, 500);
                        }
                        // 兜底: 找含"下一页"或"Next"的元素
                        const all = document.querySelectorAll('*');
                        for (const el of all) {
                            if (el.innerText && (el.innerText.includes('下一页') || el.innerText.includes('Next')) && el.children.length < 5)
                                return 'found: <' + el.tagName + ' class="' + el.className + '">' + el.outerHTML.substring(0, 300);
                        }
                        return 'none';
                    }""")
                    debug(f"方式5[{ctx_name}]: {pager_html}")
                except Exception as e5:
                    debug(f"方式5[{ctx_name}]异常: {e5}")

            console.print("未找到可用的下一页按钮，已到最后一页", style="yellow")
            return False
        except Exception as e:
            console.print(f"翻页失败: {e}", style="yellow")
            return False

    async def display_workshops(self, workshops: List[Dict]):
        table = Table(title="专题班列表")
        table.add_column("序号", style="cyan")
        table.add_column("专题班名称", style="magenta")

        for i, workshop in enumerate(workshops, 1):
            table.add_row(str(i), workshop["title"][:60])

        console.print(table)

    async def filter_by_tags(self, page: Page) -> bool:
        """根据标签筛选专题班，返回是否成功"""
        if not self.tags_to_learn:
            return True

        console.print(f"正在筛选标签: {', '.join(self.tags_to_learn)}", style="blue")
        all_found = True

        try:
            # 等待标签树加载
            await page.wait_for_timeout(5000)
            try:
                await page.wait_for_selector("ul.tag-tree-list", timeout=15000)
            except:
                debug("标签树未加载，继续尝试...")

            for tag in self.tags_to_learn:
                console.print(f"查找标签: {tag}", style="blue")

                found = False

                # 方法1：在 tag-tree-list 中查找 span.single-tag 匹配文本
                for attempt in range(3):
                    try:
                        all_tags = page.locator("ul.tag-tree-list span.single-tag")
                        cnt = await all_tags.count()
                        debug(f"tag-tree-list: {cnt} spans, URL: {page.url}")
                        if cnt == 0:
                            # 打印页面结构帮助排查
                            try:
                                body = await page.locator("body").inner_text(timeout=3000)
                                debug(f"页面内容前200字: {body[:200]}")
                            except:
                                pass
                        for i in range(cnt):
                            text = (await all_tags.nth(i).inner_text()).strip()
                            if text == tag:
                                console.print(f"  找到匹配标签: {text}", style="green")
                                await all_tags.nth(i).click()
                                await page.wait_for_timeout(3000)
                                console.print(f"  ✓ 已点击标签: {tag}", style="green")
                                found = True
                                break
                        if found:
                            break
                    except Exception as e1:
                        console.print(f"  方法1尝试 {attempt+1} 失败: {e1}", style="yellow")
                    await page.wait_for_timeout(2000)

                if found:
                    continue

                # 方法2：在全页面范围找匹配文本的可见clickable元素
                for attempt in range(3):
                    try:
                        console.print(f"  方法2: 页面搜索标签...", style="blue")
                        candidates = page.locator("span, div, li, a").filter(has_text=tag)
                        cc = await candidates.count()
                        console.print(f"  找到 {cc} 个候选元素", style="blue")
                        for j in range(min(cc, 20)):
                            try:
                                t = (await candidates.nth(j).inner_text()).strip()
                                if t == tag and await candidates.nth(j).is_visible():
                                    console.print(f"  找到可见标签元素: {tag}", style="green")
                                    await candidates.nth(j).click()
                                    await page.wait_for_timeout(3000)
                                    console.print(f"  ✓ 已点击标签: {tag}", style="green")
                                    found = True
                                    break
                            except:
                                pass
                        if found:
                            break
                    except:
                        pass
                    await page.wait_for_timeout(2000)

                if not found:
                    console.print(f"  ✗ 未找到标签: {tag}", style="red")
                    all_found = False

            if all_found:
                console.print("标签筛选完成", style="green")
            else:
                console.print("部分标签未找到，筛选可能不完整", style="yellow")
        except Exception as e:
            console.print(f"标签筛选失败: {e}", style="red")
            all_found = False

        return all_found

    async def enroll_workshop(self, page: Page, workshop_title: str):
        """报名专题班 - 进入专题班详情页，点击报名/学习"""
        try:
            console.print(f"正在查找并点击专题班: {workshop_title}", style="blue")
            
            # 记录点击前的页面集合，用于检测新标签页
            existing_page_ids = set(id(p) for p in self.context.pages)

            # 查找并点击专题班链接
            workshop_link = None
            # 方法1: 通过卡片中的详细链接点击
            try:
                card = page.locator(f"text={workshop_title}").first
                if await card.count() > 0:
                    workshop_link = card
            except:
                pass
            # 方法2: 遍历所有链接
            if not workshop_link or await workshop_link.count() == 0:
                all_links = await page.get_by_role("link").all()
                for link in all_links:
                    try:
                        text = await link.inner_text()
                        if workshop_title in text:
                            workshop_link = link
                            break
                    except:
                        pass

            if not workshop_link or (hasattr(workshop_link, 'count') and await workshop_link.count() == 0):
                console.print("未找到专题班链接", style="red")
                return False, page

            console.print("找到专题班链接，准备点击", style="green")

            # 点击链接（SPA 页面，不需要 expect_navigation）
            await workshop_link.click()
            await page.wait_for_timeout(3000)

            debug(f"当前页面URL: {page.url}")

            # 检查是否打开了新标签页（用集合差集，避免并发时抓错标签页）
            working_page = page
            for p in self.context.pages:
                if id(p) not in existing_page_ids:
                    working_page = p
                    await working_page.bring_to_front()
                    await working_page.wait_for_timeout(3000)
                    debug(f"新标签页URL: {working_page.url[:80]}")
                    break
            
            # 获取页面内容
            page_text = await working_page.locator("body").inner_text(timeout=5000)
            debug(f"页面标题预览: {page_text[:100]}")
            
            # 检查登录态
            if "密码登录" in page_text[:500]:
                console.print("页面显示登录表单，需要重新登录", style="red")
                return False, working_page
            
            # 判断是否已报名（看URL是否已跳转到myworkshop）
            current_url = working_page.url
            already_enrolled = "/myworkshop/" in current_url
            
            if already_enrolled:
                console.print("该专题班已报名", style="green")
                return True, working_page
            
            # 检查页面上是否有立即报名按钮
            async def has_enroll_btn():
                for kw in ["立即报名", "加入学习", "免费报名", "开始学习"]:
                    try:
                        btn = working_page.locator(f"text={kw}").first
                        if await btn.count() > 0:
                            try:
                                if await btn.is_visible():
                                    return kw, btn
                            except:
                                return kw, btn
                    except:
                        pass
                return None, None
            
            kw, btn_el = await has_enroll_btn()
            if not btn_el:
                console.print("未找到报名按钮，可能已报名或在已报名页", style="yellow")
                return True, working_page
            
            console.print(f"找到「{kw}」按钮，准备点击", style="blue")
            
            # 点击并验证
            enrolled = False
            for attempt in range(3):
                # 方法1：直接点击找到的元素
                try:
                    await btn_el.click()
                    await working_page.wait_for_timeout(3000)
                except:
                    pass
                
                # 验证：看URL是否跳转、按钮是否消失
                new_url = working_page.url
                kw2, _ = await has_enroll_btn()
                if "/myworkshop/" in new_url or not kw2:
                    enrolled = True
                    console.print(f"✓ 报名成功！URL: {new_url[:80]}", style="bold green")
                    break
                
                # 方法2：尝试点击元素的父级
                try:
                    parent = btn_el.locator("..")
                    if await parent.count() > 0:
                        await parent.first.click()
                        await working_page.wait_for_timeout(3000)
                        new_url = working_page.url
                        kw2, _ = await has_enroll_btn()
                        if "/myworkshop/" in new_url or not kw2:
                            enrolled = True
                            console.print("✓ 报名成功（通过父元素点击）", style="bold green")
                            break
                except:
                    pass
                
                # 方法3：用JS触发点击
                try:
                    await working_page.evaluate(f"""() => {{
                        const els = document.querySelectorAll('*');
                        for (const el of els) {{
                            if (el.innerText.includes('{kw}') && el.offsetParent !== null) {{
                                el.click();
                                el.dispatchEvent(new Event('click', {{ bubbles: true }}));
                                // 也点一下父级
                                if (el.parentElement) el.parentElement.click();
                                break;
                            }}
                        }}
                    }}""")
                    await working_page.wait_for_timeout(3000)
                    new_url = working_page.url
                    kw2, _ = await has_enroll_btn()
                    if "/myworkshop/" in new_url or not kw2:
                        enrolled = True
                        console.print("✓ 报名成功（通过JS点击）", style="bold green")
                        break
                except:
                    pass
                
                if attempt < 2:
                    console.print(f"尝试 {attempt+1} 未生效，重试...", style="yellow")
                    await working_page.wait_for_timeout(2000)
            
            if enrolled:
                # 等页面加载课程内容
                await working_page.wait_for_timeout(3000)
                return True, working_page
            else:
                console.print("报名按钮点击后未检测到变化，手动确认", style="yellow")
                await async_input("报名后按回车键继续（或等待5秒自动继续）", default="", timeout=5)
                return True, working_page
            
        except Exception as e:
            console.print(f"进入专题班失败: {e}", style="red")
            import traceback
            traceback.print_exc()
            return False, page

    async def find_and_learn_courses(self, page: Page, worker_id: int):
        """学习课程 - 获取课程列表后启动并行学习（失败自动重试5次）"""
        try:
            console.print(f"正在获取课程列表...", style="blue")
            
            # 获取专题班ID
            workshop_id = ""
            url = page.url
            import re as _re
            m = _re.search(r'id=([a-f0-9\-]+)', url)
            if m:
                workshop_id = m.group(1)
            if not workshop_id:
                console.print("无法获取专题班ID", style="red")
                return False
            
            # 多次尝试获取课程列表（表格可能是异步加载的）
            courses = []
            for attempt in range(10):
                if attempt > 0:
                    console.print(f"第 {attempt+1} 次尝试获取课程列表...", style="yellow")
                    await page.reload(wait_until="networkidle")
                    await page.wait_for_timeout(5000)
                
                courses = await self.get_courses_from_workshop(page)
                if courses:
                    break
            
            await self.display_course_table(courses)
            
            if not courses:
                console.print("重试5次后仍未获取到课程", style="red")
                return False
            
            # 启动并行学习
            await self.parallel_learn_courses(workshop_id, courses)
            return True
            
        except Exception as e:
            console.print(f"学习课程失败: {e}", style="red")
            import traceback
            traceback.print_exc()
            return False


    async def _set_lowest_quality(self, page: Page):
        """静音 + 最低画质 + 2倍速度（JS直接操作 + UI点击双重保障）"""

        # 1) 静音（JS直接设置，最可靠）
        try:
            await page.evaluate("() => { const v = document.querySelector('video'); if (v) v.muted = true; }")
        except:
            pass

        # 2) 画质：先尝试JS，失败再UI点击
        for attempt in range(2):
            try:
                if attempt == 0:
                    # JS方式：遍历画质选项找最低的并点击
                    result = await page.evaluate("""() => {
                        const btn = document.querySelector('.current-quality');
                        if (!btn) return 'no-btn';
                        btn.click();
                        return 'clicked';
                    }""")
                    if result == 'no-btn':
                        break
                    await page.wait_for_timeout(1000)
                    # 点击最后一个选项（最低画质）
                    clicked = await page.evaluate("""() => {
                        const items = document.querySelectorAll('.quality-list li');
                        if (items.length > 1) {
                            items[items.length - 1].click();
                            return items[items.length - 1].innerText.trim();
                        }
                        return null;
                    }""")
                    if clicked:
                        debug(f"画质(JS): 选 {clicked}")
                        await page.wait_for_timeout(1500)
                        break
                else:
                    # UI方式：hover展开控制栏再点击
                    try:
                        await page.locator(".prism-player, video, #player_area").first.hover()
                        await page.wait_for_timeout(500)
                    except:
                        pass
                    qbtn = page.locator('.current-quality').first
                    if await qbtn.count() > 0:
                        await qbtn.click(force=True)
                        await page.wait_for_timeout(1000)
                        # 等待下拉菜单可见
                        try:
                            await page.locator('.quality-list li').last.wait_for(state="visible", timeout=3000)
                        except:
                            pass
                        items = page.locator('.quality-list li')
                        cnt = await items.count()
                        if cnt > 1:
                            lowest = items.nth(cnt - 1)
                            text = await lowest.inner_text()
                            debug(f"画质(UI): {cnt}个, 选 {text.strip()}")
                            await lowest.click(force=True)
                            await page.wait_for_timeout(1500)
            except Exception as _qe:
                debug(f"画质异常(attempt={attempt}): {_qe}")

        # 3) 倍速：必须用UI点击（服务器需要收到事件才能正确计算进度）
        for attempt in range(3):
            try:
                # hover展开控制栏
                try:
                    await page.locator(".prism-player, video, #player_area").first.hover()
                    await page.wait_for_timeout(500)
                except:
                    pass
                rate_btn = page.locator('.current-rate').first
                if await rate_btn.count() == 0:
                    debug("倍速: 未找到速率按钮")
                    break
                cur = (await rate_btn.inner_text()).strip()
                if cur.startswith('2'):
                    debug("倍速: 已经是2x")
                    break
                # 点击展开下拉
                await rate_btn.click(force=True)
                await page.wait_for_timeout(800)
                # 等待选项可见
                opt = page.locator('li[data-rate="2.0"]').first
                try:
                    await opt.wait_for(state="visible", timeout=3000)
                except:
                    pass
                if await opt.count() > 0:
                    await opt.click(force=True)
                    debug(f"倍速: 已设为2x (attempt={attempt})")
                    await page.wait_for_timeout(500)
                    break
            except Exception as _se:
                debug(f"倍速异常(attempt={attempt}): {_se}")
                await page.wait_for_timeout(1000)


    async def _check_video_progress(self, page: Page) -> float:
        # 检查当前课程的播放进度（服务器认证的进度）
        try:
            pct = await page.evaluate('''() => {
                const el = document.querySelector('.el-progress__text');
                if (el) {
                    const t = el.innerText.trim().replace('%', '');
                    const n = parseFloat(t);
                    if (!isNaN(n)) return n;
                }
                return -1;
            }''')
            if isinstance(pct, (int, float)) and pct >= 0:
                return float(pct)
        except:
            pass
        return -1

    async def find_and_play_video(self, page: Page, worker_id: int, progress_callback=None, course_type=""):
        # 查找并播放视频，监控进度到100%
        try:
            debug(f"[工作线程 {worker_id+1}] 正在查找视频元素...")

            # 等待视频元素出现，找不到就刷新重试
            video_found = False
            video_selectors = ["video", "audio", "[class*='video']", "[class*='audio']", ".prism-player"]
            for refresh_attempt in range(10):
                for sel in video_selectors:
                    try:
                        v = await page.query_selector(sel)
                        if v:
                            debug(f"[工作线程 {worker_id+1}] 找到视频元素: {sel}")
                            video_found = True
                            break
                    except:
                        pass
                if video_found:
                    break
                if refresh_attempt < 9:
                    debug(f"[工作线程 {worker_id+1}] 视频未加载，刷新重试({refresh_attempt+1}/4)")
                    try:
                        await page.reload(wait_until="domcontentloaded", timeout=15000)
                        await page.wait_for_timeout(5000)
                    except:
                        pass

            if not video_found:
                ctype = (course_type or "").lower()
                if any(k in ctype for k in ["图书", "book", "document", "doc", "pdf", "图文"]):
                    debug(f"[工作线程 {worker_id+1}] 图书类课程，视为完成")
                    return True
                debug(f"[工作线程 {worker_id+1}] 未找到视频")
                return False

            # 确保视频开始播放（JS控制）
            await self._ensure_video_playing(page)

            await self._set_lowest_quality(page)

            for check in range(120):
                await asyncio.sleep(10)
                # 每次检查进度时，确保视频还在播放
                await self._ensure_video_playing(page)
                progress = await self._check_video_progress(page)
                if isinstance(progress, (int, float)) and progress >= 0:
                    if progress_callback:
                        progress_callback(progress)
                    if progress >= 100:
                        return True

            return True
        except Exception as e:
            debug(f"[工作线程 {worker_id+1}] 视频播放异常: {e}")
            return False

    async def _ensure_video_playing(self, page: Page):
        """用JS检查视频播放状态，暂停则恢复播放"""
        try:
            result = await page.evaluate("""() => {
                const videos = document.querySelectorAll('video');
                if (videos.length === 0) return {status: 'no_video'};
                const v = videos[0];
                if (v.paused || v.ended) {
                    try { v.play(); } catch(e) {}
                    // 取消静音（有些浏览器autoplay需要unmute）
                    try { v.muted = false; } catch(e) {}
                    return {status: 'resumed', paused: v.paused, currentTime: v.currentTime, duration: v.duration};
                }
                return {status: 'playing', currentTime: v.currentTime, duration: v.duration};
            }""")
            if result.get('status') == 'resumed':
                debug(f"  视频已恢复播放, currentTime={result.get('currentTime', 0):.1f}")
        except:
            pass


    _api_lock = None

    async def _get_courses_by_api(self, page: Page, ws_id: str, log_callback=None) -> list:
        """通过专题班详情API直接获取课程列表（429自动重试）"""
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        if not self._api_lock:
            self._api_lock = asyncio.Lock()

        api_url = f"https://api.u.ccb.com/v1/workshop/users/workshops/v2/{ws_id}"

        # fetch代码（从页面上下文取token后调API）
        fetch_code = """(url) => {
            let token = '';
            try { token = localStorage.getItem('token') || ''; } catch(e) {}
            if (!token) try { token = sessionStorage.getItem('token') || ''; } catch(e) {}
            if (!token) try { token = window.__token__ || ''; } catch(e) {}
            if (!token) try {
                const ax = window.axios;
                if (ax && ax.defaults && ax.defaults.headers)
                    token = ax.defaults.headers.common.token || ax.defaults.headers.token || '';
            } catch(e) {}
            if (!token) {
                const m = document.cookie.match(/token=([^;]+)/);
                if (m) token = m[1];
            }
            return fetch(url, {
                headers: {
                    'accept': 'application/json, text/plain, */*',
                    'source': '501',
                    'token': token,
                    'CParam1': 'L3dvcmtzaG9wLyMvbXl3b3Jrc2hvcC9kZXRhaWw=',
                    'CParam2': 'L3dvcmtzaG9wLyMvbXl3b3Jrc2hvcA=='
                },
                method: 'GET',
                credentials: 'include'
            }).then(async r => {
                if (!r.ok) {
                    let body = '';
                    try { body = await r.text(); } catch(e) {}
                    return {error: 'HTTP ' + r.status, body: body.slice(0, 200)};
                }
                return r.json();
            }).catch(e => ({error: e.message}));
        }"""

        try:
            async with self._api_lock:
                result = await page.evaluate(fetch_code, api_url)

                # 429 → 递增等待重试（5s, 10s, 15s）
                for retry_wait in [5, 10, 15]:
                    if not (isinstance(result, dict) and result.get("error") == "HTTP 429"):
                        break
                    _log(f"  429限流，等{retry_wait}秒重试...", "yellow")
                    await asyncio.sleep(retry_wait)
                    result = await page.evaluate(fetch_code, api_url)

                await asyncio.sleep(0.5)

            if isinstance(result, dict) and result.get("error"):
                err_msg = result['error']
                body = result.get('body', '')
                if 'HTTP 400' in err_msg and body:
                    _log(f"  API失败: {err_msg} ({body[:80]})", "red")
                else:
                    _log(f"  API失败: {err_msg}", "red")
                # 400是客户端错误，重试无意义，直接返回
                if 'HTTP 4' in err_msg:
                    return None
            if not isinstance(result, dict):
                _log(f"  API返回异常: {type(result).__name__}", "red")
                return None
            return result
        except Exception as e:
            _log(f"  API异常: {e}", "red")
            return None

    async def get_courses_from_workshop(self, page: Page, ws_title: str = "") -> List[Dict]:
        # 从表格提取全部课程信息（不含URL，URL由collector动态采集）
        courses = []
        try:
            debug("正在获取课程列表...")
            # 等待表格tbody有数据行（API异步加载）
            for _wait in range(6):
                row_count = await page.locator("tr.text-center").count()
                tbody_has_children = await page.evaluate(
                    "() => { const tb = document.querySelector('tbody.content'); return tb ? tb.children.length : 0; }")
                if row_count > 0 or tbody_has_children > 0:
                    break
                debug(f"  等待课程数据加载({_wait+1}/6)...")
                await page.wait_for_timeout(5000)
            await page.wait_for_timeout(2000)

            # 检查页面是否加载了课程表格
            row_count = await page.locator("tr.text-center").count()
            if row_count == 0:
                # 表格没加载出来，打印页面关键信息帮助排查
                try:
                    url_now = page.url
                    body_text = await page.locator("body").inner_text(timeout=3000)
                    # 只取前500字符避免刷屏
                    debug(f"  页面无课程表格, URL: {url_now}")
                    debug(f"  页面内容: {body_text[:500]}")
                except:
                    pass

            rows_data = await page.evaluate("""() => {
                const results = [];
                document.querySelectorAll('tr.text-center').forEach(tr => {
                    const cells = tr.querySelectorAll('td');
                    if (cells.length < 4) return;
                    const typeCell = cells[0].querySelector('.course-type');
                    const pct = cells[4].querySelector('.percent-text');
                    const actionSpan = cells[5].querySelector('.edit-block');
                    const link = cells[1].querySelector('a') || tr.querySelector('a[href*="course"]');
                    const href = link ? link.getAttribute('href') : '';
                    const dataId = tr.getAttribute('data-id') || tr.getAttribute('data-course-id') || '';
                    results.push({
                        type: typeCell ? typeCell.innerText.trim() : cells[0].innerText.trim(),
                        title: cells[1].innerText.trim(),
                        required: cells[2].innerText.trim(),
                        hours: cells[3].innerText.trim(),
                        progress: pct ? pct.innerText.trim() : cells[4].innerText.trim(),
                        action: actionSpan ? actionSpan.innerText.trim() : cells[5].innerText.trim(),
                        url: href || dataId || ''
                    });
                });
                return results;
            }""")

            debug(f"  原始表格行数: {len(rows_data)}, URL: {page.url}")
            if not rows_data:
                # 没有任何行，dump页面关键区域
                try:
                    html_snippet = await page.evaluate("""() => {
                        const t = document.querySelector('table');
                        if (t) return t.outerHTML.substring(0, 1000);
                        const main = document.querySelector('.workshop-detail, .detail-content, #app');
                        if (main) return main.innerHTML.substring(0, 1000);
                        return document.body.innerHTML.substring(0, 1000);
                    }""")
                    debug(f"  页面HTML: {html_snippet[:500]}")
                except:
                    pass

            skipped = []  # 被过滤的课程
            for row in rows_data:
                title = row.get('title', '').strip()
                ctype = row.get('type', '').strip()
                # 排除考试/scorm（不能自动完成）
                if ctype in ('考试', 'scorm'):
                    skipped.append(f"{ctype}: {title[:30]}")
                    continue
                if title and len(title) > 3:
                    courses.append(row)

            # debug: 打印所有 action 值帮助排查
            action_vals = set(c.get('action', '') for c in courses)
            debug(f"课程 action 值: {action_vals}")
            if courses:
                debug(f"前3门课程 action: {[(c['title'][:30], c['action']) for c in courses[:3]]}")

            # 检查是否有NaN（页面未完全加载）
            has_nan = False
            for c in courses:
                for v in c.values():
                    if isinstance(v, str) and 'NaN' in v:
                        has_nan = True
                        break
            if has_nan:
                debug("  课程数据包含NaN，页面未完全加载")
                return None  # 返回None表示需要重试

            # 区分：表格有数据但全被过滤 vs 表格根本没数据
            raw_count = await page.locator("tr.text-center").count()
            if raw_count > 0 and len(courses) == 0:
                # 表格有行但全被过滤（图书/考试等），不需要重试
                skipped_str = ", ".join(skipped[:5])
                if len(skipped) > 5:
                    skipped_str += f" 等{len(skipped)}项"
                debug(f"  表格有{raw_count}行但全被过滤: {skipped_str}")
                prefix = f"[{ws_title[:20]}] " if ws_title else ""
                console.print(f"{prefix}课程列表: 0 门（过滤: {skipped_str}）", style="yellow")
                return []  # 返回空列表表示确实没有可学课程

            if raw_count == 0 and len(courses) == 0:
                # 表格没数据，需要重试
                debug("  表格无数据行，需要重试")
                return None  # 返回None表示需要重试

            prefix = f"[{ws_title[:20]}] " if ws_title else ""
            console.print(f"{prefix}课程列表: {len(courses)} 门", style="green")
        except Exception as e:
            console.print(f"获取课程列表失败: {e}", style="yellow")
            import traceback
            traceback.print_exc()
            return None  # 异常也返回None表示需要重试

        return courses

    async def display_course_table(self, courses: List[Dict]):
        """显示课程表格"""
        if not courses:
            console.print("未获取到课程", style="yellow")
            return
        
        table = Table(title="课程列表（共{}门）".format(len(courses)))
        table.add_column("#", style="cyan", width=3)
        table.add_column("类型", style="blue", width=6)
        table.add_column("课程名称", style="white")
        table.add_column("学时", style="green", width=6)
        table.add_column("进度", style="magenta", width=8)
        table.add_column("操作", style="yellow", width=10)
        
        # 排除图书类型（显示时过滤）
        _display_courses = [c for c in courses if '图书' not in c.get('type', '')]
        for i, c in enumerate(_display_courses, 1):
                    table.add_row(
                str(i),
                c.get('type', '')[:4],
                c.get('title', '')[:50],
                c.get('hours', ''),
                c.get('progress', ''),
                c.get('action', '')
            )
        
        console.print(table)
        
        # 统计
        total = len(courses)
        to_learn = sum(1 for c in courses if self._is_learnable(c.get('action', ''), c.get('hours', '')))
        done = total - to_learn
        console.print(f"总计 {total} 门，可学习 {to_learn} 门，已完成 {done} 门",
                     style="bold blue")


    async def _get_study_hours(self, page=None) -> dict:
        # 从学习中心获取今年的培训学时（每次创建新页面，避免被关闭）
        _page = None
        try:
            _page = await self.context.new_page()
            await _page.goto("https://u.ccb.com/portal/#/studyCenter",
                           wait_until="domcontentloaded", timeout=20000)
            await _page.wait_for_timeout(8000)
            text = await _page.locator("body").inner_text(timeout=5000)
        except Exception as _ex:
            debug(f"学习中心加载失败: {_ex}")
            return {"central": 0, "online": 0, "total": 0}
        finally:
            if _page:
                try:
                    await _page.close()
                except:
                    pass
        
        import re as _re
        central = 0.0
        online = 0.0
        debug(f"学习中心页面内容:\n{text[:600]}")
        
        # 方法1: 找"今年已训"文本并解析
        if "今年已训" in text:
            after = text.split("今年已训")[1]
            if "完成进度" in after:
                after = after.split("完成进度")[0]
            nums = _re.findall(r'([\d.]+)\s*学时', after)
            if len(nums) >= 1:
                central = float(nums[0])
            if len(nums) >= 2:
                online = float(nums[1])
        else:
            # 方法2: 查找页面中的所有数字+学时
            debug(f"学习中心未找到[今年已训]，检查页面文本")
            nums = _re.findall(r'([\d.]+)\s*学时', text)
            debug(f"找到学时数字: {nums}")
            if len(nums) >= 4:
                # 格式: 应完成X学时, 应完成Y学时, 已训A学时, 已训B学时
                central = float(nums[2]) if len(nums) > 2 else 0
                online = float(nums[3]) if len(nums) > 3 else 0
        
        debug(f"学时解析: 集中培训={central}, 网络自学={online}")
        return {"central": central, "online": online, "total": central + online}

    async def _course_mode(self, page: Page):
        # 从 /course/#/list/1 选择课程学习
        console.print("课程列表模式", style="bold")
        list_url = "https://u.ccb.com/course/#/list/1"
        await page.goto(list_url, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(5000)

        # 课程列表交互式筛选（与专题班标签同结构）
        _fk = await async_input("是否筛选课程？(y/n，默认n)", default="n", timeout=5)
        if _fk in ('y', 'yes'):
            await page.wait_for_timeout(2000)
            # 提取tag-second过滤项
            _fitems = page.locator("li.tag-second:not(.active)")
            _fc = await _fitems.count()
            if _fc > 0:
                console.print("\n可选筛选条件:", style="bold")
                _all_flt = []
                for _fi in range(_fc):
                    _txt = (await _fitems.nth(_fi).inner_text()).strip()
                    console.print(f"  [{_fi+1:3d}] {_txt[:25]}", style="white")
                    _all_flt.append(_fitems.nth(_fi))
                _sel = await async_input("输入编号（逗号分隔，回车跳过）", default="", timeout=30)
                for _part in _sel.split(","):
                    _part = _part.strip()
                    if _part.isdigit() and 1 <= int(_part) <= len(_all_flt):
                        await _all_flt[int(_part)-1].click()
                        await page.wait_for_timeout(1500)
                await page.wait_for_timeout(3000)
            else:
                console.print("未找到筛选选项", style="yellow")

        console.print("正在提取课程列表...", style="blue")
        courses_data = []
        
        # 分页收集
        _max_page = 1
        try:
            # 等待分页栏出现
            await page.wait_for_selector("[class*=page-next], [class*=page_num]", timeout=10000)
            if await page.locator("[class*=page-next]").count() > 0:
                _pn = page.locator("[class*=page_num]")
                _tp = await _pn.count()
                if _tp > 0:
                    _lt = (await _pn.nth(_tp - 1).inner_text()).strip()
                    if _lt.isdigit():
                        console.print(f"当前显示约 {_lt} 页", style="blue")
                        _ip = await async_input("获取前几页？(回车=1，0=全部)", default="1", timeout=10)
                        _max_page = int(_lt) if _ip == "0" else (int(_ip) if _ip.isdigit() and int(_ip) > 0 else 1)
        except:
            debug("未检测到分页控件，尝试文本检测")
            _body = await page.locator("body").inner_text()
            if "下一页" in _body or "Next" in _body:
                _ip = await async_input("检测到分页，获取前几页？(回车=1，0=全部)", default="1", timeout=10)
                _max_page = 999 if _ip == "0" else (int(_ip) if _ip.isdigit() and int(_ip) > 0 else 1)
            else:
                debug("页面无分页")
        
        for _pg in range(_max_page):
            if _pg > 0:
                try:
                    _nb = page.locator("[class*=page-next]:not([class*=page_disabled])")
                    if await _nb.count() > 0:
                        await _nb.first.click()
                        await page.wait_for_timeout(5000)
                    else:
                        break
                except:
                    break
            
            _cards = page.locator("a.p-cursor[title]")
            _cc = await _cards.count()
            for _ci in range(_cc):
                _t = await _cards.nth(_ci).get_attribute("title")
                if _t:
                    courses_data.append({"title": _t.strip()[:60], "hours": ""})

        if not courses_data:
            console.print("未获取到课程", style="yellow")
            return

        console.print(f"找到 {len(courses_data)} 门课程:", style="green")
        for i, c in enumerate(courses_data, 1):
            console.print(f"  [{i:3d}] {c['title']}", style="white")

        console.print()
        sel = await async_input("输入课程编号（逗号/范围分隔，回车全学）", default="", timeout=30)
        indices = list(range(len(courses_data)))
        if sel:
            indices = []
            for p in sel.split(","):
                p = p.strip()
                if "-" in p:
                    a, b = p.split("-", 1)
                    indices.extend(range(int(a)-1, int(b)))
                elif p.isdigit():
                    indices.append(int(p)-1)
            indices = [i for i in indices if 0 <= i < len(courses_data)]

        nw = min(self.workers, len(indices))
        console.print(f"使用 {nw} 个工作线程学习 {len(indices)} 门课程", style="bold blue")

        async def cworker(wid, wp, aidx):
            for gi in aidx:
                c = courses_data[gi]
                console.print(f"[工作线程 {wid+1}] {c['title'][:35]}", style="bold blue")
                try:
                    await wp.goto(list_url, wait_until="networkidle", timeout=20000)
                    await wp.wait_for_timeout(5000)
                    links = wp.locator("a.p-cursor[title]")
                    if gi >= await links.count():
                        continue
                    async with wp.expect_event("popup", timeout=20000) as pi:
                        await links.nth(gi).click()
                    cp = await pi.value
                    await cp.wait_for_load_state()
                    await cp.wait_for_timeout(5000)
                    for kw in ["我要学习", "开始学习", "进入课程", "继续学习", "学习课程"]:
                        try:
                            sb = cp.locator(f"text={kw}").first
                            if await sb.count() > 0:
                                debug(f"找到 {kw}")
                                await sb.click()
                                await cp.wait_for_timeout(5000)
                                break
                        except:
                            pass
                    await self.find_and_play_video(cp, wid)
                    try:
                        await cp.close()
                    except:
                        pass
                except Exception as e:
                    debug(f"课程异常: {e}")

                # 检查学习目标
                if self.study_goal > 0 and not self.goal_reached:
                    h = await self._get_study_hours(wp)
                    cur = h.get("online", 0)
                    console.print(f"网络自学: {cur:.1f}/{self.study_goal} 学时", style="blue")
                    if cur >= self.study_goal:
                        console.print("已达到学习目标! 程序退出", style="bold green")
                        raise GoalReached()

        tasks = []
        for wid in range(nw):
            aidx = [indices[j] for j in range(wid, len(indices), nw)]
            tasks.append(asyncio.create_task(cworker(wid, self.pages[wid], aidx)))
            await asyncio.sleep(3)
        await asyncio.gather(*tasks)
        console.print("课程模式学习完成", style="bold green")

    @staticmethod
    def _is_learnable(action: str, hours: str = "") -> bool:
        """判断课程是否可以学习（未完成或进行中）"""
        if not action:
            return False
        # 跳过0学时课程
        try:
            h = float(hours) if hours else -1
            if h == 0:
                return False
        except:
            pass
        # 100%完成 → 不需要学
        if action in ('立即回看', '学习完成', '已完成', '已学习'):
            return False
        # 明确可学的状态
        if action in ('立即学习', '继续学习', '继续回看',
                       '开始学习', '进入课程', '学习课程'):
            return True
        # 含"学习"但不含"完成"
        if '学习' in action and '完成' not in action:
            return True
        return False

    async def _collect_one_by_click(self, ws: dict, completed_ids: set,
                                     list_page: Page = None) -> dict:
        """点击模式采集：从列表页点击进入专题班，获取课程列表"""
        ws_title = ws['title'][:50]
        detail_link = ws.get('detail_link', '')
        ws_id = ""
        m = re.search(r'id=([a-f0-9\-]+)', detail_link)
        if m:
            ws_id = m.group(1)
        if not ws_id:
            return None
        if ws_id in completed_ids:
            return None

        cp = None
        try:
            cp = await self.context.new_page()
            # 1. 导航到列表页
            list_url = "https://u.ccb.com/workshop/#/index?collegeId=&departmentId=&orderby=praise"
            await cp.goto(list_url, wait_until="domcontentloaded", timeout=20000)
            await cp.wait_for_timeout(6000)

            # 如果有标签筛选，应用标签
            if self.tags_to_learn:
                for tag in self.tags_to_learn:
                    try:
                        all_tags = cp.locator("ul.tag-tree-list span.single-tag")
                        cnt = await all_tags.count()
                        for i in range(cnt):
                            text = (await all_tags.nth(i).inner_text()).strip()
                            if text == tag:
                                await all_tags.nth(i).click()
                                await cp.wait_for_timeout(3000)
                                break
                    except:
                        pass

            # 2. 在列表中找到并点击专题班卡片
            clicked = False
            for scroll_try in range(3):  # 最多翻3页找
                cards = cp.locator(".workshop-content-list li.clearfix")
                cnt = await cards.count()
                page_titles = []
                found_on_page = False
                for i in range(cnt):
                    card = cards.nth(i)
                    try:
                        title_text = await card.locator(".workshop-list-content-title").inner_text(timeout=2000)
                        page_titles.append(title_text.strip()[:30])
                        if ws['title'].strip() in title_text.strip():
                            link = card.locator("a").first
                            if await link.count() > 0:
                                await link.click()
                                clicked = True
                                found_on_page = True
                                break
                    except:
                        continue
                if clicked:
                    break
                # 记录当前页看到的卡片，帮助排查
                debug(f"  [点击模式] 第{scroll_try+1}页({cnt}个): {page_titles[:5]}")
                # 翻页继续找
                if not found_on_page:
                    moved = await self.go_to_next_page(cp)
                    if not moved:
                        break
                    await cp.wait_for_timeout(3000)

            if not clicked:
                debug(f"  [点击模式] 未在列表中找到: {ws_title}")
                return None

            # 3. 等待详情页加载
            await cp.wait_for_timeout(8000)
            try:
                await cp.wait_for_load_state("networkidle", timeout=15000)
            except:
                pass

            # 检查是否报名截止
            body_text = ""
            try:
                body_text = await cp.locator("body").inner_text(timeout=3000)
            except:
                pass
            if "报名截止" in body_text or "报名已结束" in body_text:
                debug(f"  [点击模式] 报名已截止: {ws_title}")
                return None

            # 4. 点击"课程"标签页
            for tab_text in ["课程", "课程列表", "课程目录"]:
                try:
                    tab = cp.locator(f"text={tab_text}").first
                    if await tab.count() > 0 and await tab.is_visible():
                        await tab.click()
                        await cp.wait_for_timeout(5000)
                        break
                except:
                    pass

            # 5. 获取课程列表
            courses = await self.get_courses_from_workshop(cp, ws_title)
            if courses is None:
                # NaN或数据未加载，等待后重试一次
                await cp.wait_for_timeout(8000)
                courses = await self.get_courses_from_workshop(cp, ws_title)

            if not courses:
                debug(f"  [点击模式] 未获取到课程: {ws_title}")
                return None

            to_learn = [(i, c) for i, c in enumerate(courses)
                        if self._is_learnable(c.get('action', ''), c.get('hours', ''))]
            if not to_learn:
                if ws_id not in completed_ids:
                    completed_ids.add(ws_id)
                    self.mark_workshop_completed(ws_id)
                console.print(f"  ✓ [点击模式] 全部已完成（共{len(courses)}门）", style="green")
            else:
                console.print(f"  ✓ [点击模式] {len(to_learn)} 门待学（共{len(courses)}门）", style="green")

            return {
                "ws_id": ws_id,
                "ws_title": ws_title,
                "tasks": [(ws_id, ci, c, ws_title) for ci, c in to_learn],
                "courses": courses
            }
        except Exception as e:
            debug(f"  [点击模式] 异常: {e}")
            return None
        finally:
            if cp:
                try:
                    await cp.close()
                except:
                    pass

    async def _collect_workshops_courses(self, page: Page, workshops: List[Dict],
                                          completed_ids: set = None, log_callback=None) -> tuple:
        """预收集：并行 enroll + 获取课程列表，跳过已完成的专题班"""
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        if completed_ids is None:
            completed_ids = set()

        to_process = list(workshops)

        if completed_ids:
            _log(f"已完成 {len(completed_ids)} 个专题班，将跳过", "green")

        all_tasks = []       # [(ws_id, course_idx, course_info, ws_title), ...]
        ws_locks = {}        # ws_id -> asyncio.Lock

        # 每个collector用独立页面，避免并发干扰
        COLLECT_CONCURRENCY = 10
        collect_pages = []
        for _ in range(COLLECT_CONCURRENCY):
            try:
                collect_pages.append(await self.context.new_page())
            except:
                pass

        _log(f"使用 {len(collect_pages)} 个页面并行采集", "blue")

        # 每个采集页都先导航到专题班列表
        async def init_collect_page(cp):
            try:
                await cp.goto("https://u.ccb.com/workshop/#/index?collegeId=&departmentId=&orderby=praise",
                              wait_until="networkidle", timeout=20000)
                await cp.wait_for_timeout(3000)
                if self.tags_to_learn:
                    for tag in self.tags_to_learn:
                        try:
                            all_tags = cp.locator("ul.tag-tree-list span.single-tag")
                            cnt = await all_tags.count()
                            for i in range(cnt):
                                text = (await all_tags.nth(i).inner_text()).strip()
                                if text == tag:
                                    await all_tags.nth(i).click()
                                    await cp.wait_for_timeout(3000)
                                    break
                        except:
                            pass
            except:
                pass

        await asyncio.gather(*(init_collect_page(cp) for cp in collect_pages))

        sem = asyncio.Semaphore(COLLECT_CONCURRENCY)

        async def collect_one(idx: int, ws: dict, cp: Page):
            """单个专题班：每个专题班用新页面，避免SPA状态累积"""
            ws_title = ws['title'][:50]
            async with sem:
                _log(f"[采集 {idx+1}/{len(to_process)}] {ws_title}", "blue")

                # 从 detail_link 提取 workshop ID
                detail_link = ws.get('detail_link', '')
                ws_id = ""
                m = re.search(r'id=([a-f0-9\-]+)', detail_link)
                if m:
                    ws_id = m.group(1)
                if not ws_id:
                    _log(f"  ✗ 无法从链接提取ID: {detail_link[:60]}", "red")
                    return None

                # 检查是否已完成
                if ws_id in completed_ids:
                    _log(f"  ⊘ 已完成，跳过: {ws_title[:30]}", "green")
                    return None

                # 每个专题班创建新页面（避免SPA状态累积导致后面失败）
                try:
                    cp = await self.context.new_page()
                except:
                    pass
                try:
                    # 直接goto导航到专题班详情页
                    ws_url = f"https://u.ccb.com/workshop/#/myworkshop/detail?id={ws_id}"
                    try:
                        await cp.goto(ws_url, wait_until="domcontentloaded", timeout=20000)
                        await cp.wait_for_timeout(6000)
                    except:
                        pass

                    body_text = ""
                    try:
                        body_text = await cp.locator("body").inner_text(timeout=3000)
                    except:
                        pass

                    # 检查是否报名截止（列表页已过滤，这里兜底）
                    if "报名截止" in body_text or "报名已结束" in body_text:
                        _log(f"  ⊘ 报名已截止，跳过: {ws_title[:30]}", "yellow")
                        return None

                    # 先检查是否需要报名（必须先报名才能看到课程，否则API会400）
                    need_enroll = False
                    for kw in ["立即报名", "加入学习", "免费报名"]:
                        try:
                            btn = cp.locator(f"text={kw}").first
                            if await btn.count() > 0 and await btn.is_visible():
                                _log(f"  需要报名，点击「{kw}」", "blue")
                                old_url = cp.url
                                await btn.click()
                                for _ in range(10):
                                    await cp.wait_for_timeout(2000)
                                    if cp.url != old_url:
                                        break
                                    try:
                                        if not await cp.locator(f"text={kw}").first.is_visible(timeout=1000):
                                            break
                                    except:
                                        break
                                await cp.wait_for_load_state("networkidle", timeout=15000)
                                await cp.wait_for_timeout(3000)
                                need_enroll = True
                                break
                        except:
                            pass

                    # 报名后重新导航到详情页，等服务器处理
                    if need_enroll:
                        try:
                            await cp.goto(ws_url, wait_until="domcontentloaded", timeout=15000)
                            await cp.wait_for_timeout(5000)
                        except:
                            pass
                    else:
                        # 未报名的也点课程标签（已报名的跳过，直接API采集）
                        for tab_text in ["课程", "课程列表", "课程目录"]:
                            try:
                                tab = cp.locator(f"text={tab_text}").first
                                if await tab.count() > 0 and await tab.is_visible():
                                    await tab.click()
                                    await cp.wait_for_timeout(3000)
                                    break
                            except:
                                pass

                        # 等待课程表格加载
                        for _wait in range(3):
                            row_count = await cp.locator("tr.text-center").count()
                            if row_count > 0:
                                break
                            page_text = ""
                            try:
                                page_text = await cp.locator("body").inner_text(timeout=2000)
                            except:
                                pass
                            if "NaN" in page_text or "总课程门" in page_text:
                                debug(f"  表格数据未加载，等待刷新({_wait+1}/3)")
                                await cp.wait_for_timeout(5000)
                            else:
                                break

                    # 获取课程列表：API重试（最多5次，递增等待，400不重试）
                    courses = []
                    API_MAX_RETRIES = 5
                    api_gave_up = False
                    for api_attempt in range(API_MAX_RETRIES):
                        if api_attempt > 0:
                            wait_sec = api_attempt * 3
                            _log(f"  API重试({api_attempt}/{API_MAX_RETRIES})，等{wait_sec}秒...", "yellow")
                            await asyncio.sleep(wait_sec)
                        try:
                            api_result = await self._get_courses_by_api(cp, ws_id, log_callback=_log)
                            # api_result为None表示400等不可恢复错误，不重试
                            if api_result is None:
                                api_gave_up = True
                                break
                            if api_result and isinstance(api_result, dict):
                                data = api_result.get("contentList", [])
                                if not data:
                                    for key in ["courses", "knowledgeList", "courseList", "lessons"]:
                                        val = api_result.get(key)
                                        if isinstance(val, list) and len(val) > 0:
                                            data = val
                                            break
                                if data and isinstance(data, list):
                                    for item in data:
                                        if not isinstance(item, dict):
                                            continue
                                        title = str(item.get("knowledgeName", item.get("title",
                                            item.get("courseName", item.get("name", "")))))
                                        ctype = str(item.get("kngType", item.get("type", "")))
                                        if ctype in ("考试", "scorm", "ExamKnowledge", "ScormKnowledge"):
                                            continue
                                        if not title or len(title) <= 3:
                                            continue
                                        progress_val = item.get("progress", 0)
                                        hours_val = item.get("hours", 0)
                                        detail_url = item.get("kngDetailUrl", "")
                                        course_id = item.get("knowledgeId", item.get("id", ""))
                                        courses.append({
                                            "title": title.strip(),
                                            "type": ctype.strip(),
                                            "required": "必修" if item.get("type") == 1 else "选修",
                                            "hours": str(hours_val),
                                            "progress": f"{float(progress_val)*100:.0f}%" if progress_val else "0%",
                                            "action": "已学习" if progress_val and float(progress_val) >= 1 else "未学习",
                                            "url": detail_url or course_id,
                                        })
                                    if courses:
                                        _log(f"  ✓ API获取 {len(courses)} 门课程", "green")
                                        break
                                    else:
                                        _log(f"  API返回0门课程", "yellow")
                                else:
                                    _log(f"  API无课程数据", "yellow")
                            else:
                                _log(f"  API返回异常", "yellow")
                        except Exception as e:
                            _log(f"  API异常: {e}", "red")

                    if courses is None:
                        courses = []

                    if courses:
                        to_learn = [(i, c) for i, c in enumerate(courses)
                                    if self._is_learnable(c.get('action', ''), c.get('hours', ''))]
                        action_vals = set(c.get('action', '') for c in courses)
                        debug(f"  课程action值: {action_vals}, 待学: {len(to_learn)}")
                        if not to_learn:
                            if ws_id not in completed_ids:
                                completed_ids.add(ws_id)
                                self.mark_workshop_completed(ws_id)
                                debug(f"  标记已完成: {ws_id}")
                            _log(f"  ✓ 全部已完成（共{len(courses)}门）", "green")
                        else:
                            _log(f"  ✓ {len(to_learn)} 门待学（共{len(courses)}门）", "green")
                        return {
                            "ws_id": ws_id,
                            "ws_title": ws_title,
                            "tasks": [(ws_id, ci, c, ws_title) for ci, c in to_learn],
                            "courses": courses
                        }
                    else:
                        _log(f"  ✗ 未获取到课程", "yellow")
                        return None
                finally:
                    try:
                        await cp.close()
                    except:
                        pass

        # 并行执行所有采集任务
        console.print(f"\n开始并行采集 {len(to_process)} 个专题班...", style="bold blue")
        results = await asyncio.gather(
            *(collect_one(i, ws, collect_pages[i % len(collect_pages)])
              for i, ws in enumerate(to_process)),
            return_exceptions=True
        )

        # 汇总结果
        for r in results:
            if isinstance(r, Exception):
                debug(f"采集异常: {r}")
                continue
            if r is None:
                continue
            ws_id = r["ws_id"]
            for t in r["tasks"]:
                all_tasks.append(t)
            if ws_id not in ws_locks:
                ws_locks[ws_id] = asyncio.Lock()

        # 关闭所有采集页面
        for cp in collect_pages:
            try:
                await cp.close()
            except:
                pass

        # 确保主页面回到列表页
        try:
            await page.goto("https://u.ccb.com/workshop/#/index?collegeId=&departmentId=&orderby=praise",
                            wait_until="networkidle", timeout=15000)
            await page.wait_for_timeout(3000)
        except:
            pass

        return all_tasks, ws_locks

    async def parallel_learn_courses(self, all_tasks: List, ws_locks: Dict, fetch_more_callback=None,
                                      progress_callback=None, hours_callback=None, log_callback=None):
        """全局课程队列：所有 worker 跨专题班并发消费，自动标记已完成专题班
        fetch_more_callback: async callable(queue) -> int，队列空时调用，往queue里加新任务，返回新增数
        progress_callback: callable(data_dict) - Textual进度更新回调
        hours_callback: callable(data_dict) - Textual学时更新回调
        log_callback: callable(msg, style) - Textual日志回调"""
        if not all_tasks:
            console.print("没有需要学习的课程", style="green")
            return set()

        num_workers = min(self.workers, len(all_tasks))
        console.print(f"\n[bold]启动 {num_workers} 个工作线程，共 {len(all_tasks)} 门课程[/bold]")

        # 共享的线程状态（用于Live表格显示）
        worker_status = {}
        status_lock = asyncio.Lock()
        study_hours_info = {"central": 0, "online": 0, "updated": "未查询"}
        # 心跳：记录每个worker最后活动时间
        worker_heartbeat = {}  # {w_id: timestamp}
        HEARTBEAT_TIMEOUT = 600  # 10分钟无进展判定卡死
        # 当前任务信息（用于超时重试）
        worker_current_task = {}  # {w_id: (ws_id, cidx, course, ws_title, retry)}
        worker_cancel_event = {}  # {w_id: asyncio.Event}
        # 进度追踪：记录每个worker的(时间戳, 百分比)用于预估剩余时间
        worker_progress_history = {}  # {w_id: [(timestamp, pct), ...]}

        def update_status(w_id, **kwargs):
            """更新线程状态（线程安全）+ 心跳 + 进度追踪"""
            worker_status.setdefault(w_id, {}).update(kwargs)
            worker_heartbeat[w_id] = time.time()
            # Textual回调
            if progress_callback:
                try:
                    info = worker_status.get(w_id, {})
                    try:
                        eta = estimate_remaining(w_id)
                    except:
                        eta = "-"
                    progress_callback({
                        "wid": w_id,
                        "course": info.get("course", "-"),
                        "progress": info.get("progress", "-"),
                        "eta": eta,
                        "status": info.get("status", "-"),
                    })
                except:
                    pass
            # 记录进度变化
            pct_str = kwargs.get("progress", "")
            if pct_str and pct_str != "-":
                try:
                    pct_val = float(pct_str.replace("%", ""))
                    history = worker_progress_history.setdefault(w_id, [])
                    history.append((time.time(), pct_val))
                    # 只保留最近10条记录
                    if len(history) > 10:
                        worker_progress_history[w_id] = history[-10:]
                except:
                    pass

        def estimate_remaining(w_id):
            """根据进度变化率预估剩余时间（平滑计算）"""
            history = worker_progress_history.get(w_id, [])
            status = worker_status.get(w_id, {}).get("status", "")

            # 没有历史记录
            if not history:
                return "..." if status == "学习中" else "-"

            # 已完成
            if history[-1][1] >= 100:
                return "✓"

            # 只有1条记录，用任务开始时间估算
            if len(history) < 2:
                start_time = worker_heartbeat.get(w_id, 0)
                if start_time > 0 and history[-1][1] > 0:
                    elapsed = time.time() - start_time
                    rate = history[-1][1] / elapsed
                    if rate > 0:
                        remaining = (100 - history[-1][1]) / rate
                        return _format_time(remaining)
                return "..." if status == "学习中" else "-"

            # 多条记录：用线性回归计算平均速率
            t0, p0 = history[0]
            t_last, p_last = history[-1]
            dt = t_last - t0
            dp = p_last - p0

            if dt <= 0:
                return "..." if status == "学习中" else "-"

            # 进度没变化但还在学习中
            if dp <= 0:
                if status == "学习中":
                    return "计算中..."
                return "-"

            rate = dp / dt
            if rate <= 0:
                return "-"

            remaining = (100 - p_last) / rate
            return _format_time(remaining)

        def _format_time(seconds):
            """格式化为中文倒计时"""
            if seconds < 0:
                return "计算中"
            if seconds < 60:
                return f"剩{seconds:.0f}秒"
            elif seconds < 3600:
                m = int(seconds // 60)
                s = int(seconds % 60)
                return f"剩{m}分{s}秒" if s else f"剩{m}分"
            else:
                h = int(seconds // 3600)
                m = int((seconds % 3600) // 60)
                return f"剩{h}时{m}分" if m else f"剩{h}时"

        def make_progress_table():
            # 学时信息面板
            hours_table = Table(title="培训学时", show_header=False, box=None, padding=(0, 2))
            hours_table.add_column("项目", style="cyan")
            hours_table.add_column("数值", style="green")
            h = study_hours_info
            hours_table.add_row("集中培训", f"{h['central']:.1f} 学时")
            hours_table.add_row("网络自学", f"{h['online']:.1f} 学时")
            if self.study_goal > 0:
                goal_type_name = "集中培训" if self.goal_type == "central" else "网络自学"
                cur = h.get(self.goal_type, 0)
                pct = min(100, cur / self.study_goal * 100) if self.study_goal > 0 else 0
                bar = "█" * int(pct // 5) + "░" * (20 - int(pct // 5))
                hours_table.add_row("目标", f"{goal_type_name} {self.study_goal:.0f} 学时")
                hours_table.add_row("进度", f"[{'bold green' if pct >= 100 else 'yellow'}]{bar} {pct:.1f}%[/]")
            hours_table.add_row("更新时间", h['updated'])

            # 线程进度表
            table = Table(title=f"学习进度（完成 {completed_count[0]}/{total}，失败 {failed[0]}）")
            table.add_column("线程", style="cyan", width=4)
            table.add_column("课程", style="white", width=36)
            table.add_column("进度", style="green", width=6)
            table.add_column("预计", style="magenta", width=6)
            table.add_column("状态", style="yellow", width=10)
            for wid in range(num_workers):
                info = worker_status.get(wid, {})
                eta = estimate_remaining(wid) if info.get("status") == "学习中" else "-"
                table.add_row(
                    str(wid + 1),
                    info.get("course", "-"),
                    info.get("progress", "-"),
                    eta,
                    info.get("status", "等待中")
                )
            # 合并两个表
            from rich.console import Group
            return Group(hours_table, table)

        # 进度统计
        total = len(all_tasks)
        completed_count = [0]
        failed = [0]
        lock_stat = asyncio.Lock()

        # 按专题班统计完成情况：{ws_id: {"total": N, "done": N, "title": str}}
        ws_progress = {}
        for ws_id, cidx, course, ws_title in all_tasks:
            if ws_id not in ws_progress:
                ws_progress[ws_id] = {"total": 0, "done": 0, "title": ws_title}
            ws_progress[ws_id]["total"] += 1
        completed_ws_ids = set()

        # 构建任务队列（每个任务带重试计数）
        MAX_RETRY = 3
        course_queue = asyncio.Queue()
        seen_courses = set()  # 去重：已见过的课程URL
        dedup_count = 0
        for t in all_tasks:
            ws_id, cidx, course, ws_title = t
            # 用URL去重，没有URL则用标题
            dedup_key = course.get('url', '') or course['title'].strip()[:50]
            if dedup_key and dedup_key in seen_courses:
                dedup_count += 1
                # 重复课程直接标记完成（不入队）
                if ws_id not in ws_progress:
                    ws_progress[ws_id] = {"total": 0, "done": 0, "title": ws_title}
                ws_progress[ws_id]["total"] += 1
                ws_progress[ws_id]["done"] += 1
                continue
            if dedup_key:
                seen_courses.add(dedup_key)
            course_queue.put_nowait((*t, 0))  # (ws_id, cidx, course, ws_title, retry)
        if dedup_count > 0:
            console.print(f"  去重: 跳过 {dedup_count} 门重复课程", style="yellow")

        def retry_task(ws_id, cidx, course, ws_title, retry):
            """失败任务放回队列重试"""
            if retry < MAX_RETRY:
                course_queue.put_nowait((ws_id, cidx, course, ws_title, retry + 1))
                update_status(w_id, status=f"重试({retry+1}/{MAX_RETRY})")
                return True
            return False

        async def worker(w_id: int, page: Page):
            """单个工作线程：从队列取任务，独立完成学习"""
            cancel_event = asyncio.Event()
            worker_cancel_event[w_id] = cancel_event

            while True:
                try:
                    ws_id, cidx, course, ws_title, retry = await asyncio.wait_for(
                        course_queue.get(), timeout=30)
                except (asyncio.TimeoutError, asyncio.QueueEmpty):
                    # 队列获取超时——但不代表队列真的空了（可能是ws_lock排队）
                    if course_queue.qsize() > 0:
                        continue  # 队列还有任务，继续取
                    # 队列确实空了，尝试采集更多课程
                    if fetch_more_callback:
                        try:
                            added = await fetch_more_callback(course_queue)
                            if added > 0:
                                continue  # 有新课程，继续取
                        except Exception as e:
                            debug(f"[工作线程 {w_id+1}] 采集回调异常: {e}")
                    break

                title = course['title'][:40]
                ws_url = f"https://u.ccb.com/workshop/#/myworkshop/detail?id={ws_id}"
                # 记录当前任务（供心跳超时重试）
                worker_current_task[w_id] = (ws_id, cidx, course, ws_title, retry)
                cancel_event.clear()

                try:
                    update_status(w_id, course=title, workshop=ws_title[:20], progress="-", status="加载中")

                    # 1) 导航到专题班页（reload确保SPA刷新内容）
                    try:
                        await page.goto(ws_url, wait_until="networkidle", timeout=20000)
                        await page.reload(wait_until="networkidle", timeout=20000)
                        await page.wait_for_selector("tr.text-center", timeout=15000)
                        await page.wait_for_timeout(2000)
                    except Exception as e:
                        debug(f"[工作线程 {w_id+1}] 页面加载异常: {traceback.format_exc()}")
                        if retry_task(ws_id, cidx, course, ws_title, retry):
                            continue
                        update_status(w_id, status="加载失败")
                        async with lock_stat:
                            failed[0] += 1
                        continue

                    # 2) 加锁：同一专题班的课程串行点击
                    course_page = None
                    async with ws_locks.get(ws_id, asyncio.Lock()):
                        rows = page.locator("tr.text-center")
                        row_count = await rows.count()

                        # 按标题查找课程行（不依赖索引，避免索引超限）
                        row = None
                        course_title = course.get('title', '').strip()[:30]
                        if course_title:
                            for i in range(row_count):
                                try:
                                    r = rows.nth(i)
                                    text = await r.inner_text(timeout=2000)
                                    if course_title in text:
                                        row = r
                                        break
                                except:
                                    pass
                        # 兜底：用索引
                        if row is None and cidx < row_count:
                            row = rows.nth(cidx)
                        if row is None:
                            update_status(w_id, status="未找到课程")
                            async with lock_stat:
                                failed[0] += 1
                            continue

                        btn = row.locator("span.edit-block").first
                        if await btn.count() == 0:
                            update_status(w_id, status="无按钮")
                            async with lock_stat:
                                failed[0] += 1
                            continue

                        try:
                            async with page.expect_event("popup", timeout=20000) as pi:
                                await btn.click()
                            course_page = await pi.value
                            await course_page.wait_for_load_state()
                        except Exception as e:
                            debug(f"[工作线程 {w_id+1}] popup异常: {traceback.format_exc()}")
                            if course_page:
                                try: await course_page.close()
                                except: pass
                            if retry_task(ws_id, cidx, course, ws_title, retry):
                                continue
                            update_status(w_id, status="打开失败")
                            async with lock_stat:
                                failed[0] += 1
                            continue

                    # 3) 找学习按钮
                    update_status(w_id, status="查找按钮")
                    found_btn = False
                    for kw in ["我要学习", "开始学习", "进入课程", "继续学习", "学习课程", "进入课程学习"]:
                        try:
                            await course_page.wait_for_selector(f"text={kw}", timeout=8000)
                            sb = course_page.locator(f"text={kw}").first
                            if await sb.count() > 0:
                                await sb.click()
                                await course_page.wait_for_timeout(5000)
                                found_btn = True
                                break
                        except:
                            pass

                    # 4) 播放视频（传入进度回调和课程类型）
                    update_status(w_id, status="学习中", progress="0%")
                    def on_progress(pct):
                        update_status(w_id, progress=f"{pct:.0f}%", status="学习中")
                    await self.find_and_play_video(course_page, w_id, on_progress,
                                                   course_type=course.get('type', ''))

                    # 5) 关闭课程标签页
                    try:
                        await course_page.close()
                    except:
                        pass

                    # 6) 更新进度 + 检查专题班是否全部完成
                    async with lock_stat:
                        completed_count[0] += 1
                        wp = ws_progress.get(ws_id)
                        if wp:
                            wp["done"] += 1
                            if wp["done"] >= wp["total"] and ws_id not in completed_ws_ids:
                                completed_ws_ids.add(ws_id)
                                self.mark_workshop_completed(ws_id)
                        update_status(w_id, status="✓ 完成", progress="100%")
                        if log_callback:
                            log_callback(f"[线程{w_id+1}] 完成: {title}", "green")

                    # 7) 完成一门课后更新学时
                    try:
                        _h = await self._get_study_hours(page)
                        study_hours_info.update({
                            "central": _h.get("central", 0),
                            "online": _h.get("online", 0),
                            "updated": datetime.now().strftime("%H:%M:%S")
                        })
                        # 检查是否达标
                        if self.study_goal > 0:
                            _cur = _h.get(self.goal_type, 0)
                            if _cur >= self.study_goal:
                                update_status(w_id, status="目标达成!")
                                raise GoalReached()
                    except:
                        pass

                except Exception as e:
                    debug(f"[工作线程 {w_id+1}] 未捕获异常:\n{traceback.format_exc()}")
                    try:
                        if course_page and not course_page.is_closed():
                            await course_page.close()
                    except:
                        pass
                    if retry_task(ws_id, cidx, course, ws_title, retry):
                        continue
                    update_status(w_id, status="异常")
                    async with lock_stat:
                        failed[0] += 1

            update_status(w_id, status="已退出", course="-", workshop="-")

        # 用 Live 表格实时刷新
        from rich.live import Live
        # 创建独立页面用于定时查询学时（不与worker冲突）
        try:
            hours_page = await self.context.new_page()
        except:
            hours_page = None

        # 启动前先查询一次学时
        if hours_page:
            try:
                _h = await self._get_study_hours(hours_page)
                _info = {
                    "central": _h.get("central", 0),
                    "online": _h.get("online", 0),
                    "updated": datetime.now().strftime("%H:%M:%S"),
                }
                study_hours_info.update(_info)
                if hours_callback:
                    hours_callback(_info)
                if log_callback:
                    log_callback(f"学时更新: 集中{_info['central']:.1f} 网络{_info['online']:.1f}", "blue")
            except:
                pass

        # 启动所有 worker
        tasks = []
        for w_id in range(num_workers):
            tasks.append(asyncio.create_task(worker(w_id, self.pages[w_id])))
            await asyncio.sleep(2)

        # 定时采集学时间隔（秒）
        HOURS_CHECK_INTERVAL = 60

        # 心跳检测 + 刷新
        async def refresh_display():
            import time
            last_hours_check = time.time()
            while not all(t.done() for t in tasks):
                now = time.time()

                # 定时采集总体学习进度（两种模式统一处理）
                if now - last_hours_check >= HOURS_CHECK_INTERVAL and hours_page:
                    last_hours_check = now
                    try:
                        _h = await asyncio.wait_for(
                            self._get_study_hours(hours_page), timeout=30)
                        _info = {
                            "central": _h.get("central", 0),
                            "online": _h.get("online", 0),
                            "updated": datetime.now().strftime("%H:%M:%S"),
                        }
                        study_hours_info.update(_info)
                        if hours_callback:
                            hours_callback(_info)
                        if log_callback:
                            log_callback(f"学时更新: 集中{_info['central']:.1f} 网络{_info['online']:.1f}", "blue")
                        # 定时检查目标学时（避免因平台统计延迟导致多学）
                        if self.study_goal > 0:
                            _cur = _h.get(self.goal_type, 0)
                            if _cur >= self.study_goal:
                                raise GoalReached()
                    except GoalReached:
                        raise
                    except Exception as _hex:
                        debug(f"学时刷新失败: {_hex}")
                        if log_callback:
                            log_callback(f"学时刷新失败: {_hex}", "yellow")

                # Rich模式：更新Live表格
                if live_ctx:
                    live_ctx.update(make_progress_table())

                await asyncio.sleep(2)
                # 心跳检测
                now = time.time()
                for wid in range(num_workers):
                    last = worker_heartbeat.get(wid, 0)
                    if last > 0 and now - last > HEARTBEAT_TIMEOUT:
                        info = worker_status.get(wid, {})
                        status = info.get("status", "")
                        if status in ("学习中", "查找按钮", "加载中"):
                            debug(f"[心跳] 工作线程 {wid+1} 超时({now-last:.0f}s)，触发重试")
                            task_info = worker_current_task.get(wid)
                            if task_info:
                                ws_id, cidx, course, ws_title, retry = task_info
                                if retry < MAX_RETRY:
                                    course_queue.put_nowait((ws_id, cidx, course, ws_title, retry + 1))
                                    update_status(wid, status=f"超时重试")
                                    try:
                                        await self.pages[wid].close()
                                    except:
                                        pass
                                    try:
                                        self.pages[wid] = await self.context.new_page()
                                    except:
                                        pass
                                else:
                                    update_status(wid, status="超时放弃")
                                    async with lock_stat:
                                        failed[0] += 1
                            worker_heartbeat[wid] = now
            if live_ctx:
                live_ctx.update(make_progress_table())

        # 根据模式选择显示方式
        if progress_callback:
            # Textual模式：不使用Rich Live
            live_ctx = None
            refresh_task = asyncio.create_task(refresh_display())
            _done, _pending = await asyncio.wait(
                [refresh_task, *tasks], return_when=asyncio.FIRST_EXCEPTION)
            for p in _pending:
                p.cancel()
                try:
                    await p
                except (asyncio.CancelledError, Exception):
                    pass
            for d in _done:
                if d is not refresh_task:
                    try:
                        d.result()
                    except GoalReached:
                        for t in tasks:
                            if not t.done():
                                t.cancel()
                    except:
                        pass
        else:
            # CLI模式：使用Rich Live
            with Live(make_progress_table(), console=console, refresh_per_second=1) as live:
                live_ctx = live
                refresh_task = asyncio.create_task(refresh_display())
                _done, _pending = await asyncio.wait(
                    [refresh_task, *tasks], return_when=asyncio.FIRST_EXCEPTION)
                for p in _pending:
                    p.cancel()
                    try:
                        await p
                    except (asyncio.CancelledError, Exception):
                        pass
                for d in _done:
                    if d is not refresh_task:
                        try:
                            d.result()
                        except GoalReached:
                            for t in tasks:
                                if not t.done():
                                    t.cancel()
                        except:
                            pass

        if log_callback:
            log_callback(f"学习任务完成: 成功 {completed_count[0]} 门, 失败 {failed[0]} 门", "bold green")
            log_callback(f"已完成 {len(completed_ws_ids)}/{len(ws_progress)} 个专题班", "green")
        else:
            console.print(f"\n[bold green]学习任务完成: 成功 {completed_count[0]} 门, 失败 {failed[0]} 门[/bold green]")
            console.print(f"已完成 {len(completed_ws_ids)}/{len(ws_progress)} 个专题班", style="green")
        return completed_ws_ids

    async def learn_from_urls(self, urls: List[str], workers: int = 1,
                               progress_callback=None, hours_callback=None, log_callback=None):
        """手动模式：从指定URL列表学习课程"""
        _log = log_callback or (lambda msg, style="": console.print(msg, style=style))
        _progress = progress_callback or (lambda d: None)
        _hours = hours_callback or (lambda d: None)

        page = self.pages[0]

        # 从URL中提取专题班ID
        workshop_ids = []
        for url in urls:
            m = re.search(r'id=([a-f0-9\-]+)', url)
            if m:
                ws_id = m.group(1)
                if ws_id not in workshop_ids:
                    workshop_ids.append(ws_id)

        if not workshop_ids:
            _log("未从URL中提取到有效的专题班ID", "red")
            return

        _log(f"共 {len(workshop_ids)} 个专题班待学习", "blue")

        # 采集每个专题班的课程
        all_tasks = []
        ws_locks = {}
        for ws_id in workshop_ids:
            ws_url = f"https://u.ccb.com/workshop/#/myworkshop/detail?id={ws_id}"
            _log(f"正在采集: {ws_id[:16]}...", "blue")

            # 导航（先回列表页重置SPA，再导航到目标）
            list_url = "https://u.ccb.com/workshop/#/index?collegeId=&departmentId=&orderby=praise"
            for nav_url in [ws_url, ws_url.replace("/myworkshop/detail", "/detail")]:
                try:
                    await page.goto(list_url, wait_until="domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(2000)
                    await page.evaluate(f"window.location.hash = '{nav_url.split('#')[1]}';")
                    await page.wait_for_timeout(5000)
                except:
                    pass
                body = ""
                try:
                    body = await page.locator("body").inner_text(timeout=3000)
                except:
                    pass
                if "创建日期" in body or "报名" in body:
                    break

            if "报名截止" in body:
                _log(f"  ⊘ 报名截止，跳过", "yellow")
                continue

            # 点击课程标签
            for tab_text in ["课程", "课程列表", "课程目录"]:
                try:
                    tab = page.locator(f"text={tab_text}").first
                    if await tab.count() > 0 and await tab.is_visible():
                        await tab.click()
                        await page.wait_for_timeout(3000)
                        break
                except:
                    pass

            # 等待数据加载
            for _ in range(4):
                rows = await page.locator("tr.text-center").count()
                if rows > 0:
                    break
                await page.wait_for_timeout(3000)

            # 提取课程
            courses = await self.get_courses_from_workshop(page)
            if not courses:
                _log(f"  ✗ 未获取到课程", "yellow")
                continue

            to_learn = [(i, c) for i, c in enumerate(courses) if self._is_learnable(c.get('action', ''), c.get('hours', ''))]
            ws_title = body[:50].split("\n")[0].strip() if body else ws_id[:16]

            if not to_learn:
                _log(f"  ✓ 全部已完成（{len(courses)}门）", "green")
                continue

            _log(f"  ✓ {len(to_learn)} 门待学（共{len(courses)}门）", "green")
            ws_locks[ws_id] = asyncio.Lock()
            for ci, c in to_learn:
                all_tasks.append((ws_id, ci, c, ws_title))

        if not all_tasks:
            _log("没有需要学习的课程", "yellow")
            return

        # 开始学习
        _log(f"\n开始学习 {len(all_tasks)} 门课程", "bold blue")
        await self.parallel_learn_courses(
            all_tasks, ws_locks, None, _progress, _hours, _log
        )

    async def get_available_tags(self, page: Page) -> Dict[str, List[str]]:
        # 从页面提取所有可见标签，按分类分组
        try:
            tags_dict = await page.evaluate('''() => {
                const result = {};
                const cats = document.querySelectorAll('ul.tag-tree-list > li');
                cats.forEach(cat => {
                    const titleEl = cat.querySelector('.portal-title');
                    if (!titleEl) return;
                    const category = titleEl.innerText.trim();
                    if (!category) return;
                    const tags = [];
                    const items = cat.querySelectorAll('li.tag-second span.single-tag');
                    items.forEach(span => {
                        const text = span.innerText.trim();
                        if (text) tags.push(text);
                    });
                    if (tags.length > 0) result[category] = tags;
                });
                return result;
            }''')
            return tags_dict
        except Exception as e:
            console.print(f"获取标签列表失败: {e}", style="yellow")
            return {}

            console.print(f"获取标签列表失败: {e}", style="yellow")
            return {}

    async def interactive_tag_selection(self, page: Page) -> List[str]:
        # 等待标签树加载
        try:
            await page.wait_for_selector("ul.tag-tree-list", timeout=15000)
            await page.wait_for_timeout(3000)
        except:
            console.print("标签树未加载，尝试从页面文本提取标签...", style="yellow")
            # 尝试从文本提取（兜底）
            _txt = await page.locator("body").inner_text()
            _cats = {}
            _current_cat = ""
            for _ln in _txt.split("\n"):
                _ln = _ln.strip()
                if _ln in ("岗位标签", "党性教育", "研修院", "平台", "学科"):
                    _current_cat = _ln
                    _cats[_current_cat] = []
                elif _current_cat and _ln and len(_ln) < 30 and _ln != "不限":
                    _cats[_current_cat].append(_ln)
            if any(v for v in _cats.values()):
                console.print("从文本提取成功", style="green")
                tags_by_category = _cats
            else:
                console.print("无法获取标签", style="yellow")
                return []
        

        # 如果文本兜底已提取到标签，跳过DOM查询
        if 'tags_by_category' not in dir() or not tags_by_category:
            tags_by_category = await self.get_available_tags(page)
        if not tags_by_category:
            console.print("未获取到可用标签", style="yellow")
            return []
        
        all_tags = []
        idx = 1
        
        console.print()
        console.print("[bold]可用的标签分类:[/bold]", style="blue")
        
        for category, tags in tags_by_category.items():
            for tag in tags:
                console.print(f"  [{idx:3d}] {category} → {tag}", style="white")
                all_tags.append(tag)
                idx += 1
        
        console.print()
        console.print("请输入要筛选的标签编号（多个用逗号分隔，直接回车跳过）: ", style="yellow", end="")
        choice = await async_input("输入编号（逗号分隔，直接回车跳过）", default="", timeout=30)
        if not choice:
            console.print("跳过标签筛选", style="yellow")
            return []
        
        selected_indices = []
        for part in choice.split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= len(all_tags):
                selected_indices.append(int(part) - 1)
        
        selected_tags = [all_tags[i] for i in selected_indices]
        if selected_tags:
            self.tags_to_learn = selected_tags
            console.print(f"已选择标签: {', '.join(selected_tags)}", style="green")
        return selected_tags
    async def start_learning(self, workshops: List[Dict]):
        """(已废弃) 改用直接流程"""
        if not workshops:
            console.print("未找到专题班", style="yellow")

    async def get_study_hours(self) -> float:
        page = self.pages[0]
        try:
            await page.goto("https://u.ccb.com/portal/#/studyCenter")
            await asyncio.sleep(5)
            
            body_text = await page.locator("body").inner_text()
            console.print("学习中心页面内容预览: " + body_text[:300], style="blue")
            
        except Exception as e:
            console.print("获取学时失败", style="red")
        
        return 0.0


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """建行学习自动学习工具"""
    if ctx.invoked_subcommand is None:
        ctx.invoke(start)


@cli.command()
@click.option("--headless", is_flag=True, help="隐藏浏览器界面")
@click.option("--workers", default=1, help="同时学习的页面数量")
@click.option("--target-hours", default=0.0, help="目标学习学时，0表示不限制")
@click.option("--tags", multiple=True, help="要学习的标签，例如：党的创新理论教育 党性教育")
def start(headless, workers, target_hours, tags):
    """开始自动学习"""
    async def run():
        # 运行时询问worker数量和headless配置
        _w, _h = workers, headless
        if workers == 1 and not headless:  # 用户没有用参数，就询问
            _saved = {}
            if os.path.exists(CONFIG_PATH):
                try:
                    with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
                        _saved = json.load(_f)
                except:
                    pass
            if _saved.get("workers") is not None or _saved.get("headless") is not None:
                _sw = _saved.get("workers", 1)
                _sh = _saved.get("headless", False)
                console.print(f"发现上次配置: 工作线程={_sw}, 无头模式={'是' if _sh else '否'}", style="green")
                _use = await async_input("使用上次配置？(y/n)", default="y", timeout=5)
                if _use in ('y', 'yes', ''):
                    _w, _h = _sw, _sh
                else:
                    print()
                    _wi = await async_input("工作线程数量 (默认1): ", default="1", timeout=10)
                    if _wi.isdigit() and int(_wi) > 0:
                        _w = int(_wi)
                    _hi = await async_input("无头模式 (浏览器不显示界面)？(y/n，默认n)", default="n", timeout=5)
                    _h = _hi in ('y', 'yes')
                    # 保存配置
                    try:
                        with open(CONFIG_PATH, "w", encoding="utf-8") as _f:
                            json.dump({"workers": _w, "headless": _h}, _f, ensure_ascii=False, indent=2)
                        console.print("配置已保存", style="green")
                    except:
                        pass
            else:
                print()
                _wi = await async_input("工作线程数量 (默认1): ", default="1", timeout=10)
                if _wi.isdigit() and int(_wi) > 0:
                    _w = int(_wi)
                _hi = await async_input("无头模式 (浏览器不显示界面)？(y/n，默认n)", default="n", timeout=5)
                _h = _hi in ('y', 'yes')
                try:
                    with open(CONFIG_PATH, "w", encoding="utf-8") as _f:
                        json.dump({"workers": _w, "headless": _h}, _f, ensure_ascii=False, indent=2)
                    console.print("配置已保存", style="green")
                except:
                    pass
        
        # 显示运行配置
        from rich.panel import Panel
        config_table = Table(show_header=False, box=None, padding=(0, 2))
        config_table.add_column("项", style="cyan")
        config_table.add_column("值", style="green")
        config_table.add_row("工作线程", str(_w))
        config_table.add_row("无头模式", "是" if _h else "否")
        console.print(Panel(config_table, title="[bold]运行配置[/bold]", border_style="blue"))

        learner = CCBULearner(headless=_h, workers=_w)
        learner.target_hours = target_hours
        learner.tags_to_learn = list(tags)

        try:
            await learner.init()
            await learner.login()

            # 学习目标设置
            try:
                _gc = {}
                if os.path.exists(CONFIG_PATH):
                    with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
                        _gc = json.load(_f)
                _saved_goal = _gc.get("study_goal", 0)
                _saved_type = _gc.get("goal_type", "central")
                if _saved_goal > 0:
                    _stn = "集中培训" if _saved_type == "central" else "网络自学"
                    console.print(f"发现保存的学习目标: {_stn} {_saved_goal} 学时", style="green")
                    _use = await async_input("使用？(y/n)", default="y", timeout=5)
                    if _use in ('y', 'yes', ''):
                        learner.study_goal = _saved_goal
                        learner.goal_type = _saved_type
                if learner.study_goal <= 0:
                    _gt = await async_input("目标类型: 集中培训(c) / 网络自学(w)？(默认c)", default="c", timeout=10)
                    learner.goal_type = "online" if _gt in ('w', '网络自学') else "central"
                    _gm = await async_input("目标模式: 总学时(t) / 还需学时(n)？(默认t)", default="t", timeout=10)
                    _gi = await async_input("输入学时数（0=不限制）", default="0", timeout=10)
                    if _gi.replace('.', '').isdigit() and float(_gi) > 0:
                        goal_val = float(_gi)
                        if _gm in ('n', '还需'):
                            _h_val = await learner._get_study_hours(learner.pages[0])
                            _cur = _h_val.get(learner.goal_type, 0)
                            goal_val = _cur + goal_val
                        learner.study_goal = goal_val
                        _gc["study_goal"] = learner.study_goal
                        _gc["goal_type"] = learner.goal_type
                        with open(CONFIG_PATH, "w", encoding="utf-8") as _f:
                            json.dump(_gc, _f, ensure_ascii=False, indent=2)

                # 显示学习目标面板
                if learner.study_goal > 0:
                    _h_val = await learner._get_study_hours(learner.pages[0])
                    _tn = "集中培训" if learner.goal_type == "central" else "网络自学"
                    _cur = _h_val.get(learner.goal_type, 0)
                    _pct = min(100, _cur / learner.study_goal * 100) if learner.study_goal > 0 else 0
                    _bar = "█" * int(_pct // 5) + "░" * (20 - int(_pct // 5))

                    goal_table = Table(show_header=False, box=None, padding=(0, 2))
                    goal_table.add_column("项", style="cyan")
                    goal_table.add_column("值", style="green")
                    goal_table.add_row("目标类型", _tn)
                    goal_table.add_row("当前学时", f"{_cur:.1f}")
                    goal_table.add_row("目标学时", f"{learner.study_goal:.1f}")
                    goal_table.add_row("完成进度", f"{_bar} {_pct:.1f}%")
                    console.print(Panel(goal_table, title="[bold]学习目标[/bold]", border_style="green"))

                    if _cur >= learner.study_goal and _cur > 0:
                        console.print(f"[bold green]✓ 已达到目标，无需学习！[/bold green]")
                        await async_input("按回车键退出", default="", timeout=600, block=True)
                        return

            except Exception as _ge:
                debug(f"学习目标异常: {_ge}")
            

            # 选择学习模式（有目标时自动选择）
            if learner.study_goal > 0:
                _tn = "集中培训" if learner.goal_type == "central" else "网络自学"
                _mode = "1" if learner.goal_type == "central" else "2"
                console.print(f"目标类型「{_tn}」→ 自动选择{'专题班' if _mode == '1' else '课程'}模式", style="blue")
            else:
                _mode = await async_input("选择模式: 专题班(1) / 课程列表(2)？(默认1)", default="1", timeout=5)
            if _mode == "2":
                await learner._course_mode(learner.pages[0])
                await async_input("\n课程模式完成! 按回车键关闭浏览器", default="", timeout=600, block=True)
                return
            
            # 访问专题班页面
            page = learner.pages[0]
            await page.goto("https://u.ccb.com/workshop/#/index?collegeId=&departmentId=&orderby=praise")
            await asyncio.sleep(5)
            
            # 根据标签筛选
            if tags:
                learner.tags_to_learn = list(tags)
                await learner.filter_by_tags(page)
                await asyncio.sleep(3)
                try:
                    with open(TAGS_STATE_PATH, "w", encoding="utf-8") as _f:
                        json.dump({"tags": list(tags), "source": "cli"}, _f)
                except:
                    pass
            else:
                saved_tags = None
                if os.path.exists(TAGS_STATE_PATH):
                    try:
                        with open(TAGS_STATE_PATH, "r", encoding="utf-8") as _f:
                            saved_tags = json.load(_f).get("tags", [])
                    except:
                        pass

                use_tags = []
                if saved_tags:
                    console.print(f"已保存标签: [cyan]{', '.join(saved_tags)}[/cyan]")
                    _c = await async_input("使用(u) / 重新选择(r) / 跳过(s)？(默认u)", default="u", timeout=5)
                    if _c in ('', 'u', 'use'):
                        use_tags = saved_tags
                    elif _c in ('r', 're', '重新'):
                        use_tags = await learner.interactive_tag_selection(page)
                elif not tags:
                    _c = await async_input("是否筛选标签？(y/n，默认n)", default="n", timeout=5)
                    if _c in ('y', 'yes'):
                        use_tags = await learner.interactive_tag_selection(page)

                if use_tags:
                    learner.tags_to_learn = use_tags
                    await learner.filter_by_tags(page)
                    await asyncio.sleep(3)
                    try:
                        with open(TAGS_STATE_PATH, "w", encoding="utf-8") as _f:
                            json.dump({"tags": use_tags}, _f, ensure_ascii=False, indent=2)
                    except:
                        pass

            # 加载学习进度
            progress = learner.load_progress()
            completed_ids = set(progress.get("completed_ws_ids", []))
            if completed_ids:
                console.print(f"已有进度: [green]{len(completed_ids)}[/green] 个专题班已完成")
                _rp = await async_input("继续(回车) / 重新开始(r)？", default="", timeout=5)
                if _rp in ('r', 're', '重新'):
                    completed_ids = set()
                    learner.save_progress(set())

            # ===== 按需翻页 + 逐页采集 + 学习 =====
            page_num = 1
            no_more_pages = False  # 标记是否已无更多页
            tasks = []
            ws_locks = {}

            # 采集课程，至少凑够 worker 数量再开始学（除非已无更多页）
            while len(tasks) < learner.workers and not no_more_pages:
                current_workshops = await learner.get_workshops(page)
                if not current_workshops:
                    no_more_pages = True
                    break

                console.print(f"\n{'='*50}", style="bold blue")
                console.print(f"第 {page_num} 页: {len(current_workshops)} 个专题班", style="bold blue")
                console.print(f"{'='*50}", style="bold blue")
                await learner.display_workshops(current_workshops)

                new_tasks, new_locks = await learner._collect_workshops_courses(
                    page, current_workshops, completed_ids)
                learner.save_progress(completed_ids, page_num, 0)
                tasks.extend(new_tasks)
                ws_locks.update(new_locks)

                if len(tasks) >= learner.workers:
                    break

                # 不够，翻页继续采
                moved = await learner.go_to_next_page(page)
                if not moved:
                    no_more_pages = True
                else:
                    page_num += 1
                    await page.wait_for_timeout(3000)

            if tasks:
                # 定义回调：worker队列空时自动翻页采集更多课程
                _fetch_lock = asyncio.Lock()
                _page_ref = [page]  # 用列表包装以便闭包修改

                async def fetch_more_courses(queue):
                    nonlocal no_more_pages, page_num
                    if no_more_pages:
                        return 0
                    async with _fetch_lock:
                        # 再次检查（可能其他worker已经采了）
                        if no_more_pages:
                            return 0
                        # 检查目标学时
                        if learner.study_goal > 0:
                            try:
                                _h = await learner._get_study_hours(_page_ref[0])
                                _cur = _h.get(learner.goal_type, 0)
                                if _cur >= learner.study_goal:
                                    console.print(f"\n已达到学习目标! 停止采集", style="bold green")
                                    no_more_pages = True
                                    return 0
                            except:
                                pass
                        # 翻页
                        moved = await learner.go_to_next_page(_page_ref[0])
                        if not moved:
                            console.print("\n已无更多页", style="yellow")
                            no_more_pages = True
                            return 0
                        page_num += 1
                        await _page_ref[0].wait_for_timeout(3000)
                        # 采集新页
                        new_ws = await learner.get_workshops(_page_ref[0])
                        if not new_ws:
                            no_more_pages = True
                            return 0
                        console.print(f"\n自动翻到第 {page_num} 页: {len(new_ws)} 个专题班", style="bold blue")
                        new_tasks, new_locks = await learner._collect_workshops_courses(
                            _page_ref[0], new_ws, completed_ids)
                        learner.save_progress(completed_ids, page_num, 0)
                        # 合并锁
                        ws_locks.update(new_locks)
                        # 加入队列
                        for t in new_tasks:
                            queue.put_nowait((*t, 0))
                        if new_tasks:
                            console.print(f"新增 {len(new_tasks)} 门课程", style="green")
                        return len(new_tasks)

                console.print(f"\n{'='*50}", style="bold blue")
                console.print(f"开始学习（{len(tasks)} 门课程, {learner.workers} 个线程）", style="bold blue")
                console.print(f"{'='*50}", style="bold blue")
                await learner.parallel_learn_courses(tasks, ws_locks, fetch_more_courses)
                # 学完后重新加载已完成列表
                progress = learner.load_progress()
                completed_ids = set(progress.get("completed_ws_ids", []))
            else:
                console.print("没有需要学习的课程", style="yellow")

            console.print("\n✓ 学习流程完成! 浏览器将保持打开", style="bold green")
            await async_input("按回车键关闭浏览器", default="", timeout=600, block=True)
            
        finally:
            await learner.close()
    
    asyncio.run(run())


@cli.command()
def hours():
    """查看当前学时"""
    async def run():
        learner = CCBULearner(headless=False)
        try:
            await learner.init()
            await learner.login()
            hours = await learner.get_study_hours()
            await async_input("\n按回车键关闭浏览器", default="", timeout=600, block=True)
        finally:
            await learner.close()
    
    asyncio.run(run())


@cli.command()
def clear():
    """清除所有保存的会话、凭证和标签筛选"""
    removed = []
    for _p in [STORAGE_STATE_PATH, USER_CREDENTIALS_PATH, TAGS_STATE_PATH, CONFIG_PATH, PROGRESS_PATH]:
        if os.path.exists(_p):
            try:
                os.remove(_p)
                removed.append(_p)
            except:
                pass
    if removed:
        for _r in removed:
            console.print(f"已删除: {_r}", style="green")
        console.print("✓ 清除完成", style="bold green")
    else:
        console.print("没有需要清除的文件", style="yellow")


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    cli()
