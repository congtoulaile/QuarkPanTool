#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
音乐文件去重工具
功能:
  1. 同目录下文件名相似去重（如 song.flac 和 song(1).flac）
  2. 同目录下同一首歌不同格式，保留音质最高的版本
  3. 同目录下 MD5 完全相同的文件去重
  4. 只处理同一目录，跨目录的相同文件不处理

依赖: pip install mutagen

使用示例:
  python dedup_music.py /path/to/music              扫描并交互确认
  python dedup_music.py /path/to/music -r            递归扫描子目录
  python dedup_music.py /path/to/music --dry-run     预览模式，不删除
  python dedup_music.py /path/to/music -y            跳过确认直接删除
  python dedup_music.py /path/to/music --to-trash    移到回收站而非删除
"""

import os
import re
import sys
import hashlib
import shutil
import argparse
from pathlib import Path
from collections import defaultdict

from mutagen import File as MutagenFile

# ──────────────────────────────────────────────
#  配置
# ──────────────────────────────────────────────
SUPPORTED_EXTENSIONS = {
    ".mp3", ".flac", ".ogg", ".oga", ".m4a", ".mp4", ".aac",
    ".wma", ".wav", ".aiff", ".aif", ".ape", ".opus", ".wv",
}

# 音质优先级：数字越大越好，优先保留
FORMAT_QUALITY_RANK = {
    ".flac": 100,   # 无损
    ".ape":  95,    # 无损
    ".wav":  90,    # 无损（但通常无标签）
    ".aiff": 90,    # 无损
    ".aif":  90,
    ".wv":   85,    # WavPack 无损
    ".m4a":  60,    # AAC 有损（但通常较高质量）
    ".ogg":  55,    # Vorbis
    ".oga":  55,
    ".opus": 50,    # Opus
    ".mp3":  40,    # MP3
    ".wma":  30,    # WMA
    ".aac":  25,
    ".mp4":  20,
}

# 颜色
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

# 回收站目录名（用 --to-trash 时在同级创建）
TRASH_DIR_NAME = ".music_trash"


# ──────────────────────────────────────────────
#  工具函数
# ──────────────────────────────────────────────
def format_filesize(size_bytes):
    """格式化文件大小"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def file_md5(filepath, chunk_size=8192):
    """计算文件 MD5"""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def get_audio_quality_score(filepath):
    """
    获取音频文件的质量得分，用于决定保留哪个
    综合考虑: 格式优先级 + 比特率 + 文件大小
    返回越大越好
    """
    ext = Path(filepath).suffix.lower()
    base_score = FORMAT_QUALITY_RANK.get(ext, 0)

    # 尝试读取比特率信息作为加分项
    try:
        audio = MutagenFile(str(filepath))
        if audio and audio.info:
            # 比特率加分（归一化到 0~50 分）
            if hasattr(audio.info, "bitrate") and audio.info.bitrate:
                base_score += min(audio.info.bitrate / 10000, 50)
            # 位深度加分
            if hasattr(audio.info, "bits_per_sample") and audio.info.bits_per_sample:
                base_score += audio.info.bits_per_sample
            # 采样率加分（归一化）
            if hasattr(audio.info, "sample_rate") and audio.info.sample_rate:
                base_score += audio.info.sample_rate / 10000
    except Exception:
        pass

    return base_score


def normalize_filename(filename):
    """
    标准化文件名，用于模糊匹配
    去除:
      - 后缀的 (1), (2), _1, _2, - 副本, - Copy, (copy) 等
      - 多余空格
      - 大小写统一
    返回 (标准化名称, 扩展名)
    """
    stem = Path(filename).stem
    ext = Path(filename).suffix.lower()

    name = stem

    # 去掉常见的重复文件后缀标记
    patterns = [
        r"\s*\(\d+\)\s*$",           # song(1), song (2)
        r"\s*（\d+）\s*$",            # song（1）中文括号
        r"\s*\[\d+\]\s*$",           # song[1]
        r"\s*_\d+\s*$",              # song_1
        r"\s*-\s*\d+\s*$",           # song-1 (但注意不误删 "歌名-歌手" 中的歌手)
        r"\s*-\s*副本\s*$",          # song - 副本
        r"\s*-\s*Copy\s*$",          # song - Copy
        r"\s*\(\s*副本\s*\)\s*$",    # song(副本)
        r"\s*\(\s*copy\s*\)\s*$",    # song(copy)
        r"\s*copy\s*$",              # song copy
        r"\s+\d{1,2}\s*$",           # "song 1" (仅1-2位数字，避免误删年份)
    ]

    for pat in patterns:
        name = re.sub(pat, "", name, flags=re.IGNORECASE)

    # 统一空格和大小写
    name = re.sub(r"\s+", " ", name).strip().lower()

    return name, ext


