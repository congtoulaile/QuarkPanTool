#!/usr/bin/env python3
"""
从QQ音乐歌单获取歌曲列表，批量获取夸克网盘链接
"""

import asyncio
import csv
import re
import urllib.parse
from pathlib import Path
from playwright.async_api import async_playwright


# ============ 配置区域 ============

# 输出文件路径
OUTPUT_FILE = "quark_links.txt"

# 请求间隔（秒），避免请求过快
REQUEST_DELAY = 2


# ============ QQ音乐歌单获取 ============

async def get_song_list_title(page) -> str:
    """
    获取QQ音乐歌单名称
    
    Args:
        page: Playwright page 对象
    
    Returns:
        歌单名称字符串
    """
    title = await page.evaluate(
        "() => document.querySelector('.mod_data__name_txt')?.innerHTML?.trim() || ''"
    )
    return title


async def get_songs_from_qq_playlist(page, playlist_id: str) -> tuple[str, list[str]]:
    """
    从QQ音乐歌单获取歌单名称和歌曲列表
    
    Args:
        page: Playwright page 对象
        playlist_id: QQ音乐歌单ID
    
    Returns:
        (歌单名称, 歌曲列表)，歌曲格式：["歌手 - 歌名", ...]
    """
    url = f"https://y.qq.com/musicmac/v6/playlist/detail.html?id={playlist_id}"
    print(f"正在访问歌单: {url}")
    
    await page.goto(url, wait_until="networkidle")
    await asyncio.sleep(2)
    
    # 获取歌单名称
    title = await get_song_list_title(page)
    if title:
        print(f"歌单名称: {title}")
    else:
        print("未获取到歌单名称，将使用默认文件名")
    
    # 执行JS获取歌曲列表
    js_code = """
    () => {
        const songList = Array.from(document.querySelectorAll(".songlist__item"));
        return songList.map((v, i) => {
            const singer = v.querySelector(".singer_name")?.innerText || "";
            const songName = v.querySelector(".mod_songname__name")?.innerText || "";
            return singer + " - " + songName;
        });
    }
    """
    
    songs = await page.evaluate(js_code)
    
    # 过滤空值
    songs = [s.strip() for s in songs if s.strip() and s.strip() != " - "]
    
    print(f"获取到 {len(songs)} 首歌曲")
    return title, songs


# ============ 夸克网盘链接获取 ============

async def _search_jywav(page, song_name: str, encoded_keyword: str) -> str | None:
    """
    在 jywav.com 搜索歌曲并获取夸克网盘链接
    
    Args:
        page: Playwright page 对象
        song_name: 歌曲名称
        encoded_keyword: URL编码后的关键词
    
    Returns:
        夸克网盘链接，失败返回 None
    """
    search_url = f"https://www.jywav.com/search?page=0&keyword={encoded_keyword}"
    await page.goto(search_url, wait_until="networkidle")
    await asyncio.sleep(1)
    
    # 查找第一个搜索结果
    result_links = page.locator("a[href*='music/info.html']")
    count = await result_links.count()
    
    if count == 0:
        return None
    
    # 获取第一个结果的链接
    first_result = result_links.first
    result_url = await first_result.get_attribute("href")
    
    if not result_url:
        return None
    
    # 处理相对路径
    if result_url.startswith("/"):
        result_url = "https://www.jywav.com" + result_url
    
    # 访问详情页
    await page.goto(result_url, wait_until="networkidle")
    await asyncio.sleep(1)
    
    # 从页面内容中提取夸克网盘链接
    page_content = await page.content()
    match = re.search(r'https://pan\.quark\.cn/s/[a-zA-Z0-9]+', page_content)
    
    if match:
        return match.group(0)
    
    # 尝试点击"免费歌曲下载"按钮
    download_btn = page.locator("button:has-text('免费歌曲下载'), a:has-text('免费歌曲下载')")
    if await download_btn.count() > 0:
        await download_btn.first.click()
        await asyncio.sleep(2)
        
        page_content = await page.content()
        match = re.search(r'https://pan\.quark\.cn/s/[a-zA-Z0-9]+', page_content)
        
        if match:
            return match.group(0)
    
    return None


