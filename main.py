
"""Run the local web panel for the video processing pipeline."""

import os

from video_pipeline.webapp import serve


if __name__ == "__main__":
    serve(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
    )
