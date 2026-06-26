#!/usr/bin/env python3
"""调试课程采集：逐个测试专题班，输出详细HTML结构"""
import asyncio
import sys
import os
from playwright.async_api import async_playwright

# 要测试的专题班ID（从debug日志中提取的失败案例）
TEST_IDS = [
    "459f805d-7dcc-4cbb-af89-a94179a096d8",
    "25ea69db-93dc-48e1-9bbe-cfb0e405389d",
    "98635074-44d4-46f5-a2ac-058c854234a9",
    "17a0f167-1c21-49c5-aa62-068267f90aca",
    "458e8eeb-0f56-412e-989f-5301be134b21",
]


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context(
            storage_state="ccbu_session.json" if os.path.exists("ccbu_session.json") else None,
            viewport={"width": 1920, "height": 1080},
        )
        page = await ctx.new_page()

        # 检查登录状态
        await page.goto("https://u.ccb.com/portal/#/study", wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(3000)
        if "/sys/#/login" in page.url or "密码登录" in (await page.locator("body").inner_text(timeout=3000))[:500]:
            print("未登录，请在浏览器中手动登录后按回车...")
            input()
            # 保存会话供下次使用
            await ctx.storage_state(path="ccbu_session.json")
            print("会话已保存")
        else:
            print("已登录")

        for ws_id in TEST_IDS:
            url = f"https://u.ccb.com/workshop/#/myworkshop/detail?id={ws_id}"
            print(f"\n{'='*60}")
            print(f"测试: {ws_id}")
            print(f"URL: {url}")
            print(f"{'='*60}")

            # 1. 导航
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(5000)
            except Exception as e:
                print(f"  导航失败: {e}")
                continue

            # 2. 页面基本信息
            print(f"  当前URL: {page.url}")
            body_text = await page.locator("body").inner_text(timeout=5000)
            print(f"  页面文本长度: {len(body_text)}")
            print(f"  页面文本前200字: {body_text[:200]}")

            # 3. 检查常见元素
            checks = [
                ("tr.text-center", "课程表格行"),
                (".course-type", "课程类型"),
                (".edit-block", "操作按钮"),
                (".percent-text", "进度百分比"),
                ("table", "任意表格"),
                ("ul.tag-tree-list", "标签树"),
                (".workshop-content-list", "专题班内容列表"),
                ("text=课程", "课程标签页"),
                ("text=讨论", "讨论标签页"),
                ("text=立即报名", "报名按钮"),
                ("text=继续学习", "继续学习按钮"),
                ("text=立即学习", "立即学习按钮"),
                ("text=我要学习", "我要学习按钮"),
            ]
            for selector, name in checks:
                try:
                    count = await page.locator(selector).count()
                    visible = False
                    if count > 0:
                        try:
                            visible = await page.locator(selector).first.is_visible(timeout=1000)
                        except:
                            pass
                    print(f"  {name}: {count}个 (可见: {visible})")
                except:
                    print(f"  {name}: 查询失败")

            # 4. 尝试点击"课程"标签
            for tab_text in ["课程", "课程列表", "课程目录"]:
                try:
                    tab = page.locator(f"text={tab_text}").first
                    if await tab.count() > 0 and await tab.is_visible(timeout=1000):
                        print(f"  点击「{tab_text}」标签...")
                        await tab.click()
                        await page.wait_for_timeout(3000)
                        break
                except:
                    pass

            # 5. 再次检查课程表格
            row_count = await page.locator("tr.text-center").count()
            print(f"  点击标签后表格行数: {row_count}")

            # 6. 如果还是0，dump页面HTML结构
            if row_count == 0:
                print("  === 页面HTML片段（table相关）===")
                html = await page.evaluate("""() => {
                    // 找所有table
                    const tables = document.querySelectorAll('table');
                    if (tables.length > 0) {
                        return Array.from(tables).map(t => t.outerHTML.substring(0, 500)).join('\\n---\\n');
                    }
                    // 找main内容区
                    const main = document.querySelector('.workshop-content-list, .main-content, #app, .app-main');
                    if (main) return main.innerHTML.substring(0, 2000);
                    // 兜底：body
                    return document.body.innerHTML.substring(0, 2000);
                }""")
                print(html[:3000])

            await page.wait_for_timeout(1000)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
