#!/usr/bin/env python3
"""Convert verified embedded-video JPEG submissions into reviewable loop GIFs.

Runs on GitHub Actions in the public REVIEW queue. Never trusts an XMP flag,
a user-controlled source path, or the filename alone. A successful conversion
updates the metadata + new GIF together in a single git commit.
"""
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PENDING = ROOT / "pending"
MAX_BYTES = 10 * 1024 * 1024
MAX_OUTPUT = 10 * 1024 * 1024
SAFE_SOURCE = re.compile(r"^pending/[A-Za-z0-9_\-\u4e00-\u9fff]+\.(?:jpe?g)$")
SAFE_META = re.compile(r"^pending/[A-Za-z0-9_\-\u4e00-\u9fff]+\.json$")
BRANDS = {b"isom", b"iso2", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"qt  ", b"M4V ", b"MSNV", b"3gp4", b"3gp5", b"3gp6", b"3gp7"}


def embedded_video(data: bytes) -> bytes:
    if not data.startswith(b"\xff\xd8") or len(data) > MAX_BYTES:
        raise ValueError("JPG 图片无效或超过 10 MB")
    last_eoi = -1
    for pos in range(2, len(data) - 16):
        if data[pos:pos + 2] == b"\xff\xd9":
            last_eoi = pos + 2
        if data[pos:pos + 4] != b"ftyp":
            continue
        begin = pos - 4
        if last_eoi < 0 or not 0 <= begin - last_eoi <= 131072 or begin < 16:
            continue
        size = int.from_bytes(data[begin:begin + 4], "big")
        if size < 16 or size > 4096 or begin + size > len(data):
            continue
        brand = data[pos + 4:pos + 8]
        if brand not in BRANDS:
            continue
        tail = data[begin:]
        if len(tail) < 64 or not (b"mdat" in tail or b"moov" in tail):
            continue
        return tail
    raise ValueError("未检测到有效的内嵌视频")


def run(args, timeout=60):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=timeout)


def probe_video(src: Path):
    result = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                  "-show_entries", "stream=codec_type,width,height:format=duration",
                  "-of", "json", str(src)], timeout=15)
    info = json.loads(result.stdout)
    tracks = info.get("streams", [])
    if not tracks or int(tracks[0].get("width") or 0) < 32 or int(tracks[0].get("height") or 0) < 32:
        raise ValueError("内嵌视频没有有效的视频轨道")
    duration = float(info.get("format", {}).get("duration") or 0)
    if duration <= 0 or duration > 180:
        raise ValueError("无法确认视频时长或时长异常")


def convert(source: bytes) -> bytes:
    movie = embedded_video(source)
    with tempfile.TemporaryDirectory(prefix="liangyou-motion-") as tmp:
        src = Path(tmp) / "embedded.mov"
        dst = Path(tmp) / "output.gif"
        src.write_bytes(movie)
        probe_video(src)
        filters = (
            "fps=12,scale=320:320:force_original_aspect_ratio=decrease:flags=lanczos,"
            "pad=320:320:(ow-iw)/2:(oh-ih)/2:color=black,split[a][b];"
            "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5"
        )
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-threads", "2", "-i", str(src), "-t", "3", "-filter_complex", filters,
             "-loop", "0", "-y", str(dst)], timeout=75)
        if not dst.is_file() or not 200 <= dst.stat().st_size <= MAX_OUTPUT:
            raise ValueError("生成的 GIF 大小不符合要求")
        generated = dst.read_bytes()
        if not generated.startswith((b"GIF87a", b"GIF89a")) or (
            int.from_bytes(generated[6:8], "little"),
            int.from_bytes(generated[8:10], "little")
        ) != (320, 320):
            raise ValueError("生成的 GIF 格式或尺寸不正确")
        frames = run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                      "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(dst)], timeout=20)
        try:
            if int(frames.stdout.strip()) < 2:
                raise ValueError("视频不足两帧，不能作为动态图标")
        except (TypeError, ValueError):
            raise ValueError("无法确认 GIF 有效动画帧")
        return generated


def process(meta_path: Path, retry_failed=False):
    if not SAFE_META.fullmatch(meta_path.relative_to(ROOT).as_posix()):
        return False
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
    except (ValueError, OSError):
        return False
    if (meta.get("motionSource") != "embedded-video"
            or meta.get("sourceFormat") != "motion-jpeg"
            or meta.get("kind") != "动态"
            or meta.get("status") != "pending"
            or meta.get("conversionStatus") not in (["processing", "failed"] if retry_failed else ["processing"])):
        return False
    source_name = str(meta.get("sourceFilename") or "")
    if not SAFE_SOURCE.fullmatch(source_name):
        return False
    source = ROOT / source_name
    if not source.is_file() or source.stat().st_size > MAX_BYTES or source.stat().st_size == 0:
        return False
    if meta.get("filename") != source_name:
        return False
    output = ROOT / (source_name.rsplit(".", 1)[0] + ".gif")
    try:
        generated = convert(source.read_bytes())
        output.write_bytes(generated)
        meta.update(
            filename=output.relative_to(ROOT).as_posix(),
            bytes=len(generated),
            width=320,
            height=320,
            generatedGifUrl="https://raw.githubusercontent.com/MaddestAlistar/Icon-Review-Queue/main/"
            + output.relative_to(ROOT).as_posix(),
            conversionStatus="ready",
            convertedAt=datetime.now(timezone.utc).isoformat(),
        )
        meta.pop("conversionError", None)
        print(f"Converted {meta_path.name}: GIF {len(generated)} bytes")
    except (ValueError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        # Never expose ffmpeg's untrusted output or machine paths in the public metadata.
        meta["conversionStatus"] = "failed"
        meta["conversionError"] = (
            str(exc) if isinstance(exc, ValueError) else "视频解码或转换失败；请更换素材"
        )[:120]
        meta["convertedAt"] = datetime.now(timezone.utc).isoformat()
        print(f"Could not convert {meta_path.name}: {meta['conversionError']}")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return True


def main():
    retry_failed = os.getenv("RETRY_FAILED", "false").lower() == "true"
    changed = 0
    if PENDING.is_dir():
        for meta in sorted(PENDING.glob("*.json")):
            if process(meta, retry_failed):
                changed += 1
    print(f"Live Photo conversion attempts: {changed}")


if __name__ == "__main__":
    main()
