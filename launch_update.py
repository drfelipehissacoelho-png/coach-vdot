"""
launch_update.py — Copia o index.html atualizado para o repo GitHub (C:\coach-vdot).
Coloque este arquivo em C:\coach-vdot\ junto com update_dashboard.bat
"""
import shutil
import sys
from pathlib import Path

SRC = Path(u"C:\\Users\\drfel\\OneDrive\\Documents\\Claude\\Projects\\Coach VDOT \u2013 Running Trainer\\web\\index.html")
DST = Path(u"C:\\coach-vdot\\index.html")

print("=" * 50)
print("  Coach VDOT - Atualizar Dashboard")
print("=" * 50 + "\n")

if not SRC.exists():
    print(f"ERRO: Arquivo de origem nao encontrado:")
    print(f"  {SRC}")
    input("\nPressione Enter para fechar...")
    sys.exit(1)

try:
    shutil.copy2(str(SRC), str(DST))
    print(f"OK  index.html copiado com sucesso!")
    print(f"\n  De:   {SRC}")
    print(f"  Para: {DST}")
    print("\nProximo passo: abra o GitHub Desktop,")
    print("escreva uma mensagem de commit e faca o Push.")
except Exception as e:
    print(f"ERRO ao copiar: {e}")
    input("\nPressione Enter para fechar...")
    sys.exit(1)

input("\nPressione Enter para fechar...")
