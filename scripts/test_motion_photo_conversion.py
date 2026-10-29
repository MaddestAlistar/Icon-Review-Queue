#!/usr/bin/env python3
"""Self-contained CI regression: generate a real MP4 appended to a JPEG and convert."""
import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("motion", Path(__file__).with_name("convert_motion_photos.py"))
motion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(motion)


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)


with tempfile.TemporaryDirectory() as folder:
    root = Path(folder)
    pending = root / "pending"
    pending.mkdir()
    still = root / "still.jpg"
    movie = root / "video.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=720x720", "-frames:v", "1", str(still))
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x320:rate=12:duration=1.5",
           "-c:v", "mpeg4", "-q:v", "8", "-movflags", "+faststart", str(movie))
    source = still.read_bytes() + movie.read_bytes()
    generated = motion.convert(source)
    assert generated.startswith(b"GIF89a") or generated.startswith(b"GIF87a")
    assert len(generated) <= motion.MAX_OUTPUT
    try:
        motion.embedded_video(still.read_bytes())
        raise AssertionError("Static JPEG must not be treated as animated")
    except ValueError:
        pass
    original_root, original_pending = motion.ROOT, motion.PENDING
    try:
        motion.ROOT, motion.PENDING = root, pending
        source_name = "pending/test-20261030000000000-abc12345.jpg"
        (root / source_name).write_bytes(source)
        meta_path = pending / "test-20261030000000000-abc12345.json"
        meta_path.write_text(json.dumps({
            "id": "20261030000000000-abc12345",
            "name": "Test Motion", "kind": "动态", "status": "pending",
            "filename": source_name, "sourceFilename": source_name,
            "sourceFormat": "motion-jpeg", "motionSource": "embedded-video",
            "conversionStatus": "processing", "bytes": len(source),
            "width": 720, "height": 720
        }), "utf-8")
        assert motion.process(meta_path)
        saved = json.loads(meta_path.read_text("utf-8"))
        assert saved["conversionStatus"] == "ready"
        assert saved["filename"].endswith(".gif")
        assert (root / saved["filename"]).read_bytes() == generated
        assert not motion.process(meta_path), "Already converted file must be ignored"

        # Apple Photos "Save as Video" output: original MOV without any JPEG.
        video_source = "pending/test-20261030000000001-abc12345.mov"
        (root / video_source).write_bytes(movie.read_bytes())
        direct = motion.convert(movie.read_bytes(), "livephoto-video")
        assert direct.startswith((b"GIF89a", b"GIF87a"))
        video_meta = pending / "test-20261030000000001-abc12345.json"
        video_meta.write_text(json.dumps({
            "id": "20261030000000001-abc12345",
            "name": "Apple Live Video", "kind": "动态", "status": "pending",
            "filename": video_source, "sourceFilename": video_source,
            "sourceFormat": "livephoto-video", "motionSource": "uploaded-video",
            "conversionStatus": "processing", "bytes": movie.stat().st_size,
            "width": 0, "height": 0
        }), "utf-8")
        assert motion.process(video_meta)
        saved_video = json.loads(video_meta.read_text("utf-8"))
        assert saved_video["conversionStatus"] == "ready"
        assert (root / saved_video["filename"]).read_bytes() == direct
        assert not motion.process(video_meta)
    finally:
        motion.ROOT, motion.PENDING = original_root, original_pending

print("LIVEPHOTO_CONVERSION_OK: real embedded MP4, verified animated GIF, status ready")
