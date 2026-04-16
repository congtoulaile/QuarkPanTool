#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
音频文件元信息读取工具
支持格式: MP3, FLAC, OGG, M4A/AAC, WMA, WAV, AIFF, APE 等
依赖: pip install mutagen
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import timedelta

from mutagen import File as MutagenFile
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.oggvorbis import OggVorbis
from mutagen.mp4 import MP4
from mutagen.id3 import ID3

# 支持的音频扩展名
SUPPORTED_EXTENSIONS = {
    ".mp3", ".flac", ".ogg", ".oga", ".m4a", ".mp4", ".aac",
    ".wma", ".wav", ".aiff", ".aif", ".ape", ".opus", ".wv",
}


def format_duration(seconds):
    """将秒数格式化为 HH:MM:SS 或 MM:SS"""
    if seconds is None:
        return "未知"
    td = timedelta(seconds=int(seconds))
    total_seconds = int(td.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_bitrate(bitrate):
    """格式化比特率"""
    if bitrate is None:
        return "未知"
    return f"{bitrate // 1000} kbps"


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


def get_id3_tags(filepath):
    """从 MP3 文件读取 ID3 标签"""
    tags = {}
    try:
        id3 = ID3(filepath)
        tag_mapping = {
            "TIT2": "标题",
            "TPE1": "艺术家",
            "TALB": "专辑",
            "TDRC": "年份",
            "TRCK": "音轨号",
            "TCON": "流派",
            "TPE2": "专辑艺术家",
            "TCOM": "作曲家",
            "TPOS": "碟片号",
            "COMM": "备注",
            "TENC": "编码者",
            "TPUB": "出版商",
            "TCOP": "版权",
            "TLAN": "语言",
            "TBPM": "BPM",
            "TXXX": "自定义标签",
        }
        for id3_key, label in tag_mapping.items():
            # ID3 标签可能有多个同类型（如 COMM::eng）
            for key, value in id3.items():
                if key.startswith(id3_key):
                    text = str(value)
                    if text:
                        tags[label] = text
                        break

        # 检查是否有嵌入封面
        for key in id3.keys():
            if key.startswith("APIC"):
                apic = id3[key]
                tags["封面"] = f"有 ({apic.mime}, {format_filesize(len(apic.data))})"
                break
    except Exception:
        pass
    return tags


def get_vorbis_tags(audio):
    """从 FLAC/OGG 等文件读取 Vorbis Comment 标签"""
    tags = {}
    if audio.tags is None:
        return tags

    tag_mapping = {
        "title": "标题",
        "artist": "艺术家",
        "album": "专辑",
        "date": "年份",
        "tracknumber": "音轨号",
        "genre": "流派",
        "albumartist": "专辑艺术家",
        "composer": "作曲家",
        "discnumber": "碟片号",
        "comment": "备注",
        "encoder": "编码者",
        "publisher": "出版商",
        "copyright": "版权",
        "language": "语言",
        "bpm": "BPM",
        "lyrics": "歌词",
    }
    for vorbis_key, label in tag_mapping.items():
        values = audio.tags.get(vorbis_key)
        if values:
            tags[label] = "; ".join(values) if isinstance(values, list) else str(values)

    # FLAC 封面
    if isinstance(audio, FLAC) and audio.pictures:
        pic = audio.pictures[0]
        tags["封面"] = f"有 ({pic.mime}, {format_filesize(len(pic.data))})"

    return tags


def get_mp4_tags(audio):
    """从 M4A/MP4 文件读取标签"""
    tags = {}
    if audio.tags is None:
        return tags

    tag_mapping = {
        "\xa9nam": "标题",
        "\xa9ART": "艺术家",
        "\xa9alb": "专辑",
        "\xa9day": "年份",
        "trkn": "音轨号",
        "\xa9gen": "流派",
        "aART": "专辑艺术家",
        "\xa9wrt": "作曲家",
        "disk": "碟片号",
        "\xa9cmt": "备注",
        "\xa9too": "编码工具",
        "cprt": "版权",
        "tmpo": "BPM",
        "\xa9lyr": "歌词",
    }
    for mp4_key, label in tag_mapping.items():
        values = audio.tags.get(mp4_key)
        if values:
            if isinstance(values, list) and len(values) > 0:
                val = values[0]
                # trkn 和 disk 是 tuple: (current, total)
                if isinstance(val, tuple):
                    tags[label] = f"{val[0]}/{val[1]}" if val[1] else str(val[0])
                else:
                    tags[label] = str(val)
            else:
                tags[label] = str(values)

    # 封面
    covr = audio.tags.get("covr")
    if covr:
        tags["封面"] = f"有 ({format_filesize(len(covr[0]))})"

    return tags


def get_generic_tags(audio):
    """通用标签读取（兜底方案）"""
    tags = {}
    if audio.tags is None:
        return tags
    for key, value in audio.tags.items():
        tags[str(key)] = str(value)
    return tags


def read_audio_meta(filepath):
    """
    读取音频文件的元信息

    Args:
        filepath: 音频文件路径

    Returns:
        dict: 包含文件信息和标签信息的字典
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"文件不存在: {filepath}")

    ext = filepath.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"不支持的文件格式: {ext}")

    audio = MutagenFile(str(filepath))
    if audio is None:
        raise ValueError(f"无法解析音频文件: {filepath}")

    # ---------- 基本文件信息 ----------
    file_info = {
        "文件名": filepath.name,
        "文件路径": str(filepath.resolve()),
        "文件大小": format_filesize(filepath.stat().st_size),
        "文件格式": ext.lstrip(".").upper(),
    }

    # ---------- 音频流信息 ----------
    stream_info = {}
    info = audio.info
    if info:
        if hasattr(info, "length") and info.length:
            stream_info["时长"] = format_duration(info.length)
            stream_info["时长(秒)"] = round(info.length, 2)
        if hasattr(info, "bitrate") and info.bitrate:
            stream_info["比特率"] = format_bitrate(info.bitrate)
        if hasattr(info, "sample_rate") and info.sample_rate:
            stream_info["采样率"] = f"{info.sample_rate} Hz"
        if hasattr(info, "channels") and info.channels:
            stream_info["声道数"] = info.channels
        if hasattr(info, "bits_per_sample") and info.bits_per_sample:
            stream_info["位深度"] = f"{info.bits_per_sample} bit"
        # MP3 特有
        if hasattr(info, "mode"):
            mode_map = {0: "Stereo", 1: "Joint Stereo", 2: "Dual Channel", 3: "Mono"}
            stream_info["声道模式"] = mode_map.get(info.mode, str(info.mode))
        if hasattr(info, "encoder_info") and info.encoder_info:
            stream_info["编码器"] = info.encoder_info

    # ---------- 标签信息 ----------
    if isinstance(audio, MP3):
        tag_info = get_id3_tags(str(filepath))
    elif isinstance(audio, (FLAC, OggVorbis)):
        tag_info = get_vorbis_tags(audio)
    elif isinstance(audio, MP4):
        tag_info = get_mp4_tags(audio)
    else:
        tag_info = get_generic_tags(audio)

    return {
        "file_info": file_info,
        "stream_info": stream_info,
        "tag_info": tag_info,
    }


def print_meta(meta, use_color=True):
    """美观地打印元信息"""
    # 颜色代码
    if use_color:
        CYAN = "\033[36m"
        GREEN = "\033[32m"
        YELLOW = "\033[33m"
        BOLD = "\033[1m"
        RESET = "\033[0m"
    else:
        CYAN = GREEN = YELLOW = BOLD = RESET = ""

    separator = "─" * 50

    print(f"\n{CYAN}{separator}{RESET}")
    print(f"{BOLD}📄 文件信息{RESET}")
    print(f"{CYAN}{separator}{RESET}")
    for key, value in meta["file_info"].items():
        print(f"  {GREEN}{key:　<8}{RESET}: {value}")

    if meta["stream_info"]:
        print(f"\n{CYAN}{separator}{RESET}")
        print(f"{BOLD}🎵 音频流信息{RESET}")
        print(f"{CYAN}{separator}{RESET}")
        for key, value in meta["stream_info"].items():
            print(f"  {GREEN}{key:　<8}{RESET}: {value}")

    if meta["tag_info"]:
        print(f"\n{CYAN}{separator}{RESET}")
        print(f"{BOLD}🏷️  标签信息{RESET}")
        print(f"{CYAN}{separator}{RESET}")
        for key, value in meta["tag_info"].items():
            # 过长的内容截断显示
            display_value = str(value)
            if len(display_value) > 100:
                display_value = display_value[:100] + "..."
            print(f"  {YELLOW}{key:　<8}{RESET}: {display_value}")
    else:
        print(f"\n  ⚠️  未找到标签信息")

    print(f"{CYAN}{separator}{RESET}\n")


def extract_cover(filepath, output_path=None):
    """
    提取音频文件中的封面图片

    Args:
        filepath: 音频文件路径
        output_path: 输出路径，默认为同目录下的 cover.jpg
    """
    filepath = Path(filepath)
    audio = MutagenFile(str(filepath))

    cover_data = None
    mime = "image/jpeg"

    if isinstance(audio, MP3):
        try:
            id3 = ID3(str(filepath))
            for key in id3.keys():
                if key.startswith("APIC"):
                    cover_data = id3[key].data
                    mime = id3[key].mime
                    break
        except Exception:
            pass
    elif isinstance(audio, FLAC):
        if audio.pictures:
            cover_data = audio.pictures[0].data
            mime = audio.pictures[0].mime
    elif isinstance(audio, MP4):
        covr = audio.tags.get("covr") if audio.tags else None
        if covr:
            cover_data = bytes(covr[0])

    if cover_data is None:
        print("❌ 未找到封面图片")
        return None

    # 确定输出路径和扩展名
    ext = ".jpg"
    if "png" in mime:
        ext = ".png"
    elif "gif" in mime:
        ext = ".gif"

    if output_path is None:
        output_path = filepath.parent / f"{filepath.stem}_cover{ext}"
    else:
        output_path = Path(output_path)

    with open(output_path, "wb") as f:
        f.write(cover_data)

    print(f"✅ 封面已保存到: {output_path} ({format_filesize(len(cover_data))})")
    return str(output_path)


def scan_directory(directory, recursive=False):
    """
    扫描目录中的所有音频文件

    Args:
        directory: 目录路径
        recursive: 是否递归扫描子目录
    """
    directory = Path(directory)
    if not directory.is_dir():
        print(f"❌ 目录不存在: {directory}")
        return

    pattern = "**/*" if recursive else "*"
    audio_files = sorted(
        f for f in directory.glob(pattern)
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    if not audio_files:
        print(f"⚠️  未在 {directory} 中找到音频文件")
        return

    print(f"\n🔍 找到 {len(audio_files)} 个音频文件:\n")

    results = []
    for i, fpath in enumerate(audio_files, 1):
        try:
            meta = read_audio_meta(fpath)
            tag = meta["tag_info"]
            stream = meta["stream_info"]

            title = tag.get("标题", fpath.stem)
            artist = tag.get("艺术家", "未知")
            album = tag.get("专辑", "未知")
            duration = stream.get("时长", "未知")
            bitrate = stream.get("比特率", "未知")
            fmt = meta["file_info"]["文件格式"]

            print(f"  {i:3d}. [{fmt:4s}] {title} - {artist} | 专辑: {album} | {duration} | {bitrate}")
            results.append(meta)
        except Exception as e:
            print(f"  {i:3d}. ❌ {fpath.name}: {e}")

    print(f"\n共扫描 {len(audio_files)} 个文件，成功读取 {len(results)} 个\n")
    return results


def export_to_json(meta_list, output_path):
    """将元信息导出为 JSON 文件"""
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(meta_list, f, ensure_ascii=False, indent=2)
    print(f"✅ 已导出到: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="🎵 音频文件元信息读取工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  %(prog)s song.mp3                     读取单个文件的元信息
  %(prog)s song.mp3 song.flac           读取多个文件
  %(prog)s -d /path/to/music            扫描目录
  %(prog)s -d /path/to/music -r         递归扫描目录
  %(prog)s song.mp3 --cover             提取封面图片
  %(prog)s song.mp3 --json              以 JSON 格式输出
  %(prog)s -d /path/to/music --export result.json  导出为 JSON 文件
        """,
    )

    parser.add_argument("files", nargs="*", help="要读取的音频文件路径")
    parser.add_argument("-d", "--dir", help="扫描指定目录中的音频文件")
    parser.add_argument("-r", "--recursive", action="store_true", help="递归扫描子目录")
    parser.add_argument("--cover", action="store_true", help="提取封面图片")
    parser.add_argument("--cover-output", help="封面图片输出路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    parser.add_argument("--export", help="导出元信息到 JSON 文件")
    parser.add_argument("--no-color", action="store_true", help="禁用彩色输出")

    args = parser.parse_args()

    # 如果没有提供任何参数，显示帮助
    if not args.files and not args.dir:
        parser.print_help()
        sys.exit(0)

    use_color = not args.no_color

    # 扫描目录模式
    if args.dir:
        results = scan_directory(args.dir, args.recursive)
        if results and args.export:
            export_to_json(results, args.export)
        return

    # 单文件 / 多文件模式
    all_meta = []
    for filepath in args.files:
        try:
            meta = read_audio_meta(filepath)
            all_meta.append(meta)

            if args.json:
                print(json.dumps(meta, ensure_ascii=False, indent=2))
            else:
                print_meta(meta, use_color=use_color)

            if args.cover:
                extract_cover(filepath, args.cover_output)

        except FileNotFoundError as e:
            print(f"❌ {e}")
        except ValueError as e:
            print(f"❌ {e}")
        except Exception as e:
            print(f"❌ 读取失败 [{filepath}]: {e}")

    # 导出 JSON
    if args.export and all_meta:
        export_to_json(all_meta, args.export)


if __name__ == "__main__":
    main()
