#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
音频标签修复工具
功能:
  1. 从文件名解析歌名和歌手（支持 "歌名-歌手" 格式）
  2. 通过 MusicBrainz API 在线查询专辑名和流派
  3. 将正确信息写入音频文件标签（支持 FLAC / MP3 / M4A / OGG）

依赖: pip install mutagen httpx

使用示例:
  python fix_audio_tags.py "Apologize-Timbaland&OneRepublic.flac"
  python fix_audio_tags.py -d /path/to/music              # 批量修复目录
  python fix_audio_tags.py -d /path/to/music -r           # 递归
  python fix_audio_tags.py song.flac --sep "-"             # 指定分隔符
  python fix_audio_tags.py song.flac --dry-run             # 预览不写入
  python fix_audio_tags.py song.flac --no-fetch            # 不查询网络
"""

import os
import sys
import re
import time
import argparse
from pathlib import Path

import httpx
from mutagen import File as MutagenFile
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.oggvorbis import OggVorbis
from mutagen.mp4 import MP4
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TCON, ID3NoHeaderError

# ──────────────────────────────────────────────
#  配置
# ──────────────────────────────────────────────
SUPPORTED_EXTENSIONS = {".mp3", ".flac", ".ogg", ".oga", ".m4a", ".opus"}

# MusicBrainz API（免费，无需 API Key，限速 1 请求/秒）
MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
USER_AGENT = "AudioTagFixer/1.0 (https://github.com/example/audio-tag-fixer)"

# 颜色
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


# ──────────────────────────────────────────────
#  从文件名解析歌名和歌手
# ──────────────────────────────────────────────
def parse_filename(filepath, separator="-"):
    """
    从文件名中解析歌名和歌手

    支持的格式:
      歌名-歌手.ext        → title="歌名", artist="歌手"
      歌手-歌名.ext        → (通过 --artist-first 切换)
      歌名.ext             → title="歌名", artist=None

    Args:
        filepath: 文件路径
        separator: 分隔符，默认 "-"

    Returns:
        (title, artist) 元组
    """
    stem = Path(filepath).stem.strip()

    # 按分隔符拆分（只拆第一个）
    parts = stem.split(separator, 1)

    if len(parts) == 2:
        title = parts[0].strip()
        artist = parts[1].strip()
        return title, artist
    else:
        return stem, None


def clean_artist_name(artist):
    """清理歌手名：将 & / , / ; 等分隔的多歌手标准化"""
    if not artist:
        return artist
    # 替换中文的 、 为英文的 ;
    artist = artist.replace("、", "; ")
    return artist


# ──────────────────────────────────────────────
#  MusicBrainz API 查询
# ──────────────────────────────────────────────
def search_musicbrainz(title, artist=None):
    """
    通过 MusicBrainz API 搜索歌曲，获取专辑名和流派

    Args:
        title:  歌曲名
        artist: 歌手名（可选）

    Returns:
        dict: {"album": str, "genre": str, "year": str} 或空 dict
    """
    # 构建搜索查询
    query_parts = [f'recording:"{title}"']
    if artist:
        # 处理多歌手：取第一个作为主要搜索词
        primary_artist = re.split(r"[&,;/、]", artist)[0].strip()
        query_parts.append(f'artist:"{primary_artist}"')

    query = " AND ".join(query_parts)

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"{MUSICBRAINZ_API}/recording",
                params={"query": query, "limit": 5, "fmt": "json"},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        recordings = data.get("recordings", [])
        if not recordings:
            return {}

        # 取匹配度最高的第一条结果
        rec = recordings[0]

        result = {}

        # 获取专辑名（从 releases 字段）
        releases = rec.get("releases", [])
        if releases:
            # 优先取非 single 类型的专辑
            album_release = None
            for rel in releases:
                rg = rel.get("release-group", {})
                primary_type = rg.get("primary-type", "")
                if primary_type == "Album":
                    album_release = rel
                    break
            if album_release is None:
                album_release = releases[0]

            result["album"] = album_release.get("title", "")

            # 年份
            date = album_release.get("date", "")
            if date:
                result["year"] = date[:4]  # 只取年份部分

        # 获取流派/标签（从 tags 字段）
        tags = rec.get("tags", [])
        if tags:
            # 按 count 排序，取最相关的
            sorted_tags = sorted(tags, key=lambda t: t.get("count", 0), reverse=True)
            # 取前 3 个标签
            genre_list = [t["name"] for t in sorted_tags[:3] if t.get("name")]
            if genre_list:
                result["genre"] = "; ".join(genre_list)

        # 如果 recording 级别没有 tags，尝试从 release-group 获取
        if "genre" not in result and releases:
            rg = releases[0].get("release-group", {})
            rg_id = rg.get("id")
            if rg_id:
                result.update(_fetch_release_group_genre(rg_id))

        return result

    except httpx.HTTPStatusError as e:
        print(f"  {RED}⚠ MusicBrainz API 请求失败: HTTP {e.response.status_code}{RESET}")
        return {}
    except httpx.RequestError as e:
        print(f"  {RED}⚠ 网络请求失败: {e}{RESET}")
        return {}
    except Exception as e:
        print(f"  {RED}⚠ 查询异常: {e}{RESET}")
        return {}


def _fetch_release_group_genre(release_group_id):
    """从 release-group 获取流派信息"""
    try:
        time.sleep(1.1)  # MusicBrainz 限速
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"{MUSICBRAINZ_API}/release-group/{release_group_id}",
                params={"inc": "genres tags", "fmt": "json"},
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        result = {}
        # 优先用 genres 字段
        genres = data.get("genres", [])
        if genres:
            sorted_genres = sorted(genres, key=lambda g: g.get("count", 0), reverse=True)
            result["genre"] = "; ".join(g["name"] for g in sorted_genres[:3])
        else:
            # 退而求其次用 tags
            tags = data.get("tags", [])
            if tags:
                sorted_tags = sorted(tags, key=lambda t: t.get("count", 0), reverse=True)
                result["genre"] = "; ".join(t["name"] for t in sorted_tags[:3])
        return result
    except Exception:
        return {}


# ──────────────────────────────────────────────
#  写入标签
# ──────────────────────────────────────────────
def write_tags(filepath, title=None, artist=None, album=None, genre=None, year=None):
    """
    将标签信息写入音频文件

    Args:
        filepath: 音频文件路径
        title:    标题
        artist:   艺术家
        album:    专辑
        genre:    流派
        year:     年份
    """
    audio = MutagenFile(str(filepath))
    if audio is None:
        raise ValueError(f"无法打开文件: {filepath}")

    ext = Path(filepath).suffix.lower()

    if isinstance(audio, MP3):
        _write_id3_tags(filepath, title, artist, album, genre, year)
    elif isinstance(audio, FLAC):
        _write_vorbis_tags(audio, title, artist, album, genre, year)
    elif isinstance(audio, OggVorbis):
        _write_vorbis_tags(audio, title, artist, album, genre, year)
    elif isinstance(audio, MP4):
        _write_mp4_tags(audio, title, artist, album, genre, year)
    else:
        raise ValueError(f"不支持写入该格式的标签: {ext}")


def _write_id3_tags(filepath, title, artist, album, genre, year):
    """写入 MP3 ID3 标签"""
    try:
        id3 = ID3(str(filepath))
    except ID3NoHeaderError:
        # 文件没有 ID3 头，创建一个
        from mutagen.id3 import ID3 as ID3Class
        id3 = ID3Class()

    if title:
        id3["TIT2"] = TIT2(encoding=3, text=title)
    if artist:
        id3["TPE1"] = TPE1(encoding=3, text=artist)
    if album:
        id3["TALB"] = TALB(encoding=3, text=album)
    if genre:
        id3["TCON"] = TCON(encoding=3, text=genre)
    if year:
        from mutagen.id3 import TDRC
        id3["TDRC"] = TDRC(encoding=3, text=year)

    id3.save(str(filepath))


def _write_vorbis_tags(audio, title, artist, album, genre, year):
    """写入 FLAC / OGG Vorbis Comment 标签"""
    if audio.tags is None:
        audio.add_tags()

    if title:
        audio.tags["title"] = title
    if artist:
        audio.tags["artist"] = artist
    if album:
        audio.tags["album"] = album
    if genre:
        audio.tags["genre"] = genre
    if year:
        audio.tags["date"] = year

    audio.save()


def _write_mp4_tags(audio, title, artist, album, genre, year):
    """写入 M4A / MP4 标签"""
    if audio.tags is None:
        audio.add_tags()

    if title:
        audio.tags["\xa9nam"] = [title]
    if artist:
        audio.tags["\xa9ART"] = [artist]
    if album:
        audio.tags["\xa9alb"] = [album]
    if genre:
        audio.tags["\xa9gen"] = [genre]
    if year:
        audio.tags["\xa9day"] = [year]

    audio.save()


# ──────────────────────────────────────────────
#  处理单个文件
# ──────────────────────────────────────────────
def process_file(filepath, separator="-", artist_first=False,
                 fetch_online=True, dry_run=False, skip_confirm=False):
    """
    处理单个音频文件

    Args:
        filepath:      文件路径
        separator:      文件名中歌名与歌手的分隔符
        artist_first:   是否"歌手-歌名"格式（默认"歌名-歌手"）
        fetch_online:   是否在线查询专辑/流派
        dry_run:        预览模式，不实际写入
        skip_confirm:   跳过确认直接写入

    Returns:
        bool: 是否成功
    """
    filepath = Path(filepath)
    print(f"\n{'─' * 55}")
    print(f"{BOLD}📄 {filepath.name}{RESET}")
    print(f"{'─' * 55}")

    # 1. 从文件名解析
    title, artist = parse_filename(filepath, separator)
    if artist_first and artist:
        title, artist = artist, title

    artist = clean_artist_name(artist)

    print(f"  {GREEN}从文件名解析:{RESET}")
    print(f"    标题:   {title}")
    print(f"    艺术家: {artist or '(无)'}")

    # 2. 在线查询
    online_info = {}
    if fetch_online and title:
        print(f"\n  {CYAN}🔍 正在查询 MusicBrainz...{RESET}", end="", flush=True)
        online_info = search_musicbrainz(title, artist)

        if online_info:
            print(f" {GREEN}✓ 找到{RESET}")
            if "album" in online_info:
                print(f"    专辑:   {online_info['album']}")
            if "genre" in online_info:
                print(f"    流派:   {online_info['genre']}")
            if "year" in online_info:
                print(f"    年份:   {online_info['year']}")
        else:
            print(f" {YELLOW}✗ 未找到匹配结果{RESET}")

    # 3. 汇总要写入的信息
    new_tags = {}
    if title:
        new_tags["标题"] = title
    if artist:
        new_tags["艺术家"] = artist
    if online_info.get("album"):
        new_tags["专辑"] = online_info["album"]
    if online_info.get("genre"):
        new_tags["流派"] = online_info["genre"]
    if online_info.get("year"):
        new_tags["年份"] = online_info["year"]

    if not new_tags:
        print(f"\n  {YELLOW}⚠ 没有可写入的信息，跳过{RESET}")
        return False

    # 4. 展示要写入的内容
    print(f"\n  {BOLD}📝 将写入以下标签:{RESET}")
    for key, val in new_tags.items():
        print(f"    {YELLOW}{key:　<6}{RESET}: {val}")

    if dry_run:
        print(f"\n  {DIM}[预览模式，未实际写入]{RESET}")
        return True

    # 5. 确认
    if not skip_confirm:
        answer = input(f"\n  是否写入? (y/N/s=跳过): ").strip().lower()
        if answer == "s":
            print(f"  ⏭ 已跳过")
            return False
        if answer not in ("y", "yes"):
            print(f"  ❌ 已取消")
            return False

    # 6. 写入
    try:
        write_tags(
            filepath,
            title=new_tags.get("标题"),
            artist=new_tags.get("艺术家"),
            album=new_tags.get("专辑"),
            genre=new_tags.get("流派"),
            year=new_tags.get("年份"),
        )
        print(f"\n  {GREEN}✅ 标签已更新!{RESET}")
        return True
    except Exception as e:
        print(f"\n  {RED}❌ 写入失败: {e}{RESET}")
        return False


# ──────────────────────────────────────────────
#  主函数
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="🏷️  音频标签修复工具 — 从文件名解析歌名/歌手，在线查询专辑/流派",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  %(prog)s "Apologize-Timbaland&OneRepublic.flac"
  %(prog)s *.flac *.mp3                              批量处理
  %(prog)s -d /path/to/music                         扫描目录
  %(prog)s -d /path/to/music -r                      递归扫描
  %(prog)s song.flac --sep "-"                        指定分隔符
  %(prog)s song.flac --artist-first                   歌手在前: "歌手-歌名"
  %(prog)s song.flac --dry-run                        预览不写入
  %(prog)s song.flac --no-fetch                       不查询网络API
  %(prog)s -d /music -y                               跳过确认直接写入

文件名格式:
  默认: 歌名-歌手.ext     例: Apologize-Timbaland&OneRepublic.flac
  --artist-first: 歌手-歌名.ext   例: Timbaland-Apologize.flac

网络查询:
  使用 MusicBrainz 免费 API 查询专辑和流派信息
  API 限速 1 请求/秒，批量处理时会自动控制节奏
        """,
    )

    parser.add_argument("files", nargs="*", help="要修复的音频文件路径")
    parser.add_argument("-d", "--dir", help="扫描指定目录")
    parser.add_argument("-r", "--recursive", action="store_true", help="递归扫描子目录")
    parser.add_argument("--sep", default="-", help="文件名中歌名与歌手的分隔符 (默认: -)")
    parser.add_argument("--artist-first", action="store_true",
                        help="文件名格式为 '歌手-歌名' (默认: '歌名-歌手')")
    parser.add_argument("--no-fetch", action="store_true", help="不使用网络 API 查询")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不实际写入")
    parser.add_argument("-y", "--yes", action="store_true", help="跳过确认，直接写入")

    args = parser.parse_args()

    if not args.files and not args.dir:
        parser.print_help()
        sys.exit(0)

    # 收集要处理的文件
    files_to_process = []

    if args.dir:
        directory = Path(args.dir)
        if not directory.is_dir():
            print(f"{RED}❌ 目录不存在: {directory}{RESET}")
            sys.exit(1)
        pattern = "**/*" if args.recursive else "*"
        files_to_process = sorted(
            f for f in directory.glob(pattern)
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
        )
    else:
        for f in args.files:
            p = Path(f)
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
                files_to_process.append(p)
            else:
                print(f"{YELLOW}⚠ 跳过: {f} (不存在或不支持的格式){RESET}")

    if not files_to_process:
        print(f"{YELLOW}⚠ 没有找到可处理的音频文件{RESET}")
        sys.exit(0)

    print(f"\n{BOLD}🎵 共找到 {len(files_to_process)} 个音频文件{RESET}")

    if args.dry_run:
        print(f"{DIM}[预览模式 - 不会实际修改文件]{RESET}")

    success_count = 0
    for i, fpath in enumerate(files_to_process):
        success = process_file(
            fpath,
            separator=args.sep,
            artist_first=args.artist_first,
            fetch_online=not args.no_fetch,
            dry_run=args.dry_run,
            skip_confirm=args.yes,
        )
        if success:
            success_count += 1

        # MusicBrainz API 限速：1 请求/秒
        if not args.no_fetch and i < len(files_to_process) - 1:
            time.sleep(1.1)

    print(f"\n{'═' * 55}")
    print(f"{BOLD}📊 完成! 共处理 {len(files_to_process)} 个文件，成功更新 {success_count} 个{RESET}")
    print(f"{'═' * 55}\n")


if __name__ == "__main__":
    main()