def collect_audio_files_by_dir(root_path, recursive=False):
    """
    收集音频文件，按所在目录分组
    返回 {目录路径: [文件Path列表]}
    """
    dir_files = defaultdict(list)

    if recursive:
        for dirpath, dirnames, filenames in os.walk(root_path):
            # 跳过回收站目录
            dirnames[:] = [d for d in dirnames if d != TRASH_DIR_NAME]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                if fpath.suffix.lower() in SUPPORTED_EXTENSIONS:
                    dir_files[dirpath].append(fpath)
    else:
        for item in Path(root_path).iterdir():
            if item.is_file() and item.suffix.lower() in SUPPORTED_EXTENSIONS:
                dir_files[str(root_path)].append(item)

    return dir_files


# ──────────────────────────────────────────────
#  去重策略
# ──────────────────────────────────────────────
def find_duplicates_in_dir(file_list):
    """
    在同一目录的文件列表中找出重复组

    策略:
      1. 先按标准化文件名分组（模糊匹配）
      2. 组内如果有多个文件，再用 MD5 验证内容是否真的一样
      3. 对于同名不同格式的文件（歌名部分一致），视为同歌不同版本

    Returns:
        list of dict:
          {
            "type": "name_duplicate" | "format_variant" | "exact_duplicate",
            "canonical_name": str,  # 标准化后的名称
            "keep": Path,           # 建议保留的文件
            "remove": [Path],       # 建议删除的文件列表
            "reason": str,          # 原因说明
          }
    """
    results = []

    # ── 第一步: 按标准化名称分组 ──
    name_groups = defaultdict(list)
    for fpath in file_list:
        norm_name, ext = normalize_filename(fpath.name)
        # 分组键: 标准化名称（不含扩展名）
        name_groups[norm_name].append(fpath)

    for norm_name, group in name_groups.items():
        if len(group) < 2:
            continue

        # ── 第二步: 在组内细分 "同格式" 和 "不同格式" ──
        ext_groups = defaultdict(list)
        for fpath in group:
            ext_groups[fpath.suffix.lower()].append(fpath)

        # --- 情况 A: 同格式多个文件（如 song.flac + song(1).flac）---
        for ext, same_ext_files in ext_groups.items():
            if len(same_ext_files) < 2:
                continue

            # 用 MD5 找精确重复
            md5_groups = defaultdict(list)
            for fpath in same_ext_files:
                md5 = file_md5(fpath)
                md5_groups[md5].append(fpath)

            for md5, md5_files in md5_groups.items():
                if len(md5_files) < 2:
                    continue

                # MD5 完全相同 → 精确重复，保留文件名最短的（通常是原始文件）
                sorted_files = sorted(md5_files, key=lambda f: (len(f.stem), f.name))
                keep = sorted_files[0]
                remove = sorted_files[1:]

                results.append({
                    "type": "exact_duplicate",
                    "canonical_name": norm_name,
                    "keep": keep,
                    "remove": remove,
                    "reason": f"MD5 完全相同，保留文件名最短的原始文件",
                })

            # 同格式但 MD5 不同的（可能编码参数不同）
            # 如果有多个不同 MD5 且文件名极相似，提示但不自动删除
            if len(md5_groups) > 1:
                all_md5_files = []
                for files in md5_groups.values():
                    if len(files) == 1:
                        all_md5_files.append(files[0])
                if len(all_md5_files) >= 2:
                    # 按质量得分排序
                    scored = sorted(
                        all_md5_files,
                        key=lambda f: get_audio_quality_score(f),
                        reverse=True,
                    )
                    keep = scored[0]
                    remove = scored[1:]
                    results.append({
                        "type": "name_duplicate",
                        "canonical_name": norm_name,
                        "keep": keep,
                        "remove": remove,
                        "reason": f"文件名相似(标准化后均为 '{norm_name}{ext}')，MD5 不同，按音质得分保留最优",
                    })

        # --- 情况 B: 不同格式（如 song.mp3 + song.flac）---
        if len(ext_groups) >= 2:
            # 每种格式取一个代表（如果同格式有多个，上面已经去重了，这里取质量最高的）
            representatives = []
            for ext, same_ext_files in ext_groups.items():
                best = max(same_ext_files, key=lambda f: get_audio_quality_score(f))
                representatives.append(best)

            if len(representatives) >= 2:
                # 按音质排序
                scored = sorted(
                    representatives,
                    key=lambda f: get_audio_quality_score(f),
                    reverse=True,
                )
                keep = scored[0]
                remove = scored[1:]
                formats_str = ", ".join(f.suffix.upper() for f in scored)
                results.append({
                    "type": "format_variant",
                    "canonical_name": norm_name,
                    "keep": keep,
                    "remove": remove,
                    "reason": f"同歌不同格式({formats_str})，保留音质最高的 {keep.suffix.upper()}",
                })

    return results


