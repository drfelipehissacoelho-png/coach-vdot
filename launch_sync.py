"""
launch_sync.py — Launcher sem caracteres especiais no caminho.
Coloque em C:\coach-vdot\ e chame via bat simples.
"""
import subprocess, sys, os

project_dir = u"C:\\Users\\drfel\\OneDrive\\Documents\\Claude\\Projects\\Coach VDOT \u2013 Running Trainer"
script = os.path.join(project_dir, "strava_sync.py")
result = subprocess.run([sys.executable, script, "--dias", "2"], cwd=project_dir,
                        env={**os.environ, "PYTHONUTF8": "1"})
sys.exit(result.returncode)
