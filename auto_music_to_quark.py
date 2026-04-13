#!/usr/bin/env python3
"""
音乐歌单自动转存到夸克网盘

支持平台：
  1. QQ音乐
  2. 网易云音乐

流程：
  1. 选择音乐平台并输入歌单ID
  2. 获取歌单名称和歌曲列表
  3. 搜索每首歌的夸克网盘链接（实时保存到txt文件）
  4. 在夸克网盘指定目录下创建歌单文件夹
  5. 批量转存所有网盘链接到新文件夹
"""

import asyncio
import csv
import random
import sys
from pathlib import Path

import requests
from playwright.async_api import async_playwright

# 导入现有模块的函数和类
from download_music_links import (
    get_songs_from_qq_playlist,
    search_and_get_link,
    sanitize_filename,
    REQUEST_DELAY,
)
from quark import QuarkPanFileManager


# ============ 配置区域 ============

# 父目录ID（在此目录下创建歌单文件夹）
PARENT_DIR_ID = "735eab3ae73849d8b3032fbcd07b1879"

# 转存间隔（秒），避免请求过快
SAVE_DELAY_MIN = 1.0
SAVE_DELAY_MAX = 2.0


# ============ 网易云音乐歌单获取 ============

def get_songs_from_netease_playlist(playlist_id: str) -> tuple[str, list[str]]:
    """
    从网易云音乐歌单获取歌单名称和歌曲列表（支持获取完整歌曲列表）

    Args:
        playlist_id: 网易云音乐歌单ID

    Returns:
        (歌单名称, 歌曲列表)，歌曲格式：["歌手 - 歌名", ...]
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    # 1. 先获取歌单名称 + 全部歌曲ID
    url_detail = f"https://music.163.com/api/v1/playlist/detail?id={playlist_id}"
    print(f"正在访问网易云音乐歌单: {url_detail}")
    res = requests.get(url_detail, headers=headers)
    data = res.json()

    if "playlist" not in data:
        print(f"获取歌单失败: {data.get('msg', '未知错误')}")
        return "", []

    playlist_name = data["playlist"]["name"]
    track_ids = [str(t["id"]) for t in data["playlist"]["trackIds"]]

    # 2. 分批获取所有歌曲详情（每批最多200首，避免请求过大）
    songs = []
    batch_size = 200
    for start in range(0, len(track_ids), batch_size):
        batch_ids = track_ids[start:start + batch_size]
        c_param = [{"id": tid} for tid in batch_ids]
        res_songs = requests.post(
            "https://music.163.com/api/v3/song/detail",
            headers=headers,
            data={"c": str(c_param).replace("'", '"')},
        )
        songs_data = res_songs.json()

        for s in songs_data["songs"]:
            song_name = s["name"]
            artist = "/".join([a["name"] for a in s["ar"]])
            songs.append(f"{artist} - {song_name}")

    print(f"歌单名称: {playlist_name}")
    print(f"获取到 {len(songs)} 首歌曲")
    return playlist_name, songs


# ============ 平台选择 ============

def select_platform() -> tuple[str, str]:
    """
    交互式选择音乐平台并输入歌单ID

    Returns:
        (平台标识, 歌单ID)
    """
    print("\n" + "=" * 50)
    print("  请选择音乐平台")
    print("=" * 50)
    print("  1. QQ音乐")
    print("  2. 网易云音乐")
    print("=" * 50)

    while True:
        choice = input("\n请输入选项 (1/2): ").strip()
        if choice in ("1", "2"):
            break
        print("无效输入，请输入 1 或 2")

    platform = "qq" if choice == "1" else "netease"
    platform_name = "QQ音乐" if platform == "qq" else "网易云音乐"

    playlist_id = input(f"\n请输入{platform_name}歌单ID: ").strip()
    if not playlist_id:
        print("歌单ID不能为空")
        sys.exit(1)

    return platform, playlist_id


# ============ 核心流程 ============

def append_link_to_file(link: str, output_file: str) -> None:
    """追加写入链接到文件"""
    with open(output_file, "a", encoding="utf-8") as f:
        f.write(f"{link}\n")


def load_links_from_file(filepath: str) -> list[str]:
    """从文件读取链接列表"""
    if not Path(filepath).exists():
        return []

    with open(filepath, "r", encoding="utf-8") as f:
        links = [line.strip() for line in f if line.strip()]
    return links


async def step1_get_playlist_and_search_links(
    platform: str, playlist_id: str
) -> tuple[str, str, list[dict]]:
    """
    步骤1&2: 获取歌单信息并搜索网盘链接

    Args:
        platform: 平台标识 ("qq" 或 "netease")
        playlist_id: 歌单ID

    Returns:
        (歌单名称, 输出文件路径, 结果列表)
    """
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # 步骤1: 获取歌单信息
        platform_name = "QQ音乐" if platform == "qq" else "网易云音乐"
        print("=" * 60)
        print(f"步骤1: 获取{platform_name}歌单信息")
        print("=" * 60)

        if platform == "qq":
            title, songs = await get_songs_from_qq_playlist(page, playlist_id)
        else:
            # 网易云音乐使用 requests，不需要 page
            title, songs = get_songs_from_netease_playlist(playlist_id)

        if not title:
            title = f"歌单_{playlist_id}"
            print(f"未获取到歌单名称，使用默认名称: {title}")

        safe_title = sanitize_filename(title)
        output_file = f"{safe_title}.txt"
        csv_file = f"{safe_title}.csv"

        if not songs:
            print("未获取到歌曲，退出")
            await browser.close()
            return title, output_file, []

        # 打印歌曲列表
        print(f"\n歌单: {title}")
        print(f"共 {len(songs)} 首歌曲")
        print("-" * 60)
        for i, song in enumerate(songs, 1):
            print(f"  {i:3d}. {song}")
        print("-" * 60)

        # 清空输出文件（新开始）
        with open(output_file, "w", encoding="utf-8") as f:
            pass

        # 步骤2: 搜索网盘链接
        print("\n" + "=" * 60)
        print("步骤2: 搜索夸克网盘链接")
        print("=" * 60)

        for i, song in enumerate(songs, 1):
            print(f"[{i:3d}/{len(songs)}] {song} ... ", end="", flush=True)

            try:
                link = await search_and_get_link(page, song)

                if link:
                    print(f"✓ {link}")
                    results.append({"song": song, "link": link})
                    append_link_to_file(link, output_file)
                else:
                    print("✗ 未找到")
                    results.append({"song": song, "link": ""})
            except Exception as e:
                print(f"✗ 错误: {e}")
                results.append({"song": song, "link": ""})

            if i < len(songs):
                await asyncio.sleep(REQUEST_DELAY)

        await browser.close()

    # 保存CSV（包含歌曲名和链接）
    with open(csv_file, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["song", "link"])
        writer.writeheader()
        writer.writerows(results)

    # 保存未找到链接的歌曲
    not_found_songs = [r["song"] for r in results if not r["link"]]
    not_found_file = f"{safe_title}_not_found.txt"
    if not_found_songs:
        with open(not_found_file, "w", encoding="utf-8") as f:
            for song in not_found_songs:
                f.write(f"{song}\n")

    # 统计
    found_count = sum(1 for r in results if r["link"])
    print(f"\n链接搜索完成: {found_count}/{len(songs)} 首找到链接")
    print(f"链接已保存至: {output_file}")
    print(f"详细结果保存至: {csv_file}")
    if not_found_songs:
        print(f"未找到链接的歌曲({len(not_found_songs)}首)已保存至: {not_found_file}")

    return title, output_file, results


async def step3_save_to_quark(title: str, links_file: str) -> tuple[int, int]:
    """
    步骤3: 在夸克网盘创建文件夹并批量转存

    Returns:
        (成功数, 失败数)
    """
    print("\n" + "=" * 60)
    print("步骤3: 转存到夸克网盘")
    print("=" * 60)

    # 读取链接
    links = load_links_from_file(links_file)
    if not links:
        print("没有可转存的链接")
        return 0, 0

    print(f"待转存链接数: {len(links)}")

    # 初始化夸克网盘管理器
    print("\n初始化夸克网盘...")
    quark_manager = QuarkPanFileManager(headless=True, slow_mo=0)

    # 获取用户信息
    user = await quark_manager.get_user_info()
    print(f"当前用户: {user}")

    # 创建歌单文件夹
    safe_title = sanitize_filename(title)
    print(f"\n在目录 {PARENT_DIR_ID} 下创建文件夹: {safe_title}")

    new_folder_id = await quark_manager.create_dir(pdir_name=safe_title, pdir_fid=PARENT_DIR_ID)

    if not new_folder_id:
        print("文件夹创建失败，无法继续转存")
        return 0, len(links)

    print(f"文件夹创建成功，ID: {new_folder_id}")

    # 批量转存
    print(f"\n开始批量转存 {len(links)} 个链接...")
    print("-" * 60)

    success_count = 0
    fail_count = 0
    failed_links = []

    for i, link in enumerate(links, 1):
        print(f"\n[{i:3d}/{len(links)}] 转存: {link}")

        try:
            await quark_manager.run(link, folder_id=new_folder_id)
            success_count += 1
        except Exception as e:
            print(f"  ✗ 转存失败: {e}")
            fail_count += 1
            failed_links.append(link)

        # 添加随机延迟
        if i < len(links):
            delay = random.uniform(SAVE_DELAY_MIN, SAVE_DELAY_MAX)
            await asyncio.sleep(delay)

    print("-" * 60)

    # 保存失败的链接
    if failed_links:
        failed_file = f"{safe_title}_failed.txt"
        with open(failed_file, "w", encoding="utf-8") as f:
            for link in failed_links:
                f.write(f"{link}\n")
        print(f"\n失败的链接已保存至: {failed_file}")

    return success_count, fail_count


async def auto_pipeline(platform: str, playlist_id: str) -> None:
    """
    自动化流水线主函数
    """
    platform_name = "QQ音乐" if platform == "qq" else "网易云音乐"

    print("\n" + "╔" + "═" * 58 + "╗")
    print("║" + f" {platform_name}歌单自动转存到夸克网盘 ".center(46) + "║")
    print("╚" + "═" * 58 + "╝")
    print(f"\n音乐平台: {platform_name}")
    print(f"歌单ID: {playlist_id}")
    print(f"目标父目录ID: {PARENT_DIR_ID}")

    # 步骤1&2: 获取歌单并搜索链接
    title, output_file, results = await step1_get_playlist_and_search_links(platform, playlist_id)

    if not results or not any(r["link"] for r in results):
        print("\n没有获取到任何网盘链接，流程结束")
        return

    # 步骤3: 转存到夸克网盘
    success_count, fail_count = await step3_save_to_quark(title, output_file)

    # 最终统计
    print("\n" + "=" * 60)
    print("流程完成！统计结果")
    print("=" * 60)
    print(f"音乐平台: {platform_name}")
    print(f"歌单名称: {title}")
    print(f"歌曲总数: {len(results)}")
    print(f"找到链接: {sum(1 for r in results if r['link'])}")
    print(f"转存成功: {success_count}")
    print(f"转存失败: {fail_count}")
    print("=" * 60)


# ============ 入口 ============

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        # 命令行模式: python auto_music_to_quark.py <平台> <歌单ID>
        # 平台: qq / netease
        platform_arg = sys.argv[1].lower()
        if platform_arg not in ("qq", "netease"):
            print("平台参数错误，支持: qq, netease")
            sys.exit(1)
        playlist_id = sys.argv[2]
    elif len(sys.argv) == 2:
        # 兼容旧用法: 默认QQ音乐
        platform_arg = "qq"
        playlist_id = sys.argv[1]
    else:
        # 交互模式
        platform_arg, playlist_id = select_platform()

    asyncio.run(auto_pipeline(platform_arg, playlist_id))
