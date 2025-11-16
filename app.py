"""Flask web application exposing the highest-quality YouTube downloader."""

from __future__ import annotations

import time
from pathlib import Path
from threading import Thread
from typing import BinaryIO, Tuple

from flask import Flask, Response, abort, request, send_file, send_from_directory

from downloader import DOWNLOAD_DIR, download_highest_quality

BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__)


def _cleanup_downloads_background(interval_seconds: int = 300) -> None:
    def _worker() -> None:
        while True:
            time.sleep(interval_seconds)
            for file_path in DOWNLOAD_DIR.glob("*"):
                if not file_path.is_file():
                    continue
                age = time.time() - file_path.stat().st_mtime
                if age < interval_seconds:
                    continue
                try:
                    file_path.unlink()
                except OSError:
                    app.logger.exception("Background cleanup failed for %s", file_path)

    Thread(target=_worker, daemon=True).start()


_cleanup_downloads_background()


@app.route("/", methods=["GET"])
def index() -> Tuple[str, int]:
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/download", methods=["POST"])
def download_video() -> Response:
    url = (request.form.get("url") or "").strip()
    if not url:
        abort(400, "Please provide a valid YouTube URL.")

    try:
        video_path = download_highest_quality(url, DOWNLOAD_DIR, progress_callback=app.logger.info)
    except Exception as exc:  # Catch yt_dlp and IO errors and present a clean response
        app.logger.exception("Video download failed")
        abort(502, f"Failed to download video: {exc}")

    file_handle: BinaryIO = video_path.open("rb")

    def remove_file() -> None:
        def _cleanup() -> None:
            try:
                file_handle.close()
            except OSError:
                app.logger.exception("Failed to close file handle for %s", video_path)
            time.sleep(1)
            for attempt in range(5):
                try:
                    video_path.unlink()
                except FileNotFoundError:
                    return
                except OSError:
                    if attempt == 4:
                        app.logger.exception("Failed to delete downloaded file %s", video_path)
                        return
                    time.sleep(0.5)
                else:
                    app.logger.info("Deleted downloaded file %s", video_path)
                    return

        Thread(target=_cleanup, daemon=True).start()

    try:
        response = send_file(file_handle, as_attachment=True, download_name=video_path.name)
    except Exception:
        file_handle.close()
        raise
    response.call_on_close(remove_file)
    return response


if __name__ == "__main__":
    app.run(debug=True)
