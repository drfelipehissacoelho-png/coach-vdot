"""
processor.py
Orquestra o pipeline completo para um arquivo de atividade.

Suporta:
  .fit  → lido pelo fit_reader  (Garmin)
  .json → lido pelo strava_reader (Strava)

Fluxo:
  1. Verifica duplicata (state_manager)
  2. Detecta tipo e lê o arquivo
  3. Monta prompt (payload_builder)
  4. Envia ao Claude (claude_client)
  5. Envia ao Telegram (telegram_client)
  6. Salva histórico (history_manager)
  7. Publica no GitHub/Vercel (web_publisher)
  8. Move para processed/
  9. Registra no state

Em caso de falha:
  - Claude falha → arquivo NÃO é movido (reprocessado depois)
  - Telegram falha → arquivo É movido (análise gerada, só notificação falhou)
  - Arquivo inválido → movido com sufixo _invalid
"""

import logging
import shutil
import time
from pathlib import Path

from src.payload_builder import build_user_message, get_system_prompt
from src.claude_client import analyze_training
from src.telegram_client import send_analysis
from src.state_manager import StateManager

logger = logging.getLogger("garmin_coach.processor")


def process_file(
    file_path: Path,
    processed_dir: Path,
    state_manager: StateManager,
    stable_wait: int = 3,
) -> bool:
    """
    Pipeline completo para .fit ou .json.
    Detecta o tipo automaticamente e usa o leitor correto.
    """

    # ── 0. Duplicata? ──────────────────────────────────────────
    if state_manager.is_processed(file_path):
        logger.info(f"Ignorado (ja processado): {file_path.name}")
        return True

    logger.info("=" * 55)
    logger.info(f"Novo arquivo: {file_path.name}")
    logger.info("=" * 55)

    # ── 1. Aguardar estabilidade ───────────────────────────────
    _wait_for_stable_file(file_path, wait=stable_wait)

    # ── 2. Ler arquivo (Garmin .fit ou Strava .json) ───────────
    ext = file_path.suffix.lower()
    try:
        if ext == ".fit":
            from src.fit_reader import read_fit_file
            training_data = read_fit_file(file_path)
        elif ext == ".json":
            from src.strava_reader import read_strava_file
            training_data = read_strava_file(file_path)
        else:
            logger.error(f"Formato nao suportado: {ext}")
            return False
    except ValueError as e:
        logger.error(f"Arquivo invalido: {e}")
        _move_file(file_path, processed_dir, suffix="_invalid")
        state_manager.mark_processed(file_path, {"error": str(e), "status": "invalid"})
        return False
    except Exception as e:
        logger.error(f"Erro ao ler arquivo: {e}")
        return False

    # ── 2.1. Normalizar métricas Stryd para FIT ───────────────────
    # fit_reader armazena potência como avg_power_w; todo o pipeline
    # downstream (stryd_analyzer, payload_builder, history_manager)
    # lê avg_watts. Adicionar alias aqui evita tocar em todos os consumidores.
    # attach_stryd_metrics era chamado apenas pelo strava_reader — agora
    # também chamado para FIT, calculando running_effectiveness, VI, etc.
    if ext == ".fit":
        if training_data.get("avg_power_w") is not None:
            training_data["avg_watts"] = training_data["avg_power_w"]
            training_data.setdefault("device_watts", True)
        try:
            from src.stryd_analyzer import attach_stryd_metrics
            attach_stryd_metrics(training_data)
            if training_data.get("avg_watts"):
                logger.info(
                    f"Stryd OK: {training_data['avg_watts']}W | "
                    f"RE={training_data.get('stryd_metrics', {}).get('running_effectiveness')} | "
                    f"VI={training_data.get('stryd_metrics', {}).get('power_variability_pct')}"
                )
        except Exception as _e:
            logger.warning(f"Stryd metrics indisponíveis (FIT): {_e}")

    # ── 2.2. Filtro de atividade mínima ───────────────────────────
    # Descarta testes curtos (configuração de sensor, GPS fix, etc.)
    # Limites: >= 5 minutos OU >= 0.5 km de distância
    MIN_DURATION_SEC = 5 * 60   # 5 minutos
    MIN_DISTANCE_KM  = 0.5

    # total_elapsed_time_s  → strava_reader
    # duration_seconds      → fit_reader (se existir)
    # fallback              → duration_formatted "HH:MM:SS" ou "MM:SS"
    _dur_sec = (
        training_data.get("total_elapsed_time_s")
        or training_data.get("duration_seconds")
        or 0
    )
    _dist_km = training_data.get("total_distance_km") or 0.0

    if _dur_sec == 0 and training_data.get("duration_formatted"):
        try:
            parts = training_data["duration_formatted"].split(":")
            if len(parts) == 2:
                _dur_sec = int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                _dur_sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except Exception:
            pass

    if _dur_sec < MIN_DURATION_SEC and _dist_km < MIN_DISTANCE_KM:
        logger.info(
            f"Ignorado (atividade muito curta): {file_path.name} "
            f"[{_dur_sec}s / {_dist_km:.2f}km — mínimo: {MIN_DURATION_SEC}s ou {MIN_DISTANCE_KM}km]"
        )
        _move_file(file_path, processed_dir, suffix="_short")
        state_manager.mark_processed(file_path, {
            "status": "skipped_short",
            "duration_sec": _dur_sec,
            "distance_km": _dist_km,
        })
        return True

    # ── 2.5. Enriquecer com clima (umidade + heat index) ───────
    # Só faz sentido para corrida outdoor — esteira e natação ignoram.
    try:
        sport          = (training_data.get("sport") or "run").lower()
        is_treadmill   = training_data.get("is_treadmill", False)
        # fit_reader e strava_reader expõem "start_time" em ISO 8601 (UTC do Garmin, local do Strava).
        # Fallback: "start_time_local" no formato "DD/MM/YYYY HH:MM".
        start_time_iso = training_data.get("start_time")
        if not start_time_iso:
            start_time_iso = _iso_from_local(training_data.get("start_time_local"))
        if sport != "swim" and not is_treadmill and start_time_iso:
            from src.weather_client import get_weather
            weather = get_weather(start_time_iso)
            # Preferir a temperatura do open-meteo (ar real) à do sensor de
            # pulso do relógio: o sensor lê alto por causa do calor corporal e
            # inflava o ajuste de calor. O dado do dispositivo vira fallback
            # quando o open-meteo não retorna temperatura.
            if weather.get("temperature_c") is not None:
                training_data["avg_temperature_c"] = weather["temperature_c"]
            training_data["humidity_pct"]   = weather.get("humidity_pct")
            training_data["dew_point_c"]    = weather.get("dew_point_c")
            training_data["heat_index_c"]   = weather.get("heat_index_c")
            training_data["weather_source"] = weather.get("source")
            logger.info(
                f"Clima: T={weather.get('temperature_c')}°C "
                f"UR={weather.get('humidity_pct')}% "
                f"HI={weather.get('heat_index_c')}°C "
                f"[{weather.get('source')}]"
            )
    except Exception as e:
        # Clima é opcional — nunca bloqueia o pipeline
        logger.warning(f"Falha ao buscar clima (continuando sem): {e}")

    # ── 3. Montar prompt ───────────────────────────────────────
    try:
        system_prompt = get_system_prompt(training_data)
        user_message  = build_user_message(training_data)
    except Exception as e:
        logger.error(f"Erro ao montar prompt: {e}")
        return False

    # ── 4. Claude API ──────────────────────────────────────────
    try:
        logger.info("Enviando para o Claude...")
        analysis = analyze_training(system_prompt, user_message)
        logger.info("Analise recebida.")
    except RuntimeError as e:
        logger.error(f"Falha na Claude API: {e}")
        logger.warning("Arquivo mantido em incoming/ para nova tentativa.")
        return False

    # ── 5. Telegram ────────────────────────────────────────────
    # IMPORTANTE: o Claude emite o bloco <!-- METRICS {...} --> ao final da
    # análise (consumido pelo history_manager). O Telegram com parse_mode=HTML
    # NÃO aceita comentários HTML e devolve 400. Removemos o bloco aqui antes
    # do envio. (Para o JSON / MD do dashboard, o history_manager já cuida disso.)
    try:
        from src.history_manager import _strip_metrics_block
        analysis_for_telegram = _strip_metrics_block(analysis)
        ok = send_analysis(analysis_for_telegram, training_data)
        if not ok:
            logger.warning("Telegram falhou, mas analise foi gerada. Continuando...")
    except Exception as e:
        logger.warning(f"Erro no Telegram: {e}. Continuando mesmo assim...")

    # ── 6. Salvar histórico ────────────────────────────────────
    try:
        from src.history_manager import save_analysis_markdown, update_web_json
        save_analysis_markdown(training_data, analysis)
        update_web_json(training_data, analysis)
        logger.info("Histórico salvo.")
    except Exception as e:
        logger.warning(f"Erro ao salvar histórico: {e}")

    # ── 7. Publicar no GitHub/Vercel ───────────────────────────
    try:
        from src.web_publisher import publish_to_web
        publish_to_web()
    except Exception as e:
        logger.warning(f"Erro ao publicar web: {e}")

    # ── 8. Mover para processed/ ───────────────────────────────
    dest = _move_file(file_path, processed_dir)

    # ── 7. Registrar no state ──────────────────────────────────
    state_manager.mark_processed(
        dest if dest else file_path,
        metadata={
            "source":       training_data.get("source", ext.lstrip(".")),
            "distance_km":  training_data.get("total_distance_km"),
            "duration":     training_data.get("duration_formatted"),
            "avg_pace":     training_data.get("avg_pace"),
            "avg_hr":       training_data.get("avg_heart_rate"),
            "status":       "success",
        },
    )

    logger.info(f"Pipeline concluido: {file_path.name}")
    logger.info(f"Total processados: {state_manager.total_processed()}")
    return True


