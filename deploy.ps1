git push
if ($LASTEXITCODE -ne 0) { throw "git push failed" }

ssh kevin@KevinMedia 'cd ~/game-night-bot && git pull --ff-only && venv/bin/pip install -q -r requirements.txt && sudo systemctl restart gamenightbot && systemctl is-active gamenightbot'
if ($LASTEXITCODE -ne 0) { throw "deploy failed" }