async def _search_yyfang(page, song_name: str, encoded_keyword: str) -> str | None:
    """
    在 yyfang.top（备份站）搜索歌曲并获取夸克网盘链接
    
    Args:
        page: Playwright page 对象
        song_name: 歌曲名称
        encoded_keyword: URL编码后的关键词
    
    Returns:
        夸克网盘链接，失败返回 None
    """
    search_url = f"https://yyfang.top/search?page=0&keyword={encoded_keyword}"
    await page.goto(search_url, wait_until="networkidle")
    await asyncio.sleep(1)
    
    # 查找第一个搜索结果
    result_links = page.locator("a[href*='music/info.html']")
    count = await result_links.count()
    
    if count == 0:
        return None
    
    # 获取第一个结果的链接
    first_result = result_links.first
    result_url = await first_result.get_attribute("href")
    
    if not result_url:
        return None
    
    # 处理相对路径
    if result_url.startswith("/"):
        result_url = "https://yyfang.top" + result_url
    
    # 访问详情页
    await page.goto(result_url, wait_until="networkidle")
    await asyncio.sleep(1)
    
    # 从页面内容中提取夸克网盘链接
    page_content = await page.content()
    match = re.search(r'https://pan\.quark\.cn/s/[a-zA-Z0-9]+', page_content)
    
    if match:
        return match.group(0)
    
    # 尝试点击"免费歌曲下载"按钮
    download_btn = page.locator("button:has-text('免费歌曲下载'), a:has-text('免费歌曲下载')")
    if await download_btn.count() > 0:
        await download_btn.first.click()
        await asyncio.sleep(2)
        
        page_content = await page.content()
        match = re.search(r'https://pan\.quark\.cn/s/[a-zA-Z0-9]+', page_content)
        
        if match:
            return match.group(0)
    
    return None


async def search_and_get_link(page, song_name: str) -> str | None:
    """
    搜索歌曲并获取夸克网盘链接
    优先使用 jywav.com，未找到时使用备份站 yyfang.top
    
    Args:
        page: Playwright page 对象
        song_name: 歌曲名称（格式：歌手 - 歌名）
    
    Returns:
        夸克网盘链接，失败返回 None
    """
    try:
        encoded_keyword = urllib.parse.quote(song_name)
        
        # 1. 优先在 jywav.com 搜索
        link = await _search_jywav(page, song_name, encoded_keyword)
        if link:
            return link
        
        # 2. jywav 未找到，尝试备份站 yyfang.top
        print("jywav未找到，尝试备份站yyfang... ", end="", flush=True)
        link = await _search_yyfang(page, song_name, encoded_keyword)
        if link:
            return link
        
        return None
            
    except Exception as e:
        print(f"  ✗ 处理失败: {song_name}, 错误: {e}")
        return None


def sanitize_filename(name: str) -> str:
    """
    清理文件名，去除非法字符
    """
    # 替换文件名中不允许的字符
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    # 去除首尾空白
    name = name.strip()
    # 限制长度
    if len(name) > 200:
        name = name[:200]
    return name


async def main(playlist_id: str, output_file: str | None = None):
    """
    主函数
    """
    results = []
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        
        # 1. 从QQ音乐获取歌曲列表
        print("=" * 50)
        print("步骤1: 获取QQ音乐歌单")
        print("=" * 50)
        title, songs = await get_songs_from_qq_playlist(page, playlist_id)
        
        # 根据歌单名称生成输出文件名
        if output_file is None:
            if title:
                safe_title = sanitize_filename(title)
                output_file = f"{safe_title}.txt"
            else:
                output_file = OUTPUT_FILE
        
        if not songs:
            print("未获取到歌曲，退出")
            await browser.close()
            return
        
        # 打印歌曲列表
        print("\n歌曲列表：")
        print("-" * 50)
        for i, song in enumerate(songs, 1):
            print(f"{i}. {song}")
        print("-" * 50)
        print(f"共 {len(songs)} 首\n")
        
        # 2. 获取夸克网盘链接
        print("=" * 50)
        print("步骤2: 获取夸克网盘链接")
        print("=" * 50)
        
        for i, song in enumerate(songs, 1):
            print(f"[{i}/{len(songs)}] {song} ... ", end="", flush=True)
            link = await search_and_get_link(page, song)
            
            if link:
                print(f"✓ {link}")
                results.append({"song": song, "link": link})
            else:
                print("✗ 未找到")
            
            if i < len(songs):
                await asyncio.sleep(REQUEST_DELAY)
        
        await browser.close()
    
    # 3. 保存结果
    if results:
        save_results(results, output_file)
        print(f"\n完成！共获取 {len(results)} 个链接，已保存至 {output_file}")
    else:
        print("\n未获取到任何链接")


def save_results(results: list[dict], output_file: str):
    """
    保存结果到文件（只保存网盘链接）
    """
    output_path = Path(output_file)
    
    # 只保存链接（每行一个）
    with open(output_path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(f"{r['link']}\n")
    
    # 同时保存csv格式（包含歌曲名和链接）
    csv_path = output_path.with_suffix(".csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["song", "link"])
        writer.writeheader()
        writer.writerows(results)


# ============ 入口 ============

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("用法: python download_music_links.py <QQ音乐歌单ID> [输出文件]")
        print("示例: python download_music_links.py 123456789")
        sys.exit(1)
    
    playlist_id = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    
    asyncio.run(main(playlist_id, output_file))
