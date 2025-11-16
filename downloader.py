"""YouTube highest-quality downloader using yt-dlp.

This script downloads the best available video and audio streams for a given
YouTube URL and merges them into a single MP4 file using ffmpeg (required by
yt-dlp for muxing). The output is stored in the local ``downloads`` directory.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import yt_dlp

DOWNLOAD_DIR = Path(__file__).resolve().parent / "downloads"
ARIA2C_EXECUTABLE = shutil.which("aria2c")
FFMPEG_BINARY = shutil.which("ffmpeg")
FFPROBE_BINARY = shutil.which("ffprobe")

FAST_DOWNLOAD_OPTS: Dict[str, object] = {
    "concurrent_fragment_downloads": 10,
    "continuedl": True,
    "retries": 10,
    "fragment_retries": 10,
    "http_chunk_size": 10 * 1024 * 1024,  # 10MB chunks
}


def _find_winget_ffmpeg() -> Tuple[Optional[Path], Optional[Path]]:
    base = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if not base.exists():
        return None, None

    for package_dir in sorted(base.glob("Gyan.FFmpeg*"), reverse=True):
        for ffmpeg_path in package_dir.glob("ffmpeg*/bin/ffmpeg.exe"):
            ffmpeg_path = ffmpeg_path.resolve()
            ffprobe_path = ffmpeg_path.with_name("ffprobe.exe")
            if not ffprobe_path.exists():
                ffprobe_path = None
            return ffmpeg_path, ffprobe_path
    return None, None


def _resolve_external_tools() -> Tuple[Optional[Path], Optional[Path]]:
    ffmpeg_path = Path(FFMPEG_BINARY).resolve() if FFMPEG_BINARY else None
    ffprobe_path = Path(FFPROBE_BINARY).resolve() if FFPROBE_BINARY else None

    if ffmpeg_path and ffmpeg_path.exists():
        if not ffprobe_path:
            candidate = ffmpeg_path.with_name("ffprobe.exe")
            if candidate.exists():
                ffprobe_path = candidate
        return ffmpeg_path, ffprobe_path

    winget_ffmpeg, winget_ffprobe = _find_winget_ffmpeg()
    if winget_ffmpeg:
        return winget_ffmpeg, winget_ffprobe

    common_ffmpeg = Path("C:/ffmpeg/bin/ffmpeg.exe")
    if common_ffmpeg.exists():
        ffmpeg_path = common_ffmpeg.resolve()
        if not ffprobe_path:
            candidate = ffmpeg_path.with_name("ffprobe.exe")
            if candidate.exists():
                ffprobe_path = candidate
        return ffmpeg_path, ffprobe_path

    return None, ffprobe_path


RESOLVED_FFMPEG, RESOLVED_FFPROBE = _resolve_external_tools()


def _format_status(percent: Optional[str], speed: Optional[str], eta: Optional[str]) -> str:
    percent = (percent or "").strip()
    speed = (speed or "").strip()
    eta = (eta or "").strip()
    parts = []
    if percent:
        parts.append(percent)
    if speed:
        parts.append(speed)
    if eta and eta.upper() != "ETA UNKNOWN":
        parts.append(f"ETA {eta}")
    return " | ".join(parts)


def build_downloader(
    output_dir: Path,
    progress_callback: Optional[Callable[[str], None]] = None,
    *,
    format_selector: str = (
        "bv*[ext=mp4]+ba[ext=m4a]/"
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
        "best[ext=mp4]/best"
    ),
    merge_to_mp4: bool = True,
) -> yt_dlp.YoutubeDL:
    output_dir.mkdir(parents=True, exist_ok=True)

    def hook(status: Dict[str, str]) -> None:
        if status.get("status") == "downloading":
            message = _format_status(status.get("_percent_str"), status.get("_speed_str"), status.get("_eta_str"))
            if message and progress_callback:
                progress_callback(message)
        elif status.get("status") == "finished" and progress_callback:
            progress_callback("Download finished, processing...")

    ydl_opts: Dict[str, object] = {
        "format": format_selector,
        "noplaylist": True,
        "outtmpl": str(output_dir / "%(title)s.%(ext)s"),
        "progress_hooks": [hook],
        "quiet": False,
        "no_warnings": True,
        "no_color": True,
    }

    ydl_opts.update(FAST_DOWNLOAD_OPTS)

    if merge_to_mp4 and RESOLVED_FFMPEG:
        ydl_opts["merge_output_format"] = "mp4"
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            },
            {"key": "FFmpegMetadata"},
        ]
        ydl_opts["postprocessor_args"] = ["-movflags", "faststart"]

        ydl_opts["ffmpeg_location"] = str(RESOLVED_FFMPEG.parent)
        if RESOLVED_FFPROBE:
            ydl_opts["ffprobe_location"] = str(RESOLVED_FFPROBE.parent)

    if merge_to_mp4 and not RESOLVED_FFMPEG:
        raise FileNotFoundError(
            "ffmpeg executable not found. Install ffmpeg and ensure it is on PATH to merge high-quality streams."
        )

    if ARIA2C_EXECUTABLE:
        ydl_opts["external_downloader"] = "aria2c"
        ydl_opts["external_downloader_args"] = [
            "-x",
            "16",
            "-k",
            "1M",
            "-j",
            "16",
        ]

    return yt_dlp.YoutubeDL(ydl_opts)


def download_highest_quality(
    url: str,
    output_dir: Path = DOWNLOAD_DIR,
    progress_callback: Optional[Callable[[str], None]] = print,
) -> Path:
    last_report: Optional[str] = None

    def printer(message: str) -> None:
        nonlocal last_report
        if message != last_report:
            if progress_callback:
                progress_callback(message)
            last_report = message

    callback = printer if progress_callback else None

    try:
        with build_downloader(output_dir, callback) as ydl:
            info = ydl.extract_info(url, download=True)
            prepared = Path(ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadError as err:
        message = str(err)
        if "ffmpeg" not in message.lower():
            raise

        if progress_callback:
            progress_callback("ffmpeg not found; falling back to progressive download...")

        with build_downloader(
            output_dir,
            callback,
            format_selector="best[ext=mp4][acodec!=none][vcodec!=none]/best",
            merge_to_mp4=False,
        ) as ydl:
            info = ydl.extract_info(url, download=True)
            prepared = Path(ydl.prepare_filename(info))

    # Prefer the merged MP4 file if yt-dlp created one
    merged_candidate = prepared.with_suffix(".mp4")
    if merged_candidate.exists():
        return merged_candidate
    if prepared.exists():
        return prepared

    # Fallback: inspect requested downloads for existing files
    requested = info.get("requested_downloads") or []
    for item in requested:
        filepath = item.get("filepath")
        if filepath and Path(filepath).exists():
            return Path(filepath)

    raise FileNotFoundError(
        "Download finished, but the merged file could not be located. "
        "Ensure ffmpeg is installed and retry."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download YouTube videos in the highest available quality.")
    parser.add_argument("url", help="Full YouTube video URL to download")
    parser.add_argument(
        "--output",
        type=Path,
        default=DOWNLOAD_DIR,
        help="Directory where the downloaded file should be saved (default: ./downloads)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        final_path = download_highest_quality(args.url, args.output)
    except yt_dlp.utils.DownloadError as err:
        raise SystemExit(f"Download failed: {err}") from err
    except FileNotFoundError as err:
        raise SystemExit(str(err)) from err

    print(f"\nSaved highest-quality video to: {final_path}")


if __name__ == "__main__":
    main()
