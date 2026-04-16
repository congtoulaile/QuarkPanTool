#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🎵 音乐文件一站式整理工具

按顺序执行四个步骤:
  Step 1: 清理垃圾文件 — 删除 .mgg 等无用格式 + 小于指定大小的文件
  Step 2: 去重          — 同目录下重复文件去重（MD5/文件名相似/同歌不同格式）
  Step 3: 修复文件名    — 去掉 (1) 后缀、[xxx.cn] 前缀等无用标记
  Step 4: 修复标签      — 从文件名解析歌名/歌手，通过 MusicBrainz 查询专辑/流派

依赖: pip install mutagen httpx

使用示例:
  python music_toolkit.py /path/to/music                 执行全部步骤（交互确认）
  python music_toolkit.py /path/to/music -r              递归扫描子目录
  python music_toolkit.py /path/to/music --dry-run       预览模式，不做任何修改
  python music_toolkit.py /path/to/music -y              跳过确认直接执行
  python music_toolkit.py /path/to/music --steps 1,2,3   只执行指定步骤
  python music_toolkit.py /path/to/music --no-fetch      不查询网络API（跳过标签的专辑/流派）
  python music_toolkit.py /path/to/music --min-size 5    设置最小文件大小为5MB（默认3MB）
"""

import os
import re
import sys
import time
import hashlib
import shutil
import argparse
from pathlib import Path
from collections import defaultdict

import httpx
from mutagen import File as MutagenFile
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.oggvorbis import OggVorbis
from mutagen.mp4 import MP4
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TCON, ID3NoHeaderError

# ══════════════════════════════════════════════
#  全局配置
# ══════════════════════════════════════════════
AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".ogg", ".oga", ".m4a", ".mp4", ".aac",
    ".wma", ".wav", ".aiff", ".aif", ".ape", ".opus", ".wv",
}

# 需要删除的垃圾格式
JUNK_EXTENSIONS = {".mgg", ".mflac", ".qmc0", ".qmc3", ".qmcflac", ".tkm", ".bkcmp3", ".bkcflac"}

# 支持写入标签的格式
TAGGABLE_EXTENSIONS = {".mp3", ".flac", ".ogg", ".oga", ".m4a", ".opus"}

# 音质优先级
FORMAT_QUALITY_RANK = {
    ".flac": 100, ".ape": 95, ".wav": 90, ".aiff": 90, ".aif": 90,
    ".wv": 85, ".m4a": 60, ".ogg": 55, ".oga": 55, ".opus": 50,
    ".mp3": 40, ".wma": 30, ".aac": 25, ".mp4": 20,
}

# MusicBrainz API
MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
MB_USER_AGENT = "MusicToolkit/1.0 (https://github.com/example/music-toolkit)"

# 颜色
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
MAGENTA = "\033[35m"

TRASH_DIR_NAME = ".music_trash"


# ══════════════════════════════════════════════
#  通用工具函数
# ══════════════════════════════════════════════
def format_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def file_md5(filepath, chunk_size=8192):
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def safe_rename(old_path, new_path):
    """安全重命名，处理目标已存在的情况"""
    old_path, new_path = Path(old_path), Path(new_path)
    if old_path == new_path:
        return old_path
    if new_path.exists():
        # 如果目标已存在，加序号
        counter = 1
        stem = new_path.stem
        ext = new_path.suffix
        while new_path.exists():
            new_path = new_path.parent / f"{stem}_{counter}{ext}"
            counter += 1
    old_path.rename(new_path)
    return new_path


def collect_all_files(root_path, recursive=False):
    """收集目录下所有文件"""
    root_path = Path(root_path)
    files = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames if d != TRASH_DIR_NAME and not d.startswith(".")]
            for fname in filenames:
                files.append(Path(dirpath) / fname)
    else:
        for item in root_path.iterdir():
            if item.is_file():
                files.append(item)
    return sorted(files)


def collect_audio_files(root_path, recursive=False):
    """收集音频文件"""
    return [f for f in collect_all_files(root_path, recursive)
            if f.suffix.lower() in AUDIO_EXTENSIONS]


def collect_audio_by_dir(root_path, recursive=False):
    """按目录分组收集音频文件"""
    dir_files = defaultdict(list)
    for f in collect_audio_files(root_path, recursive):
        dir_files[str(f.parent)].append(f)
    return dir_files


def print_step_header(step_num, title, icon):
    print(f"\n{'━' * 65}")
    print(f"  {icon}  {BOLD}Step {step_num}: {title}{RESET}")
    print(f"{'━' * 65}")


def print_summary_line(label, value, color=GREEN):
    print(f"    {label}: {color}{value}{RESET}")


# ══════════════════════════════════════════════
#  Step 1: 清理垃圾文件
# ══════════════════════════════════════════════
def step_cleanup(root_path, recursive=False, min_size_mb=8,
                 dry_run=False, skip_confirm=False):
    """
    清理垃圾文件:
    1. 删除 .mgg 等加密/无用格式
    2. 删除小于 min_size_mb 的音频文件
    """
    print_step_header(1, "清理垃圾文件", "🧹")

    all_files = collect_all_files(root_path, recursive)
    min_size_bytes = min_size_mb * 1024 * 1024

    # 分类要删除的文件
    junk_format_files = []    # 垃圾格式
    too_small_files = []      # 太小的音频

    for f in all_files:
        ext = f.suffix.lower()
        if ext in JUNK_EXTENSIONS:
            junk_format_files.append(f)
        elif ext in AUDIO_EXTENSIONS:
            try:
                if f.stat().st_size < min_size_bytes:
                    too_small_files.append(f)
            except OSError:
                pass

    total_to_delete = len(junk_format_files) + len(too_small_files)

    if total_to_delete == 0:
        print(f"\n  {GREEN}✅ 没有需要清理的文件{RESET}")
        return 0

    # 展示垃圾格式文件
    if junk_format_files:
        print(f"\n  {RED}■ 垃圾格式文件 ({len(junk_format_files)} 个):{RESET}")
        total_junk_size = 0
        for f in junk_format_files:
            size = f.stat().st_size
            total_junk_size += size
            print(f"    ✘ {f.name}  ({format_size(size)})  📁 {f.parent}")
        print(f"    {DIM}合计: {format_size(total_junk_size)}{RESET}")

    # 展示过小文件
    if too_small_files:
        print(f"\n  {RED}■ 小于 {min_size_mb}MB 的音频文件 ({len(too_small_files)} 个):{RESET}")
        total_small_size = 0
        for f in too_small_files:
            size = f.stat().st_size
            total_small_size += size
            print(f"    ✘ {f.name}  ({format_size(size)})  📁 {f.parent}")
        print(f"    {DIM}合计: {format_size(total_small_size)}{RESET}")

    if dry_run:
        print(f"\n  {DIM}[预览模式 — 不会删除]{RESET}")
        return total_to_delete

    # 确认
    if not skip_confirm:
        answer = input(f"\n  确认删除以上 {total_to_delete} 个文件? (y/N): ").strip().lower()
        if answer not in ("y", "yes"):
            print(f"  {DIM}已跳过 Step 1{RESET}")
            return 0

    # 执行删除
    deleted = 0
    freed = 0
    for f in junk_format_files + too_small_files:
        try:
            size = f.stat().st_size
            f.unlink()
            deleted += 1
            freed += size
        except Exception as e:
            print(f"    {RED}❌ 删除失败: {f.name} — {e}{RESET}")

    print(f"\n  {GREEN}✅ 已删除 {deleted} 个文件，释放 {format_size(freed)}{RESET}")
    return deleted


# ══════════════════════════════════════════════
#  Step 2: 去重
# ══════════════════════════════════════════════
def normalize_filename_for_dedup(filename):
    """标准化文件名用于去重匹配"""
    stem = Path(filename).stem
    ext = Path(filename).suffix.lower()
    name = stem

    # 先去掉前缀 [xxx.cn] [xxx.com] 等
    name = re.sub(r"^\[.*?\]\s*", "", name)

    # 去掉常见重复后缀
    dup_patterns = [
        r"\s*\(\d+\)\s*$",           # (1) (2)
        r"\s*（\d+）\s*$",            # （1）
        r"\s*\[\d+\]\s*$",           # [1]
        r"\s*_\d+\s*$",              # _1
        r"\s*-\s*副本\s*$",          # - 副本
        r"\s*-\s*Copy\s*$",          # - Copy
        r"\s*\(\s*副本\s*\)\s*$",
        r"\s*\(\s*copy\s*\)\s*$",
        r"\s*copy\s*$",
        r"\s+\d{1,2}\s*$",
    ]
    for pat in dup_patterns:
        name = re.sub(pat, "", name, flags=re.IGNORECASE)

    name = re.sub(r"\s+", " ", name).strip().lower()
    return name, ext


def get_audio_quality_score(filepath):
    ext = Path(filepath).suffix.lower()
    score = FORMAT_QUALITY_RANK.get(ext, 0)
    try:
        audio = MutagenFile(str(filepath))
        if audio and audio.info:
            if hasattr(audio.info, "bitrate") and audio.info.bitrate:
                score += min(audio.info.bitrate / 10000, 50)
            if hasattr(audio.info, "bits_per_sample") and audio.info.bits_per_sample:
                score += audio.info.bits_per_sample
            if hasattr(audio.info, "sample_rate") and audio.info.sample_rate:
                score += audio.info.sample_rate / 10000
    except Exception:
        pass
    return score


def find_duplicates_in_dir(file_list):
    """在同目录的文件列表中找出重复组"""
    results = []
    name_groups = defaultdict(list)
    for fpath in file_list:
        norm_name, ext = normalize_filename_for_dedup(fpath.name)
        name_groups[norm_name].append(fpath)

    for norm_name, group in name_groups.items():
        if len(group) < 2:
            continue

        ext_groups = defaultdict(list)
        for fpath in group:
            ext_groups[fpath.suffix.lower()].append(fpath)

        # 同格式重复
        for ext, same_ext_files in ext_groups.items():
            if len(same_ext_files) < 2:
                continue

            md5_groups = defaultdict(list)
            for fpath in same_ext_files:
                md5 = file_md5(fpath)
                md5_groups[md5].append(fpath)

            # MD5 完全相同
            for md5, md5_files in md5_groups.items():
                if len(md5_files) < 2:
                    continue
                sorted_files = sorted(md5_files, key=lambda f: (len(f.stem), f.name))
                results.append({
                    "type": "exact_duplicate",
                    "keep": sorted_files[0],
                    "remove": sorted_files[1:],
                    "reason": "MD5 完全相同",
                })

            # 同格式 MD5 不同
            if len(md5_groups) > 1:
                singles = [files[0] for files in md5_groups.values() if len(files) == 1]
                if len(singles) >= 2:
                    scored = sorted(singles, key=get_audio_quality_score, reverse=True)
                    results.append({
                        "type": "name_duplicate",
                        "keep": scored[0],
                        "remove": scored[1:],
                        "reason": f"文件名相似，按音质保留最优",
                    })

        # 不同格式
        if len(ext_groups) >= 2:
            reps = [max(files, key=get_audio_quality_score) for files in ext_groups.values()]
            if len(reps) >= 2:
                scored = sorted(reps, key=get_audio_quality_score, reverse=True)
                fmts = "/".join(f.suffix.upper().lstrip(".") for f in scored)
                results.append({
                    "type": "format_variant",
                    "keep": scored[0],
                    "remove": scored[1:],
                    "reason": f"同歌不同格式({fmts})，保留 {scored[0].suffix.upper()}",
                })

    return results


TYPE_LABELS = {
    "exact_duplicate": "📋 精确重复",
    "name_duplicate":  "📝 文件名相似",
    "format_variant":  "🎵 同歌不同格式",
}


def step_dedup(root_path, recursive=False, dry_run=False, skip_confirm=False):
    """去重处理"""
    print_step_header(2, "去重", "🔍")

    dir_files = collect_audio_by_dir(root_path, recursive)
    total_files = sum(len(f) for f in dir_files.values())
    print(f"\n  共扫描 {total_files} 个音频文件，分布在 {len(dir_files)} 个目录\n")

    all_dups = []
    total_removable_size = 0

    for dir_path, files in sorted(dir_files.items()):
        if len(files) < 2:
            continue
        dups = find_duplicates_in_dir(files)
        if not dups:
            continue

        print(f"  {CYAN}📁 {dir_path}/{RESET}")
        for dup in dups:
            all_dups.append(dup)
            keep = dup["keep"]
            label = TYPE_LABELS.get(dup["type"], "")
            keep_size = format_size(keep.stat().st_size)
            score = get_audio_quality_score(keep)
            print(f"    {label} — {DIM}{dup['reason']}{RESET}")
            print(f"      {GREEN}✔ 保留: {keep.name}{RESET}  ({keep_size}, 分:{score:.0f})")
            for rm in dup["remove"]:
                rm_size = format_size(rm.stat().st_size)
                rm_score = get_audio_quality_score(rm)
                total_removable_size += rm.stat().st_size
                print(f"      {RED}✘ 删除: {rm.name}{RESET}  ({rm_size}, 分:{rm_score:.0f})")

    total_remove = sum(len(d["remove"]) for d in all_dups)
    if total_remove == 0:
        print(f"  {GREEN}✅ 没有发现重复文件{RESET}")
        return 0

    print(f"\n  {BOLD}共发现 {len(all_dups)} 组重复，可删除 {total_remove} 个文件，释放 {format_size(total_removable_size)}{RESET}")

    if dry_run:
        print(f"  {DIM}[预览模式 — 不会删除]{RESET}")
        return total_remove

    if not skip_confirm:
        answer = input(f"\n  确认删除以上 {total_remove} 个重复文件? (y/N): ").strip().lower()
        if answer not in ("y", "yes"):
            print(f"  {DIM}已跳过 Step 2{RESET}")
            return 0

    deleted = 0
    freed = 0
    for dup in all_dups:
        for rm in dup["remove"]:
            try:
                size = rm.stat().st_size
                rm.unlink()
                deleted += 1
                freed += size
            except Exception as e:
                print(f"    {RED}❌ {rm.name}: {e}{RESET}")

    print(f"\n  {GREEN}✅ 已删除 {deleted} 个文件，释放 {format_size(freed)}{RESET}")
    return deleted


# ══════════════════════════════════════════════
#  Step 3: 修复文件名
# ══════════════════════════════════════════════
def clean_filename(filename):
    """
    清理文件名，返回新的 stem（不含扩展名）
    - 去掉 [livepoo.cn] 等 [xxx] 前缀
    - 去掉 (1) (2) 等重复后缀
    - 去掉多余空格
    """
    stem = Path(filename).stem
    original_stem = stem

    # 1. 去掉开头的 [xxx.xxx] [xxx] 前缀（如 [livepoo.cn]、[music.163.com] 等）
    stem = re.sub(r"^\[.*?\]\s*", "", stem)

    # 2. 去掉末尾的重复标记
    dup_patterns = [
        r"\s*\(\d+\)\s*$",           # (1) (2)
        r"\s*（\d+）\s*$",            # （1）（2）
        r"\s*\[\d+\]\s*$",           # [1]
        r"\s*_\(\d+\)\s*$",          # _(1)
        r"\s*-\s*副本\s*$",          # - 副本
        r"\s*-\s*Copy\s*$",          # - Copy
        r"\s*\(\s*副本\s*\)\s*$",
        r"\s*\(\s*copy\s*\)\s*$",
    ]
    for pat in dup_patterns:
        stem = re.sub(pat, "", stem, flags=re.IGNORECASE)

    # 3. 清理多余空格
    stem = re.sub(r"\s+", " ", stem).strip()

    # 4. 如果清理后为空，保留原名
    if not stem:
        stem = original_stem

    return stem


def step_fix_filenames(root_path, recursive=False, dry_run=False, skip_confirm=False):
    """修复文件名"""
    print_step_header(3, "修复文件名", "✏️")

    audio_files = collect_audio_files(root_path, recursive)
    rename_plan = []  # (old_path, new_path)

    for fpath in audio_files:
        new_stem = clean_filename(fpath.name)
        ext = fpath.suffix  # 保留原始扩展名大小写
        new_name = new_stem + ext
        if new_name != fpath.name:
            new_path = fpath.parent / new_name
            rename_plan.append((fpath, new_path))

    if not rename_plan:
        print(f"\n  {GREEN}✅ 所有文件名都很干净，无需修改{RESET}")
        return 0

    print(f"\n  需要重命名 {len(rename_plan)} 个文件:\n")
    for old, new in rename_plan:
        # 高亮差异部分
        print(f"    {RED}{old.name}{RESET}")
        print(f"    → {GREEN}{new.name}{RESET}")
        if str(old.parent) != str(root_path):
            print(f"      📁 {DIM}{old.parent}{RESET}")
        print()

    if dry_run:
        print(f"  {DIM}[预览模式 — 不会重命名]{RESET}")
        return len(rename_plan)

    if not skip_confirm:
        answer = input(f"  确认重命名以上 {len(rename_plan)} 个文件? (y/N): ").strip().lower()
        if answer not in ("y", "yes"):
            print(f"  {DIM}已跳过 Step 3{RESET}")
            return 0

    renamed = 0
    for old, new in rename_plan:
        try:
            actual = safe_rename(old, new)
            renamed += 1
        except Exception as e:
            print(f"    {RED}❌ {old.name}: {e}{RESET}")

    print(f"\n  {GREEN}✅ 已重命名 {renamed} 个文件{RESET}")
    return renamed


# ══════════════════════════════════════════════
#  Step 4: 修复标签
# ══════════════════════════════════════════════
def parse_song_from_filename(filepath, separator="-"):
    """从文件名解析歌名和歌手"""
    stem = Path(filepath).stem.strip()
    # 先清理前缀（以防 step3 没执行）
    stem = re.sub(r"^\[.*?\]\s*", "", stem)
    # 清理重复后缀
    stem = re.sub(r"\s*\(\d+\)\s*$", "", stem)
    stem = re.sub(r"\s*（\d+）\s*$", "", stem)
    stem = stem.strip()

    parts = stem.split(separator, 1)
    if len(parts) == 2:
        title = parts[0].strip()
        artist = parts[1].strip()
        if title and artist:
            return title, artist
    return stem, None


def search_musicbrainz(title, artist=None):
    """查询 MusicBrainz 获取专辑和流派"""
    query_parts = [f'recording:"{title}"']
    if artist:
        primary = re.split(r"[&,;/、]", artist)[0].strip()
        query_parts.append(f'artist:"{primary}"')

    query = " AND ".join(query_parts)
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"{MUSICBRAINZ_API}/recording",
                params={"query": query, "limit": 5, "fmt": "json"},
                headers={"User-Agent": MB_USER_AGENT, "Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        recordings = data.get("recordings", [])
        if not recordings:
            return {}

        rec = recordings[0]
        result = {}

        releases = rec.get("releases", [])
        if releases:
            album_rel = None
            for rel in releases:
                rg = rel.get("release-group", {})
                if rg.get("primary-type") == "Album":
                    album_rel = rel
                    break
            if album_rel is None:
                album_rel = releases[0]
            result["album"] = album_rel.get("title", "")
            date = album_rel.get("date", "")
            if date:
                result["year"] = date[:4]

        tags = rec.get("tags", [])
        if tags:
            sorted_tags = sorted(tags, key=lambda t: t.get("count", 0), reverse=True)
            genre_list = [t["name"] for t in sorted_tags[:3] if t.get("name")]
            if genre_list:
                result["genre"] = "; ".join(genre_list)

        if "genre" not in result and releases:
            rg = releases[0].get("release-group", {})
            rg_id = rg.get("id")
            if rg_id:
                result.update(_fetch_rg_genre(rg_id))

        return result
    except Exception:
        return {}


def _fetch_rg_genre(rg_id):
    try:
        time.sleep(1.1)
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                f"{MUSICBRAINZ_API}/release-group/{rg_id}",
                params={"inc": "genres tags", "fmt": "json"},
                headers={"User-Agent": MB_USER_AGENT, "Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        genres = data.get("genres", [])
        if genres:
            s = sorted(genres, key=lambda g: g.get("count", 0), reverse=True)
            return {"genre": "; ".join(g["name"] for g in s[:3])}
        tags = data.get("tags", [])
        if tags:
            s = sorted(tags, key=lambda t: t.get("count", 0), reverse=True)
            return {"genre": "; ".join(t["name"] for t in s[:3])}
    except Exception:
        pass
    return {}


def write_tags(filepath, title=None, artist=None, album=None, genre=None, year=None):
    """写入标签"""
    audio = MutagenFile(str(filepath))
    if audio is None:
        return

    if isinstance(audio, MP3):
        try:
            id3 = ID3(str(filepath))
        except ID3NoHeaderError:
            id3 = ID3()
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

    elif isinstance(audio, (FLAC, OggVorbis)):
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

    elif isinstance(audio, MP4):
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


def step_fix_tags(root_path, recursive=False, separator="-", artist_first=False,
                  fetch_online=True, dry_run=False, skip_confirm=False):
    """修复标签"""
    print_step_header(4, "修复标签", "🏷️")

    audio_files = [
        f for f in collect_audio_files(root_path, recursive)
        if f.suffix.lower() in TAGGABLE_EXTENSIONS
    ]

    if not audio_files:
        print(f"\n  {YELLOW}⚠ 没有可处理的音频文件{RESET}")
        return 0

    print(f"\n  共找到 {len(audio_files)} 个可处理的音频文件")
    if fetch_online:
        print(f"  {DIM}(将通过 MusicBrainz API 查询专辑/流派，限速 1 请求/秒){RESET}")

    # 先收集所有待写入的信息
    tag_plan = []  # (filepath, tags_dict)

    for i, fpath in enumerate(audio_files):
        title, artist = parse_song_from_filename(fpath, separator)
        if artist_first and artist:
            title, artist = artist, title

        # 清理歌手名
        if artist:
            artist = artist.replace("、", "; ")

        tags = {}
        if title:
            tags["title"] = title
        if artist:
            tags["artist"] = artist

        # 在线查询
        online = {}
        if fetch_online and title:
            print(f"  {DIM}[{i+1}/{len(audio_files)}] 查询: {title} - {artist or '?'}...{RESET}",
                  end="", flush=True)
            online = search_musicbrainz(title, artist)
            if online:
                print(f" {GREEN}✓{RESET}")
            else:
                print(f" {YELLOW}✗{RESET}")

            if online.get("album"):
                tags["album"] = online["album"]
            if online.get("genre"):
                tags["genre"] = online["genre"]
            if online.get("year"):
                tags["year"] = online["year"]

            # MusicBrainz 限速
            if i < len(audio_files) - 1:
                time.sleep(1.1)

        if tags:
            tag_plan.append((fpath, tags))

    if not tag_plan:
        print(f"\n  {GREEN}✅ 无标签需要更新{RESET}")
        return 0

    # 展示计划
    print(f"\n  {BOLD}标签更新计划 ({len(tag_plan)} 个文件):{RESET}\n")
    for fpath, tags in tag_plan:
        print(f"    {CYAN}{fpath.name}{RESET}")
        for k, v in tags.items():
            label_map = {"title": "标题", "artist": "艺术家", "album": "专辑",
                         "genre": "流派", "year": "年份"}
            display = str(v)
            if len(display) > 60:
                display = display[:60] + "..."
            print(f"      {YELLOW}{label_map.get(k, k):　<5}{RESET}: {display}")
        print()

    if dry_run:
        print(f"  {DIM}[预览模式 — 不会写入标签]{RESET}")
        return len(tag_plan)

    if not skip_confirm:
        answer = input(f"  确认更新以上 {len(tag_plan)} 个文件的标签? (y/N): ").strip().lower()
        if answer not in ("y", "yes"):
            print(f"  {DIM}已跳过 Step 4{RESET}")
            return 0

    updated = 0
    for fpath, tags in tag_plan:
        try:
            write_tags(
                fpath,
                title=tags.get("title"),
                artist=tags.get("artist"),
                album=tags.get("album"),
                genre=tags.get("genre"),
                year=tags.get("year"),
            )
            updated += 1
        except Exception as e:
            print(f"    {RED}❌ {fpath.name}: {e}{RESET}")

    print(f"\n  {GREEN}✅ 已更新 {updated} 个文件的标签{RESET}")
    return updated


# ══════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="🎵 音乐文件一站式整理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  %(prog)s /path/to/music                   执行全部 4 个步骤（交互确认）
  %(prog)s /path/to/music -r                递归扫描子目录
  %(prog)s /path/to/music --dry-run         预览模式，不做任何修改
  %(prog)s /path/to/music -y                跳过确认直接执行
  %(prog)s /path/to/music --steps 1,2,3     只执行指定步骤
  %(prog)s /path/to/music --steps 4 --no-fetch   只修复标签，不查网络
  %(prog)s /path/to/music --min-size 5      最小文件大小改为 5MB

处理步骤:
  Step 1: 🧹 清理垃圾      删除 .mgg 等格式 + 小于 3MB 的音频
  Step 2: 🔍 去重          同目录下重复文件/同歌不同格式去重
  Step 3: ✏️  修复文件名    去掉 (1) 后缀、[livepoo.cn] 前缀
  Step 4: 🏷️  修复标签      从文件名解析歌名/歌手 + MusicBrainz 查专辑/流派

建议:
  首次使用先加 --dry-run 预览全部步骤的效果，确认无误再正式执行
        """,
    )

    parser.add_argument("directory", help="音乐文件目录路径")
    parser.add_argument("-r", "--recursive", action="store_true", help="递归扫描子目录")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不做任何修改")
    parser.add_argument("-y", "--yes", action="store_true", help="跳过所有确认")
    parser.add_argument("--steps", default="1,2,3,4",
                        help="要执行的步骤，逗号分隔 (默认: 1,2,3,4)")
    parser.add_argument("--min-size", type=float, default=3,
                        help="最小文件大小(MB)，低于此值的音频文件将被删除 (默认: 3)")
    parser.add_argument("--sep", default="-",
                        help="文件名中歌名与歌手的分隔符 (默认: -)")
    parser.add_argument("--artist-first", action="store_true",
                        help="文件名格式为 '歌手-歌名' (默认: '歌名-歌手')")
    parser.add_argument("--no-fetch", action="store_true",
                        help="不使用 MusicBrainz API 查询专辑/流派")

    args = parser.parse_args()

    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"{RED}❌ 目录不存在: {directory}{RESET}")
        sys.exit(1)

    steps = set()
    for s in args.steps.split(","):
        s = s.strip()
        if s in ("1", "2", "3", "4"):
            steps.add(int(s))
        else:
            print(f"{YELLOW}⚠ 忽略无效步骤: {s}{RESET}")

    if not steps:
        print(f"{RED}❌ 没有有效的步骤可执行{RESET}")
        sys.exit(1)

    # Banner
    print(f"\n{'═' * 65}")
    print(f"  {BOLD}{MAGENTA}🎵 音乐文件一站式整理工具{RESET}")
    print(f"{'═' * 65}")
    print(f"  目录: {CYAN}{directory.resolve()}{RESET}")
    print(f"  递归: {'是' if args.recursive else '否'}")
    print(f"  步骤: {', '.join(f'Step {s}' for s in sorted(steps))}")
    if args.dry_run:
        print(f"  模式: {YELLOW}预览模式 (不做任何修改){RESET}")
    print(f"{'═' * 65}")

    results = {}

    # Step 1: 清理
    if 1 in steps:
        results["cleanup"] = step_cleanup(
            directory, args.recursive, args.min_size,
            args.dry_run, args.yes,
        )

    # Step 2: 去重
    if 2 in steps:
        results["dedup"] = step_dedup(
            directory, args.recursive,
            args.dry_run, args.yes,
        )

    # Step 3: 修复文件名
    if 3 in steps:
        results["rename"] = step_fix_filenames(
            directory, args.recursive,
            args.dry_run, args.yes,
        )

    # Step 4: 修复标签
    if 4 in steps:
        results["tags"] = step_fix_tags(
            directory, args.recursive, args.sep, args.artist_first,
            not args.no_fetch, args.dry_run, args.yes,
        )

    # 最终汇总
    print(f"\n{'━' * 65}")
    print(f"  {BOLD}{MAGENTA}📊 执行结果汇总{RESET}")
    print(f"{'━' * 65}")
    if "cleanup" in results:
        print_summary_line("  Step 1 清理垃圾", f"{results['cleanup']} 个文件")
    if "dedup" in results:
        print_summary_line("  Step 2 去重删除", f"{results['dedup']} 个文件")
    if "rename" in results:
        print_summary_line("  Step 3 重命名  ", f"{results['rename']} 个文件")
    if "tags" in results:
        print_summary_line("  Step 4 标签修复", f"{results['tags']} 个文件")
    print(f"{'━' * 65}")
    print(f"  {GREEN}{BOLD}✅ 全部完成!{RESET}\n")


if __name__ == "__main__":
    main()