# ──────────────────────────────────────────────
#  执行删除 / 移入回收站
# ──────────────────────────────────────────────
def remove_file(filepath, to_trash=False):
    """删除文件或移入回收站"""
    filepath = Path(filepath)
    if to_trash:
        trash_dir = filepath.parent / TRASH_DIR_NAME
        trash_dir.mkdir(exist_ok=True)
        dest = trash_dir / filepath.name
        # 如果回收站已有同名文件，加序号
        counter = 1
        while dest.exists():
            dest = trash_dir / f"{filepath.stem}_{counter}{filepath.suffix}"
            counter += 1
        shutil.move(str(filepath), str(dest))
        return f"已移入回收站: {dest.name}"
    else:
        filepath.unlink()
        return "已删除"


# ──────────────────────────────────────────────
#  打印报告
# ──────────────────────────────────────────────
TYPE_LABELS = {
    "exact_duplicate": "📋 精确重复（MD5 相同）",
    "name_duplicate":  "📝 文件名相似",
    "format_variant":  "🎵 同歌不同格式",
}


def print_duplicate_group(idx, dup, verbose=False):
    """打印一组重复信息"""
    type_label = TYPE_LABELS.get(dup["type"], dup["type"])

    print(f"\n  {BOLD}[{idx}] {type_label}{RESET}")
    print(f"  {DIM}{dup['reason']}{RESET}")

    keep = dup["keep"]
    keep_size = format_filesize(keep.stat().st_size) if keep.exists() else "?"
    keep_score = get_audio_quality_score(keep) if keep.exists() else 0
    print(f"    {GREEN}✔ 保留: {keep.name}{RESET}  ({keep_size}, 质量分: {keep_score:.1f})")

    for rm in dup["remove"]:
        rm_size = format_filesize(rm.stat().st_size) if rm.exists() else "?"
        rm_score = get_audio_quality_score(rm) if rm.exists() else 0
        print(f"    {RED}✘ 删除: {rm.name}{RESET}  ({rm_size}, 质量分: {rm_score:.1f})")


