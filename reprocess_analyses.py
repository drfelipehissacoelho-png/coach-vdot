#!/usr/bin/env python3
"""
reprocess_analyses.py
Reprocessa atividades já processadas para aplicar o novo pipeline
(Fase 0: clima + bloco METRICS + campos enriquecidos no analyses.json).

Uso:
    # Dry-run (padrão): lista o que seria feito, sem modificar nada
    python reprocess_analyses.py

    # Executa de fato
    python reprocess_analyses.py --execute

    # Reprocessa apenas a partir de uma data (inclusive)
    python reprocess_analyses.py --since 2026-04-16 --execute

    # Reprocessa um arquivo específico
    python reprocess_analyses.py --file 2026-04-17_Longo_18142036403.json --execute

    # Limita o número de arquivos (útil para testar)
    python reprocess_analyses.py --limit 3 --execute

Comportamento:
  - Lê cada arquivo em garmin_coach/data/processed/
  - Enriquecer com clima (quando outdoor)
  - Chama o Claude com o prompt novo (que pede bloco METRICS)
  - Atualiza garmin_coach/analyses/ (MD) e web/data/analyses.json
  - NÃO envia Telegram (reprocessamento é silencioso)
  - NÃO publica no Git (opcional, fazer manualmente depois)

Segurança:
  - Faz backup de web/data/analyses.json antes de qualquer escrita
  - Dry-run padrão (nunca modifica sem --execute)
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

# ── Setup de caminhos e logging ────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
GARMIN_DIR = BASE_DIR / "garmin_coach"
PROCESSED_DIR = GARMIN_DIR / "data" / "processed"
WEB_JSON_PATH = BASE_DIR / "web" / "data" / "analyses.json"

# Permite importar os módulos do garmin_coach
sys.path.insert(0, str(GARMIN_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("reprocess")


# ── Parsing de argumentos ──────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Reprocessa atividades aplicando o pipeline novo (Fase 0).",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="Executa de fato. Sem esta flag, só lista o que faria (dry-run).",
    )
    p.add_argument(
        "--since",
        type=str,
        default=None,
        help="Reprocessa apenas arquivos com data >= YYYY-MM-DD.",
    )
    p.add_argument(
        "--file",
        type=str,
        default=None,
        help="Reprocessa apenas o arquivo indicado (nome dentro de processed/).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Processa no máximo N arquivos (útil para testar).",
    )
    p.add_argument(
        "--no-weather",
        action="store_true",
        help="Pula a consulta ao clima (útil offline ou para não gastar cache).",
    )
    return p.parse_args()


# ── Filtros de arquivo ─────────────────────────────────────────

def iter_processed(args: argparse.Namespace) -> list[Path]:
    """
    Lista os arquivos processados em **ordem cronológica explícita**,
    respeitando --since / --file / --limit.

    Por que ordem cronológica importa (Prioridade 3 da auditoria):
        O matcher (session_matcher) consome estado prévio do mês para decidir
        se uma sessão é "extra" ou parte da prescrição. Se reprocessarmos
        fora de ordem (ex.: 2026-05-13 antes de 2026-05-12), o estado do
        matcher fica inconsistente — o treino de 13/05 toma decisões com
        base num estado que ainda não viu 12/05.

        Antes desta correção, a ordem dependia de `sorted(glob(...))` que
        é alfabético. Funciona POR COINCIDÊNCIA porque o prefixo é
        YYYY-MM-DD — mas qualquer arquivo sem o prefixo (legacy, manual,
        importação histórica) era inserido no lugar errado silenciosamente.

    Agora: chave de ordenação explícita (data parseada, nome do arquivo).
    Arquivos com data ilegível vão pro FINAL do batch e geram warning.
    """
    if not PROCESSED_DIR.exists():
        logger.error(f"Pasta não encontrada: {PROCESSED_DIR}")
        return []

    raw_files = list(PROCESSED_DIR.glob("*.fit")) + list(PROCESSED_DIR.glob("*.json"))

    # Ordem cronológica explícita.
    # Chave: (data, nome). Arquivos sem data parseável recebem date.max
    # para ir pro fim do batch — não para a primeira posição.
    from datetime import date as _date_t
    SENTINEL = _date_t.max
    all_files = sorted(
        raw_files,
        key=lambda p: (_date_from_name(p.name) or SENTINEL, p.name),
    )

    # Warn sobre arquivos sem data parseável
    sem_data = [f.name for f in all_files if _date_from_name(f.name) is None]
    if sem_data:
        logger.warning(
            f"{len(sem_data)} arquivo(s) sem prefixo YYYY-MM-DD — "
            f"processados por último, fora da ordem cronológica:"
        )
        for name in sem_data[:5]:
            logger.warning(f"    {name}")
        if len(sem_data) > 5:
            logger.warning(f"    ... e mais {len(sem_data) - 5}")

    if args.file:
        all_files = [f for f in all_files if f.name == args.file]

    if args.since:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            logger.error(f"--since inválido: {args.since} (use YYYY-MM-DD)")
            return []
        filtered: list[Path] = []
        for f in all_files:
            d = _date_from_name(f.name)
            if d is not None and d >= since:
                filtered.append(f)
        all_files = filtered

    if args.limit is not None:
        all_files = all_files[: args.limit]

    # Log do range cronológico que será processado (boa pista pra debug)
    if all_files:
        dates_parsed = [_date_from_name(f.name) for f in all_files]
        dates_valid  = [d for d in dates_parsed if d is not None]
        if dates_valid:
            logger.info(
                f"Range cronológico: {dates_valid[0].isoformat()} "
                f"→ {dates_valid[-1].isoformat()}  ({len(all_files)} arquivos)"
            )

    return all_files


def _date_from_name(name: str) -> "datetime.date | None":
    # Esperado: 2026-04-17_Longo_xxx.ext
    try:
        return datetime.strptime(name[:10], "%Y-%m-%d").date()
    except Exception:
        return None


# ── Backup ─────────────────────────────────────────────────────

def backup_web_json() -> Path | None:
    """Faz backup do analyses.json antes de modificar."""
    if not WEB_JSON_PATH.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = WEB_JSON_PATH.with_suffix(f".backup_{ts}.json")
    shutil.copy2(WEB_JSON_PATH, backup)
    logger.info(f"Backup criado: {backup.name}")
    return backup


# ── Execução de um arquivo ─────────────────────────────────────

def process_one(file_path: Path, execute: bool, skip_weather: bool) -> dict:
    """Retorna dict com o resultado do processamento (para o sumário final)."""
    result = {
        "file": file_path.name,
        "status": "dry_run",
        "error": None,
    }

    # 1. Ler arquivo
    try:
        ext = file_path.suffix.lower()
        if ext == ".fit":
            from src.fit_reader import read_fit_file  # type: ignore
            training_data = read_fit_file(file_path)
        elif ext == ".json":
            from src.strava_reader import read_strava_file  # type: ignore
            training_data = read_strava_file(file_path)
        else:
            result["status"] = "skipped"
            result["error"] = f"Formato não suportado: {ext}"
            return result
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"Leitura falhou: {e}"
        return result

    # 2. Enriquecer com clima
    if not skip_weather:
        try:
            sport        = (training_data.get("sport") or "run").lower()
            is_treadmill = training_data.get("is_treadmill", False)
            start_time   = training_data.get("start_time")
            if sport != "swim" and not is_treadmill and start_time:
                from src.weather_client import get_weather  # type: ignore
                w = get_weather(start_time)
                # Preferir a temperatura do open-meteo (ar real) à do sensor de
                # pulso do relógio, que lê alto e inflava o ajuste de calor.
                # Mesma regra do processor.py (caminho do daemon).
                if w.get("temperature_c") is not None:
                    training_data["avg_temperature_c"] = w["temperature_c"]
                training_data["humidity_pct"]   = w.get("humidity_pct")
                training_data["dew_point_c"]    = w.get("dew_point_c")
                training_data["heat_index_c"]   = w.get("heat_index_c")
                training_data["weather_source"] = w.get("source")
                logger.info(
                    f"  clima: T={w.get('temperature_c')}°C UR={w.get('humidity_pct')}% "
                    f"HI={w.get('heat_index_c')}°C [{w.get('source')}]"
                )
        except Exception as e:
            logger.warning(f"  clima falhou (seguindo sem): {e}")

    if not execute:
        result["status"] = "dry_run"
        return result

    # 3. Prompt + Claude
    try:
        from src.payload_builder import build_user_message, get_system_prompt  # type: ignore
        from src.claude_client import analyze_training  # type: ignore
        system_prompt = get_system_prompt(training_data)
        user_message  = build_user_message(training_data)
        logger.info("  chamando Claude...")
        analysis = analyze_training(system_prompt, user_message)
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"Claude falhou: {e}"
        return result

    # 4. Salvar histórico (MD + JSON)
    try:
        from src.history_manager import save_analysis_markdown, update_web_json  # type: ignore
        save_analysis_markdown(training_data, analysis)
        update_web_json(training_data, analysis)
        result["status"] = "ok"
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"Histórico falhou: {e}"

    return result


# ── Main ───────────────────────────────────────────────────────

def main() -> int:
    args = parse_args()

    # Sanity: .env precisa estar carregado para a Claude API
    if args.execute:
        try:
            from dotenv import load_dotenv  # type: ignore
            load_dotenv(GARMIN_DIR / ".env")
        except Exception:
            pass  # se não tiver python-dotenv, assume que as vars já estão no ambiente

    files = iter_processed(args)
    if not files:
        logger.warning("Nenhum arquivo encontrado com os filtros informados.")
        return 0

    mode = "EXECUÇÃO" if args.execute else "DRY-RUN"
    logger.info("=" * 60)
    logger.info(f"Modo: {mode}  |  Arquivos: {len(files)}")
    logger.info("=" * 60)

    if args.execute:
        backup_web_json()

    results = []
    for i, f in enumerate(files, 1):
        logger.info(f"[{i}/{len(files)}] {f.name}")
        r = process_one(f, execute=args.execute, skip_weather=args.no_weather)
        results.append(r)
        if r["error"]:
            logger.warning(f"  → {r['status'].upper()}: {r['error']}")
        else:
            logger.info(f"  → {r['status']}")

    # Sumário
    logger.info("=" * 60)
    logger.info("SUMÁRIO")
    logger.info("=" * 60)
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    for status, count in sorted(by_status.items()):
        logger.info(f"  {status:10s} {count}")

    errors = [r for r in results if r["status"] == "error"]
    if errors:
        logger.warning("Erros:")
        for r in errors:
            logger.warning(f"  - {r['file']}: {r['error']}")

    # Publica no GitHub/Vercel quando o reprocess realmente rodou
    # (o pipeline normal do processor.py já faz isso no fim de cada análise individual,
    #  mas o reprocess_analyses.py em lote precisa disparar uma única vez no final)
    if args.execute and any(r["status"] == "ok" for r in results):
        try:
            from src.web_publisher import publish_to_web  # type: ignore
            logger.info("")
            logger.info("📤 Publicando dashboard atualizado no GitHub/Vercel...")
            publish_to_web()
        except Exception as e:
            logger.warning(f"Publicação web falhou (rode manualmente): {e}")

    if not args.execute:
        logger.info("")
        logger.info("🔎 Dry-run concluído. Para executar de fato:")
        logger.info("   python reprocess_analyses.py --execute")

    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