# ── Manter compatibilidade com nome antigo ─────────────────────
def process_fit_file(file_path, processed_dir, state_manager, stable_wait=3):
    """Alias para compatibilidade com código existente."""
    return process_file(file_path, processed_dir, state_manager, stable_wait)


# ── Helpers ────────────────────────────────────────────────────

def _wait_for_stable_file(file_path: Path, wait: int = 3):
    if wait <= 0:
        return
    size_before = file_path.stat().st_size if file_path.exists() else 0
    time.sleep(wait)
    size_after = file_path.stat().st_size if file_path.exists() else 0
    if size_before != size_after:
        time.sleep(5)


def _iso_from_local(local_str: str | None) -> str | None:
    """
    Converte string 'DD/MM/YYYY HH:MM' (ou 'DD/MM/YYYY HH:MM:SS') em ISO 8601.
    Retorna None se não conseguir parsear.
    """
    if not local_str:
        return None
    from datetime import datetime as _dt
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return _dt.strptime(local_str, fmt).isoformat()
        except (ValueError, TypeError):
            continue
    return None


def _move_file(source: Path, dest_dir: Path, suffix: str = "") -> Path:
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        stem  = source.stem + suffix
        dest  = dest_dir / (stem + source.suffix)
        count = 1
        while dest.exists():
            dest = dest_dir / f"{stem}_{count}{source.suffix}"
            count += 1
        shutil.move(str(source), str(dest))
        logger.info(f"Arquivo movido: {dest.name}")
        return dest
    except Exception as e:
        logger.error(f"Erro ao mover {source.name}: {e}")
        return None
