#!/usr/bin/env bash
set -euo pipefail
git push
ssh kevin@KevinMedia 'cd ~/game-night-bot && git pull --ff-only && venv/bin/pip install -q -r requirements.txt && sudo systemctl restart gamenightbot && systemctl is-active gamenightbot'