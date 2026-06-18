"""
fit_reader.py
Lê e extrai dados relevantes de um arquivo .fit do Garmin.

Retorna um dicionário estruturado com:
- Dados gerais da sessão (distância, duração, pace, FC)
- Splits por volta (laps)
- Dados ambientais (temperatura)
- Indicadores de treino (TSS, training effect)

Todos os campos são opcionais: se o relógio não gravou, vira None.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("garmin_coach.fit_reader")


# ── Helpers de conversão ───────────────────────────────────────

def _seconds_to_hms(seconds: Optional[float]) -> str:
    """Converte segundos para formato legível HH:MM:SS."""
    if seconds is None:
        return "N/D"
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"


def _speed_to_pace(speed_ms: Optional[float]) -> str:
    """Converte velocidade em m/s para pace em min/km (ex: '5:32/km')."""
    if not speed_ms or speed_ms <= 0:
        return "N/D"
    pace_total_seconds = 1000 / speed_ms          # segundos por km
    pace_min = int(pace_total_seconds // 60)
    pace_sec = int(pace_total_seconds % 60)
    return f"{pace_min}:{pace_sec:02d}/km"


def _safe_get(record, field: str):
    """Extrai valor de um campo fitparse sem lançar exceção."""
    try:
        data = record.get_value(field)
        return data
    except Exception:
        return None


# ── Parser principal ───────────────────────────────────────────

def read_fit_file(file_path: Path) -> dict:
    """
    Parseia um arquivo .fit e retorna um dicionário com todos os dados do treino.
    Lança ValueError se o arquivo não for reconhecido como atividade de corrida válida.
    """
    try:
        from fitparse import FitFile
    except ImportError:
        raise RuntimeError("fitparse não instalado. Execute: pip install fitparse")

    logger.info(f"Lendo arquivo: {file_path.name}")

    try:
        fitfile = FitFile(str(file_path))
        # Força o parse completo logo no início para pegar erros cedo
        fitfile.parse()
    except Exception as e:
        raise ValueError(f"Arquivo .fit inválido ou corrompido: {e}")

    result = {
        "filename": file_path.name,
        # ── Dados da sessão ──────────────────────────────────
        "sport": None,
        "sub_sport": None,
        "is_treadmill": False,
        "start_time": None,
        "start_time_local": None,
        "total_distance_km": None,
        "total_timer_time_s": None,        # tempo em movimento
        "total_elapsed_time_s": None,      # tempo total (com pausas)
        "duration_formatted": None,
        "avg_pace": None,
        "max_pace": None,
        "avg_speed_ms": None,
        # ── Frequência cardíaca ──────────────────────────────
        "avg_heart_rate": None,
        "max_heart_rate": None,
        # ── Cadência ─────────────────────────────────────────
        "avg_cadence_spm": None,           # passadas por minuto (× 2 do raw Garmin)
        "max_cadence_spm": None,
        # ── Outros métricas ───────────────────────────────────
        "total_calories": None,
        "total_ascent_m": None,
        "total_descent_m": None,
        "avg_temperature_c": None,
        "max_temperature_c": None,
        # ── Indicadores de treino ─────────────────────────────
        "training_stress_score": None,
        "aerobic_training_effect": None,
        "anaerobic_training_effect": None,
        # ── Running Dynamics ─────────────────────────────────
        # Preenchido quando Stryd ou HRM-Pro estiver ativo.
        # None quando sensor ausente — pipeline trata graciosamente.
        "avg_ground_contact_time_ms":  None,   # ms — alvo elite ~190ms
        "avg_vertical_oscillation_cm": None,   # cm — alvo 6–9
        "avg_vertical_ratio_pct":      None,   # % — alvo <8
        "avg_stride_length_m":         None,   # m
        "avg_gct_balance_pct":         None,   # % — 50% = simétrico
        # ── Stryd Power & métricas avançadas ─────────────────
        "avg_power_w":              None,   # W — potência de corrida
        "avg_form_power_w":         None,   # W — custo postural (alvo <20% do total)
        "avg_air_power_w":          None,   # W — resistência do ar
        "avg_leg_spring_stiffness": None,   # kN/m — rigidez do tendão
        "avg_impact_loading_rate":  None,   # N/s — taxa de impacto
        # ── Frequência respiratória ───────────────────────────
        "avg_respiration_rate":     None,   # breaths/min (campo FIT 108 / 100)
        # ── Splits (laps) ─────────────────────────────────────
        "laps": [],
    }

    # ── 0. Provider ID (Prioridade 5) ──────────────────────────
    # Extrai um identificador estável do bloco file_id. A combinação
    # (serial_number, time_created) é única por device por gravação —
    # tem maior chance de unicidade que qualquer slug de nome. Quando
    # ambos faltarem, ficamos sem provider_activity_id e o fallback
    # determinístico de activity_id.make_stable_id assume.
    result["source"] = "garmin"
    result["provider"] = "garmin"
    result["provider_activity_id"] = None
    try:
        for fid in fitfile.get_messages("file_id"):
            fid_data = {d.name: d.value for d in fid}
            tc = fid_data.get("time_created")
            sn = fid_data.get("serial_number")
            tc_str = (
                tc.strftime("%Y%m%d%H%M%S") if hasattr(tc, "strftime") else str(tc or "")
            )
            sn_str = str(sn or "")
            if tc_str or sn_str:
                # Formato: "<serial>-<timestamp>" — compacto e estável.
                result["provider_activity_id"] = f"{sn_str}-{tc_str}".strip("-")
            break  # só o primeiro file_id basta
    except Exception as e:
        logger.debug(f"file_id não disponível: {e}")

    # ── 1. Extrair dados da sessão ─────────────────────────────
    for session in fitfile.get_messages("session"):
        data = {d.name: d.value for d in session}

        result["sport"]      = str(data.get("sport", "running")).lower()
        result["sub_sport"]  = str(data.get("sub_sport", "generic")).lower()

        # Tempo de início.
        # O FIT grava o timestamp em UTC. Convertemos para o fuso local do
        # atleta (Recife = UTC-3, sem horário de verão) para que a narrativa e
        # a busca de clima usem a hora REAL do treino. Sem isso, 04:45 local
        # virava 07:45 UTC -> hora errada na análise e clima do horário errado
        # (pós-nascer-do-sol, mais quente) inflando o ajuste de calor.
        # Offset configurável via .env (ATHLETE_UTC_OFFSET); default -3.
        start = data.get("start_time")
        if start:
            if hasattr(start, "astimezone"):
                try:
                    _off_h = float(os.getenv("ATHLETE_UTC_OFFSET", "-3"))
                except (TypeError, ValueError):
                    _off_h = -3.0
                _utc = start if start.tzinfo is not None else start.replace(tzinfo=timezone.utc)
                _local = _utc.astimezone(timezone(timedelta(hours=_off_h)))
                result["start_time"] = _local.isoformat()
                result["start_time_local"] = _local.strftime("%d/%m/%Y %H:%M")
            else:
                result["start_time"] = str(start)
                result["start_time_local"] = str(start)

        # Distância
        dist = data.get("total_distance")
        if dist is not None:
            result["total_distance_km"] = round(dist / 1000, 2)

        # Duração
        timer_time = data.get("total_timer_time")
        elapsed_time = data.get("total_elapsed_time")
        result["total_timer_time_s"]   = timer_time
        result["total_elapsed_time_s"] = elapsed_time
        result["duration_formatted"]   = _seconds_to_hms(timer_time)

        # Velocidade / Pace
        # Garmin firmware recente grava como enhanced_avg_speed (m/s com maior precisão).
        # Fallback para avg_speed se enhanced não estiver disponível.
        avg_speed = data.get("enhanced_avg_speed") or data.get("avg_speed")
        max_speed = data.get("enhanced_max_speed") or data.get("max_speed")
        result["avg_speed_ms"] = avg_speed
        result["avg_pace"]     = _speed_to_pace(avg_speed)
        result["max_pace"]     = _speed_to_pace(max_speed)

        # Frequência Cardíaca
        result["avg_heart_rate"] = data.get("avg_heart_rate")
        result["max_heart_rate"] = data.get("max_heart_rate")

        # Cadência: Garmin grava em passos/min de UM pé → ×2 para SPM real
        avg_cad = data.get("avg_cadence")
        max_cad = data.get("max_cadence")
        result["avg_cadence_spm"] = int(avg_cad * 2) if avg_cad else None
        result["max_cadence_spm"] = int(max_cad * 2) if max_cad else None

        # Outros
        result["total_calories"]  = data.get("total_calories")
        result["total_ascent_m"]  = data.get("total_ascent")
        result["total_descent_m"] = data.get("total_descent")
        result["avg_temperature_c"] = data.get("avg_temperature")
        result["max_temperature_c"] = data.get("max_temperature")

        # Indicadores de treino (nem todos os relógios gravam)
        result["training_stress_score"]      = data.get("training_stress_score")
        result["aerobic_training_effect"]    = data.get("total_training_effect")
        result["anaerobic_training_effect"]  = data.get("total_anaerobic_training_effect")

        # Running Dynamics — Stryd/HRM-Pro gravam em session.
        # Tolerante a None: se campo ausente, fica None no resultado.
        gct      = data.get("avg_ground_contact_time") or data.get("avg_stance_time")
        vert_osc = data.get("avg_vertical_oscillation")
        vert_rt  = data.get("avg_vertical_ratio")
        stride   = data.get("avg_step_length")
        # GCT Balance: Stryd grava como avg_stance_time_percent (0–100%)
        # HRM-Pro grava como avg_stance_time_balance ou avg_ground_contact_balance
        gct_bal  = (data.get("avg_stance_time_percent")
                    or data.get("avg_stance_time_balance")
                    or data.get("avg_ground_contact_balance"))
        if gct is not None:
            result["avg_ground_contact_time_ms"] = float(gct)
        if vert_osc is not None:
            # Stryd/Garmin grava em mm → converter para cm
            result["avg_vertical_oscillation_cm"] = round(float(vert_osc) / 10, 2)
        if vert_rt is not None:
            result["avg_vertical_ratio_pct"] = float(vert_rt)
        if stride is not None:
            # mm → m
            result["avg_stride_length_m"] = round(float(stride) / 1000, 3)
        if gct_bal is not None:
            result["avg_gct_balance_pct"] = float(gct_bal)

        break  # Pega apenas a primeira sessão

    # ── 2. Extrair métricas Stryd e respiração dos records ────────
    # Stryd grava Power, Form Power, Air Power, Leg Spring Stiffness
    # e Impact Loading Rate como developer fields nos records.
    # Respiratory rate vem no campo padrão unknown_108 (FIT field 108,
    # scale 100, unit breaths/min) nos dispositivos Garmin com HRM óptico.
    # Calculamos médias simples excluindo zeros/None.
    _power_vals, _form_vals, _air_vals, _lss_vals, _ilr_vals, _resp_vals = [], [], [], [], [], []
    _has_gps = False          # detecção de esteira: corrida sem GPS = indoor
    _n_records = 0
    for rec in fitfile.get_messages("record"):
        rec_data = {d.name: d.value for d in rec}
        _n_records += 1
        if rec_data.get("position_lat") is not None or rec_data.get("position_long") is not None:
            _has_gps = True
        if rec_data.get("Power") not in (None, 0):
            _power_vals.append(float(rec_data["Power"]))
        if rec_data.get("Form Power") not in (None, 0):
            _form_vals.append(float(rec_data["Form Power"]))
        if rec_data.get("Air Power") is not None:
            _air_vals.append(float(rec_data["Air Power"]))
        if rec_data.get("Leg Spring Stiffness") not in (None, 0):
            _lss_vals.append(float(rec_data["Leg Spring Stiffness"]))
        if rec_data.get("Impact Loading Rate") not in (None, 0):
            _ilr_vals.append(float(rec_data["Impact Loading Rate"]))
        # Respiratory rate: FIT field 108, valor = breaths/min × 100
        raw_resp = rec_data.get("unknown_108")
        if raw_resp not in (None, 0):
            _resp_vals.append(float(raw_resp) / 100.0)

    def _avg(lst): return round(sum(lst) / len(lst), 1) if lst else None

    result["avg_power_w"]              = _avg(_power_vals)
    result["avg_form_power_w"]         = _avg(_form_vals)
    result["avg_air_power_w"]          = _avg(_air_vals)
    result["avg_leg_spring_stiffness"] = _avg(_lss_vals)
    result["avg_impact_loading_rate"]  = _avg(_ilr_vals)
    result["avg_respiration_rate"]     = _avg(_resp_vals)   # breaths/min

    # Log compacto dos dados Stryd/respiração encontrados
    if result["avg_power_w"]:
        logger.info(
            f"   Stryd: {result['avg_power_w']}W | "
            f"Form {result['avg_form_power_w']}W | "
            f"LSS {result['avg_leg_spring_stiffness']} kN/m | "
            f"ILR {result['avg_impact_loading_rate']}"
        )
    if result["avg_respiration_rate"]:
        logger.info(f"   Respiração: {result['avg_respiration_rate']} resp/min")

    # ── Detecção de esteira (treadmill) ──────────────────────────
    # Sinais determinísticos: sub_sport explícito OU corrida sem NENHUM
    # ponto de GPS (indoor). Sem isto, treino de esteira era analisado como
    # corrida de rua (clima/ajuste de calor indevidos). O relógio nem sempre
    # marca sub_sport, então a ausência de GPS é o sinal robusto.
    _sub = (result.get("sub_sport") or "").lower()
    _sport = (result.get("sport") or "").lower()
    result["is_treadmill"] = (
        _sub in {"treadmill", "indoor_running", "virtual_activity"}
        or (_sport in ("running", "run") and _n_records > 0 and not _has_gps)
    )
    if result["is_treadmill"]:
        logger.info(f"   Esteira detectada (sub_sport={_sub or 'n/d'}, gps={_has_gps})")

    # ── 3. Extrair splits (laps) ───────────────────────────────
    for i, lap in enumerate(fitfile.get_messages("lap"), start=1):
        data = {d.name: d.value for d in lap}

        lap_dist  = data.get("total_distance")
        lap_time  = data.get("total_timer_time")
        lap_speed = data.get("enhanced_avg_speed") or data.get("avg_speed")

        # Running dynamics por lap (Stryd)
        lap_gct     = data.get("avg_stance_time")
        lap_osc     = data.get("avg_vertical_oscillation")
        lap_ratio   = data.get("avg_vertical_ratio")
        lap_stride  = data.get("avg_step_length")
        lap_gct_bal = data.get("avg_stance_time_percent")
        lap_power   = data.get("Lap Power")   # developer field do Stryd

        lap_entry = {
            "lap_number":      i,
            "distance_km":     round(lap_dist / 1000, 2) if lap_dist else None,
            "duration":        _seconds_to_hms(lap_time),
            "avg_pace":        _speed_to_pace(lap_speed),
            "avg_heart_rate":  data.get("avg_heart_rate"),
            "avg_cadence_spm": int(data["avg_cadence"] * 2) if data.get("avg_cadence") else None,
            # Stryd / Running Dynamics por lap (None se sensor ausente)
            "avg_power_w":              int(lap_power) if lap_power else None,
            "avg_gct_ms":               float(lap_gct) if lap_gct else None,
            "avg_vertical_osc_cm":      round(float(lap_osc) / 10, 2) if lap_osc else None,
            "avg_vertical_ratio_pct":   float(lap_ratio) if lap_ratio else None,
            "avg_stride_length_m":      round(float(lap_stride) / 1000, 3) if lap_stride else None,
            "avg_gct_balance_pct":      float(lap_gct_bal) if lap_gct_bal else None,
        }
        result["laps"].append(lap_entry)

    # ── 3. Validação mínima ────────────────────────────────────
    if result["total_distance_km"] is None and not result["laps"]:
        logger.warning(f"Arquivo {file_path.name} não contém dados de sessão reconhecíveis.")

    logger.info(
        f"✅ Lido: {result['total_distance_km']} km | "
        f"{result['duration_formatted']} | "
        f"Pace: {result['avg_pace']} | "
        f"FC: {result['avg_heart_rate']} bpm"
    )

    return result
