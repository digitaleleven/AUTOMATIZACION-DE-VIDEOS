#!/usr/bin/env bash
set -euo pipefail

# No package install or provider call is needed: dependencies are pinned in
# requirements.txt and the integration tests use local fixtures/mocks.
python -m compileall -q main.py video_pipeline tests
node --check web/app.js
python -m unittest discover -s tests
