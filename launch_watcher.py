"""
launch_watcher.py — Launcher sem caracteres especiais no caminho.
Coloque em C:\coach-vdot\ e chame via bat simples.
"""
import subprocess, sys, os

garmin_dir = u"C:\\Users\\drfel\\OneDrive\\Documents\\Claude\\Projects\\Coach VDOT \u2013 Running Trainer\\garmin_coach"
os.chdir(garmin_dir)
result = subprocess.run([sys.executable, "main.py"], cwd=garmin_dir)
sys.exit(result.returncode)