# ──────────────────────────────────────────────
#  主流程
# ──────────────────────────────────────────────
def process_directory(root_path, recursive=False, dry_run=False,
                      skip_confirm=False, to_trash=False):
    """处理指定目录"""
    root_path = Path(root_path).resolve()
    print(f"\n{BOLD}🔍 扫描目录: {root_path}{RESET}")
    if recursive:
        print(f"   {DIM}(递归模式){RESET}")

    # 收集文件
    dir_files = collect_audio_files_by_dir(root_path, recursive)

    total_files = sum(len(files) for files in dir_files.values())
    print(f"   共找到 {total_files} 个音频文件，分布在 {len(dir_files)} 个目录中\n")

    if total_files == 0:
        print(f"{YELLOW}⚠ 没有找到音频文件{RESET}")
        return

    all_duplicates = []
    total_removable_size = 0

    # 逐目录分析
    for dir_path, files in sorted(dir_files.items()):
        if len(files) < 2:
            continue

        dups = find_duplicates_in_dir(files)
        if not dups:
            continue

        print(f"{CYAN}{'─' * 60}{RESET}")
        print(f"{BOLD}📁 {dir_path}/{RESET}  ({len(files)} 个音频文件)")

        for dup in dups:
            all_duplicates.append((dir_path, dup))
            idx = len(all_duplicates)
            print_duplicate_group(idx, dup)

            for rm in dup["remove"]:
                if rm.exists():
                    total_removable_size += rm.stat().st_size

    # 汇总报告
    print(f"\n{'═' * 60}")
    total_remove_count = sum(len(dup["remove"]) for _, dup in all_duplicates)

    if not all_duplicates:
        print(f"{GREEN}✅ 没有发现重复文件，目录很干净！{RESET}")
        return

    print(f"{BOLD}📊 扫描结果汇总{RESET}")
    print(f"   发现 {YELLOW}{len(all_duplicates)}{RESET} 组重复")
    print(f"   可删除 {YELLOW}{total_remove_count}{RESET} 个文件")
    print(f"   可释放 {YELLOW}{format_filesize(total_removable_size)}{RESET} 空间")
    print(f"{'═' * 60}")

    if dry_run:
        print(f"\n{DIM}[预览模式 — 不会删除任何文件]{RESET}\n")
        return

    # 确认并执行删除
    if not skip_confirm:
        print(f"\n操作选项:")
        print(f"  {GREEN}a{RESET} = 全部删除    {YELLOW}s{RESET} = 逐个确认    {RED}q{RESET} = 取消退出")
        action_str = "移入回收站" if to_trash else "删除"
        choice = input(f"\n选择操作 (a/s/q): ").strip().lower()

        if choice == "q":
            print(f"\n{DIM}已取消{RESET}\n")
            return
        elif choice == "a":
            skip_confirm = True
        elif choice != "s":
            print(f"\n{DIM}无效选择，已取消{RESET}\n")
            return

    # 执行删除
    deleted_count = 0
    freed_size = 0

    for idx, (dir_path, dup) in enumerate(all_duplicates, 1):
        for rm_file in dup["remove"]:
            if not rm_file.exists():
                continue

            if not skip_confirm:
                type_label = TYPE_LABELS.get(dup["type"], "")
                print(f"\n  {type_label}")
                print(f"    保留: {GREEN}{dup['keep'].name}{RESET}")
                action_str = "移入回收站" if to_trash else "删除"
                answer = input(f"    {action_str}: {RED}{rm_file.name}{RESET}? (y/N): ").strip().lower()
                if answer not in ("y", "yes"):
                    print(f"    ⏭ 已跳过")
                    continue

            try:
                file_size = rm_file.stat().st_size
                result = remove_file(rm_file, to_trash)
                deleted_count += 1
                freed_size += file_size
                action_icon = "🗑" if to_trash else "🗑"
                print(f"    {action_icon} {rm_file.name} — {result}")
            except Exception as e:
                print(f"    {RED}❌ 失败: {rm_file.name} — {e}{RESET}")

    # 最终报告
    action_word = "移入回收站" if to_trash else "删除"
    print(f"\n{'═' * 60}")
    print(f"{BOLD}✅ 完成!{RESET}")
    print(f"   已{action_word} {GREEN}{deleted_count}{RESET} 个文件")
    print(f"   释放空间 {GREEN}{format_filesize(freed_size)}{RESET}")
    if to_trash:
        print(f"   {DIM}(文件已移入各目录下的 {TRASH_DIR_NAME}/ 文件夹，确认无误后可手动清理){RESET}")
    print(f"{'═' * 60}\n")


def main():
    parser = argparse.ArgumentParser(
        description="🗑️  音乐文件去重工具 — 智能识别并清理同目录下的重复音频文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  %(prog)s /path/to/music                扫描目录（交互确认）
  %(prog)s /path/to/music -r             递归扫描子目录
  %(prog)s /path/to/music --dry-run      预览模式，只看不删
  %(prog)s /path/to/music -y             跳过确认直接删除
  %(prog)s /path/to/music --to-trash     移入回收站而不直接删除

去重规则:
  1. 精确重复: MD5 完全相同的文件，保留文件名最短的（原始文件）
  2. 文件名相似: 如 song.flac 和 song(1).flac，按音质保留最优
  3. 同歌不同格式: 如 song.mp3 和 song.flac，保留音质最高的格式
     格式优先级: FLAC > APE > WAV > M4A > OGG > OPUS > MP3 > WMA

注意:
  - 只处理同一目录下的重复，跨目录不处理
  - 建议先用 --dry-run 预览，确认无误后再正式执行
  - 使用 --to-trash 更安全，文件会移入同目录下的 .music_trash/ 文件夹
        """,
    )

    parser.add_argument("directory", help="要扫描的音乐目录路径")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="递归扫描子目录")
    parser.add_argument("--dry-run", action="store_true",
                        help="预览模式，只显示结果不删除")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="跳过确认，直接执行删除")
    parser.add_argument("--to-trash", action="store_true",
                        help="移入回收站(.music_trash/)而不直接删除")

    args = parser.parse_args()

    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"{RED}❌ 目录不存在: {directory}{RESET}")
        sys.exit(1)

    process_directory(
        directory,
        recursive=args.recursive,
        dry_run=args.dry_run,
        skip_confirm=args.yes,
        to_trash=args.to_trash,
    )


if __name__ == "__main__":
    main()
