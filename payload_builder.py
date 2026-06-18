"""
payload_builder.py
Monta o prompt em português para o Claude com:
  - Papel de treinador (system prompt)
  - Dados completos do treino
  - Perfil real do atleta (lido do user_profile.md)

O perfil personaliza cada análise com VDOT, zonas de pace,
objetivo de prova, fase do treinamento e histórico.
"""

import json
import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Optional

from src.heat_utils import calculate_heat_adjustment, pace_str_to_sec, sec_to_pace_str

logger = logging.getLogger("garmin_coach.payload")

# ── Localização do perfil do atleta ───────────────────────────
# Padrão: uma pasta acima do garmin_coach (raiz do Running Trainer)
_DEFAULT_PROFILE = Path(__file__).parent.parent.parent / "user_profile.md"
PROFILE_PATH = Path(os.getenv("ATHLETE_PROFILE_PATH", str(_DEFAULT_PROFILE)))

# ── Localização do plano MPR ──────────────────────────────────
_MPR_PLAN_PATH = Path(__file__).parent.parent.parent / "web" / "data" / "mpr_plan.json"


# ── System prompts ─────────────────────────────────────────────
SYSTEM_PROMPT = """Você é o Coach VDOT, treinador especializado em meia maratona e maratona, \
com profundo conhecimento em: VDOT (Jack Daniels), método 80/20 (Matt Fitzgerald), \
Pose Method (Romanov), Método Norueguês, Pfitzinger e Hansons.

Você interpreta dados de treino do Garmin/Strava e fornece análises objetivas, \
precisas e motivadoras em português, sempre contextualizando com o perfil do atleta.

Responda sempre com esta estrutura exata, usando tags HTML para formatação no Telegram:

<b>📊 Resumo do Treino</b>
[2–3 linhas descrevendo o que foi feito, referenciando o VDOT e fase atual]

<b>✅ Pontos Positivos</b>
[2–3 pontos fortes, comparando com zonas de pace/FC do VDOT do atleta]

<b>⚠️ Sinais de Alerta</b>
[alertas relevantes, ou "Nenhum sinal preocupante" se tudo estiver bem]

<b>🏁 Impacto na Preparação</b>
[como este treino contribui para o objetivo de prova do atleta]

<b>📅 Próximo Treino Sugerido</b>
[recomendação concreta: tipo, distância, pace-alvo baseado no VDOT, foco]

<b>🎯 Aderência ao Objetivo</b>
Nota: X/10 — [justificativa em 1 linha]

Seja direto. Evite parágrafos longos. Use linguagem de treinador.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚨 INSTRUÇÃO CRÍTICA — FCmáx
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A ÚNICA fonte de verdade para a FCmáx deste atleta é o campo "FC Máx Referência" \
informado na seção "Frequência Cardíaca" dos dados do treino. Esse valor foi \
calibrado estatisticamente em 25/04/2026 com base em 197 corridas reais.

REGRAS NÃO-NEGOCIÁVEIS:
1. SEMPRE calcule percentuais (%FCmáx) sobre o valor "FC Máx Referência" do payload.
2. NUNCA use fórmulas (220-idade, 207-0,7×idade) ou números antigos que apareçam em \
   prosa do perfil — eles foram superados pela calibração estatística.
3. NUNCA mencione "FCmáx estimada de 175 bpm" ou similares — esse valor está OBSOLETO.
4. Quando os dados incluírem aviso "POSSÍVEL ARTEFATO", trate o pico como ruído da \
   cinta de FC e foque a análise na FC MÉDIA — não invente bandeira clínica em cima \
   de pico isolado.
5. Pico de FC ≤92% da FC Máx Referência num treino easy NÃO é "FC excedida" — é \
   normal/aceitável. Bandeira clínica de "FC máxima excedida" só deve ser emitida se \
   o pico for ≥98% FC Máx Referência E sustentado por mais de 30s sem prescrição de \
   intervalo.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🌡️ INSTRUÇÃO CRÍTICA — CALOR / AMBIENTE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Trate o calor com PROPORÇÃO e linguagem factual. Regras:
1. NÃO use termos dramáticos ("extremo", "hostil", "brutal", "severo") para o clima. \
   Descreva as condições pelos números (temperatura, umidade, Heat Index).
2. Classifique a carga térmica APENAS pelo Heat Index informado no payload: ≥32°C = \
   sobrecarga térmica significativa; 27–31°C = impacto moderado; <27°C = calor leve, \
   sem dramatizar (treino de madrugada em Recife costuma cair nesta faixa).
3. O calor é UM fator entre vários (condicionamento, hidratação, pacing, fase de treino). \
   NÃO atribua toda a deriva cardíaca ou a FC elevada ao calor — pondere as outras causas.
4. O ajuste de calor já vem pré-calculado (determinístico) no payload. Use esse valor; \
   não o reforce nem exagere na narrativa.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPORTANTE — BLOCO DE MÉTRICAS ESTRUTURADAS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Ao FINAL da sua resposta (após o Aderência ao Objetivo), adicione EXATAMENTE um \
bloco oculto para consumo programático (o Telegram ignora comentários HTML). \
Preencha apenas campos que conseguir inferir com confiança — use null caso contrário. \
Nunca invente números. Não adicione texto após o bloco.

Formato obrigatório:
<!-- METRICS
{
  "vdot_referencia": 39,
  "tipo_treino_detectado": "longao|rodagem|intervalado|limiar|fartlek|regenerativo|progressivo|ritmo_prova|tecnica|outro",
  "zona_prescrita": "E|M|T|I|R|mista",
  "pace_prescrito_min_km": "5:58-6:40",
  "pace_executado_min_km": "5:34",
  "aderencia_pace_pct": 92,
  "distribuicao_intensidade": {"facil_pct": 78, "moderado_pct": 15, "forte_pct": 7},
  "gray_zone_alerta": false,
  "deriva_cardiaca_pct": 4.2,
  "eficiencia_aerobica": 1.85,
  "cadencia_media_spm": 180,
  "ajuste_calor_segundos_km": 18,
  "pace_equivalente_frio_min_km": "5:16",
  "limitador_principal": "deriva_cardiaca|polarizacao|eficiencia|adesao|acwr|red_s|tecnica|clima|nenhum",
  "bandeira_clinica": null,
  "nota_aderencia": 8,
  "confianca_analise": "alta|media|baixa"
}
-->"""

SWIM_SYSTEM_PROMPT = """Você é o Coach VDOT, treinador especializado em maratona e meia maratona. \
O atleta usa natação como cross-training aeróbico complementar à corrida.

Você interpreta dados de treino de natação do Garmin/Strava e fornece análise focada \
no benefício aeróbico para a corrida, em português, com linguagem motivadora e direta.

Responda sempre com esta estrutura exata, usando tags HTML para formatação no Telegram:

<b>🏊 Resumo da Natação</b>
[2–3 linhas: distância, duração, ritmo médio por 100m, contexto cross-training]

<b>✅ Benefícios para a Corrida</b>
[2–3 pontos: impacto aeróbico (FC, duração), recuperação ativa, capacidade pulmonar]

<b>⚠️ Observações</b>
[alertas se FC muito alta/baixa, sessão muito curta para benefício aeróbico, ou "Ótima sessão de cross-training"]

<b>🏃 Integração com o Plano de Corrida</b>
[como esta natação se encaixa no ciclo semanal: volume aeróbico, recuperação, reduz impacto]

<b>📅 Próxima Sessão de Natação Sugerida</b>
[recomendação: distância-alvo, ritmo aeróbico (FC 65-75% FCmax), foco técnico]

<b>🎯 Qualidade do Cross-Training</b>
Nota: X/10 — [justificativa em 1 linha focada no benefício aeróbico para corrida]

Avalie a natação como ferramenta de condicionamento aeróbico, não como esporte principal. \
Foque em: duração em zona aeróbica, volume total, recuperação muscular para corridas. \
Seja direto. Evite parágrafos longos.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPORTANTE — BLOCO DE MÉTRICAS ESTRUTURADAS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Ao FINAL da sua resposta, adicione EXATAMENTE um bloco oculto para consumo \
programático (o Telegram ignora comentários HTML). Preencha apenas campos que \
conseguir inferir — use null caso contrário. Não invente números.

Formato obrigatório:
<!-- METRICS
{
  "vdot_referencia": 39,
  "tipo_treino_detectado": "natacao_aerobica|natacao_tecnica|natacao_intervalado|outro",
  "zona_prescrita": "aerobica_leve|aerobica|limiar|outro",
  "aderencia_zona_fc_pct": 85,
  "tempo_em_zona_aerobica_min": 32,
  "bracadas_por_min": 32,
  "limitador_principal": "volume|intensidade|tecnica|frequencia|nenhum",
  "nota_aderencia": 8,
  "confianca_analise": "alta|media|baixa"
}
-->"""


# ── Seção condicional adicionada ao SYSTEM_PROMPT quando há sensor real ──
_POWER_SYSTEM_SECTION = (
    "\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "⚡ INSTRUÇÃO: SEÇÃO DE POTÊNCIA / STRYD\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "O payload contém dados de sensor de potência real (Stryd/Garmin Running Power).\n"
    "Inclua OBRIGATORIAMENTE a seção abaixo após <b>⚠️ Sinais de Alerta</b>:\n\n"
    "<b>⚡ Potência / Stryd</b>\n"
    "Analise com base EXCLUSIVAMENTE nos valores do payload.\n"
    "PROIBIDO citar números ausentes nos dados enviados.\n\n"
    "• Running Effectiveness (RE): eficiência m/s por W. "
    "Referência: 0.010–0.014 para amadores. RE alto = eficiente; baixo = custo mecânico elevado.\n"
    "• Variability Index (VI): <5% = uniforme; 5–10% = moderado; >10% = instável/fartlek.\n"
    "• Form Power: ideal <20% da potência total. Alto = postura ineficiente ou fadiga de core.\n"
    "• Decoupling W/FC: classifique a fadiga — cardiovascular (FC↑ W estável), "
    "mecânica (W↓ FC estável) ou neuromuscular (ambos↓).\n"
    "• Conclusão: 1–2 linhas dizendo se potência confirma ou contradiz pace/FC.\n"
)

# ── Seletor de system prompt ──────────────────────────────────

def get_system_prompt(training_data: dict) -> str:
    """
    Seleciona o prompt de sistema adequado para o tipo de atividade.
    - Natação                             → SWIM_SYSTEM_PROMPT
    - Corrida com sensor real de potência → SYSTEM_PROMPT + _POWER_SYSTEM_SECTION
    - Qualquer outro                       → SYSTEM_PROMPT
    """
    sport = (training_data.get("sport") or "run").lower()
    if sport == "swim":
        return SWIM_SYSTEM_PROMPT
    has_real_power = (
        training_data.get("avg_watts") is not None
        and bool(training_data.get("device_watts", False))
    )
    if has_real_power:
        return SYSTEM_PROMPT + _POWER_SYSTEM_SECTION
    return SYSTEM_PROMPT


# ── Plano MPR ─────────────────────────────────────────────────

def _extract_iso_date(d: dict) -> str:
    """
    Extrai uma data em formato ISO (YYYY-MM-DD) do training_data.
    Prefere 'start_time' (já em ISO 8601), com fallback para
    'start_time_local' que vem como 'DD/MM/YYYY HH:MM'.
    Retorna '' se não conseguir.
    """
    iso = d.get("start_time")
    if isinstance(iso, str) and len(iso) >= 10:
        # ISO 8601 → 'YYYY-MM-DD...' — só pegar os 10 primeiros
        candidate = iso[:10]
        try:
            date.fromisoformat(candidate)
            return candidate
        except ValueError:
            pass

    local = d.get("start_time_local") or ""
    if local:
        # Esperado 'DD/MM/YYYY HH:MM' — converter para 'YYYY-MM-DD'
        for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
            try:
                from datetime import datetime as _dt
                return _dt.strptime(local, fmt).date().isoformat()
            except (ValueError, TypeError):
                continue

    return ""


def load_mpr_plan() -> dict | None:
    """Carrega o mpr_plan.json. Retorna None se não existir."""
    if not _MPR_PLAN_PATH.exists():
        logger.debug("mpr_plan.json não encontrado — contexto MPR não incluído.")
        return None
    try:
        return json.loads(_MPR_PLAN_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Erro ao ler mpr_plan.json: {e}")
        return None


def find_mpr_session(training_date_str: str, plan: dict) -> dict | None:
    """
    DEPRECADO — mantido apenas por compatibilidade com chamadas antigas.

    Este lookup ingênuo (±1 dia indiscriminado) é a raiz do BUG 1: um
    treino EXTRA em D+1 era pareado com a prescrição de D, fazendo o
    sistema acusar não cumprimento da sessão original e duplicar o
    matching. A nova entrada correta é `match_training_against_plan` →
    `session_matcher.SessionMatcher.match`, que respeita estado prévio.

    Uso atual: somente busca exata (sem janela ±1) para casos onde a
    chamada legada ainda não foi migrada.
    """
    if not plan or not training_date_str:
        return None

    target = None
    s = training_date_str.strip()
    try:
        target = date.fromisoformat(s[:10])
    except ValueError:
        from datetime import datetime as _dt
        for fmt in ("%d/%m/%Y", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
            try:
                target = _dt.strptime(s, fmt).date()
                break
            except ValueError:
                continue
        if target is None:
            logger.warning(f"find_mpr_session: data não reconhecida: {training_date_str!r}")
            return None

    target_iso = target.isoformat()
    for session in plan.get("sessions", []):
        if session.get("date") == target_iso:
            return session
    return None


# ── Matcher robusto (corrige o BUG 1) ─────────────────────────

def _try_recover_partial_json(text: str) -> dict | None:
    """
    Recupera o prefixo válido de um analyses.json corrompido cortando no
    último entry completo do array `analyses`. Foi a estratégia usada na
    investigação forense do incidente de 14/05/2026 e é o caminho mais
    robusto quando uma escrita atômica é interrompida.

    Retorna None se nem o prefixo é parseável.
    """
    cut = text.rfind('},\n    {\n      "id":')
    if cut < 0:
        return None
    candidate = text[: cut + 1] + "\n  ]\n}"
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _load_existing_activities(
    exclude_id: str | None = None,
    exclude_pid: str | None = None,
) -> list:
    """
    Carrega o histórico atual do dashboard (analyses.json) e converte
    em uma lista de session_matcher.Activity, para que o matcher saiba
    quais sessões prescritas já foram cumpridas por outras atividades.

    `exclude_id`:  ID da atividade sendo processada agora. Remove o entry
    antigo pelo id estável (Prioridade 5).
    `exclude_pid`: provider_activity_id completo (Strava id / FIT serial).
    Exclui entries cujo campo provider_activity_id é idêntico a este valor,
    cobrindo o caso em que o id estável muda entre reprocessamentos mas o
    provider_id permanece o mesmo (ex.: FIT Garmin com PID em formato
    "<serial>-<timestamp>" que não casa com o sufixo truncado do id).
    """
    from src.session_matcher import existing_to_activity
    web_data = Path(os.getenv(
        "WEB_DATA_PATH",
        str(Path(__file__).parent.parent.parent / "web" / "data" / "analyses.json"),
    ))
    if not web_data.exists():
        return []
    try:
        raw_text = web_data.read_text(encoding="utf-8")
        # Tolerância a arquivo com JSON duplo (gravação atômica interrompida)
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as decode_err:
            # Tentativa 1: prefixo válido via raw_decode (lixo no final)
            try:
                decoder = json.JSONDecoder()
                raw, _ = decoder.raw_decode(raw_text)
                logger.warning(
                    "analyses.json com lixo após JSON válido — recuperado "
                    f"(corrupção em char {decode_err.pos}). RECOMENDADO: "
                    "restaurar do backup mais recente e investigar."
                )
            except json.JSONDecodeError:
                # Tentativa 2: corte no último entry válido do array
                recovered = _try_recover_partial_json(raw_text)
                if recovered is None:
                    # CORREÇÃO ARQUITETURAL pós-incidente 14/05/2026:
                    # Falhar LOUD em vez de retornar [] silenciosamente.
                    # Retornar [] aqui transforma corrupção em "histórico vazio",
                    # o que faz o matcher gravar matches errados em cima do
                    # estado bom — cascade venenoso confirmado em produção.
                    raise RuntimeError(
                        f"analyses.json CORROMPIDO E IRRECUPERÁVEL "
                        f"(parse error em char {decode_err.pos}). "
                        f"Pipeline ABORTADO para preservar integridade do "
                        f"estado. Restaure de web/data/analyses.backup_*.json "
                        f"antes de continuar."
                    )
                raw = recovered
                logger.error(
                    f"analyses.json parcialmente corrompido. Recovery: "
                    f"{len(raw.get('analyses', []))} entries recuperados. "
                    "Investigar causa raiz antes de novos reprocesses."
                )
    except RuntimeError:
        # Propaga o erro de corrupção sem mascarar
        raise
    except Exception as e:
        # Outros erros de IO (permissão, disco cheio, etc.) — também propaga
        logger.error(f"Erro CRÍTICO lendo analyses.json: {e}")
        raise
    entries = raw.get("analyses", [])
    if exclude_id:
        # Exclui pelo ID novo OU por provider_activity_id quando coincide
        # com o que o ID novo carrega como sufixo. Isso garante que durante
        # a transição entre o esquema antigo de ID e o novo (Prioridade 5),
        # uma reanálise não auto-bloqueie o próprio match.
        excl_pid_match = None
        # ID novo termina com _<provider_id_alnum>; extrai esse sufixo
        # para também identificar entries antigos que descrevem a MESMA
        # atividade Strava mas usavam o ID curto.
        parts = exclude_id.split("_")
        if parts:
            tail = parts[-1]
            if tail.isalnum() and len(tail) >= 8:
                excl_pid_match = tail
        entries = [
            a for a in entries
            if a.get("id") != exclude_id
            and not (excl_pid_match and a.get("provider_activity_id") == excl_pid_match)
            # G2b: também exclui pelo PID completo (ex.: FIT "3422...-20260603...")
            and not (exclude_pid and a.get("provider_activity_id") == exclude_pid)
        ]
    return [existing_to_activity(a) for a in entries]


def _make_activity_id(training_data: dict, iso_date: str) -> str:
    """
    Gera o mesmo ID que o history_manager usará ao persistir a análise.
    Usado para excluir o entry antigo ao reprocessar uma atividade.

    Delega para activity_id.make_stable_id para garantir que este ID e o
    de history_manager._make_id NUNCA divergem (Prioridade 5 — antes da
    refatoração, eles tinham regras de slug ligeiramente diferentes:
    truncavam em 20 vs 40 chars e tratavam espaços diferentemente,
    causando bugs silenciosos quando o nome ficava perto do limite).
    """
    from src.activity_id import make_stable_id
    return make_stable_id(training_data, iso_date)


def _speed_to_pace_str(speed_ms: float) -> Optional[str]:
    """
    Converte velocidade em m/s (campo Strava 'average_speed') para string
    de pace no formato "M:SS/km".
    Retorna None se speed_ms for zero ou inválida.
    """
    if not speed_ms or speed_ms <= 0:
        return None
    pace_min_per_km = (1.0 / speed_ms) * (1000.0 / 60.0)
    # Sanity check: ignora paces absurdos (pausa ou sprint impossível)
    if pace_min_per_km > 20 or pace_min_per_km < 2:
        return None
    mins = int(pace_min_per_km)
    secs = int(round((pace_min_per_km - mins) * 60))
    if secs == 60:
        mins += 1
        secs = 0
    return f"{mins}:{secs:02d}/km"


def _extract_min_lap_pace(d: dict) -> Optional[str]:
    """
    Extrai o pace do lap mais rápido dentre todos os laps da atividade.

    Usado para detectar sessões estruturadas (SERIES Z1+Z3+Z1) como "quality"
    mesmo quando o avg_pace geral fica diluído pelo warmup/cooldown.

    Suporta laps brutos do Strava (campo 'average_speed' em m/s) e laps
    processados (campo 'avg_pace' em string "M:SS/km").

    Laps fantasma (distância < 0.2 km OU duração < 60 s) são ignorados —
    evita que um lap residual de 2 s / 0.01 km com pace "5:25/km" force-
    classifique uma corrida easy como "quality".

    Retorna string no formato "5:24/km" ou None se não houver dados.
    """
    from src.session_matcher import _pace_to_minutes
    laps = d.get("laps") or []
    if not laps:
        return None

    min_pace_min: Optional[float] = None
    min_pace_str: Optional[str]   = None

    for lap in laps:
        # ── Filtro de lap fantasma ────────────────────────────────
        # FIT processado:  distance_km (float)  + duration ("M:SS" / "H:MM:SS")
        # Strava raw:      distance   (metros)   + moving_time (segundos)
        dist_km = lap.get("distance_km")
        dist_m  = lap.get("distance")         # Strava raw (metros)
        if dist_km is None and dist_m is not None:
            dist_km = dist_m / 1000.0

        dur_s = lap.get("moving_time")        # Strava raw (segundos)
        if dur_s is None:
            dur_str = lap.get("duration")
            if dur_str:
                try:
                    parts = str(dur_str).split(":")
                    if len(parts) == 3:
                        dur_s = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                    elif len(parts) == 2:
                        dur_s = int(parts[0]) * 60 + int(parts[1])
                except (ValueError, TypeError):
                    pass

        # Rejeita laps residuais: < 0.2 km OU < 60 s
        if (dist_km is not None and dist_km < 0.2) or (dur_s is not None and dur_s < 60):
            logger.debug(
                f"_extract_min_lap_pace: ghost lap ignorado "
                f"(dist={dist_km} km, dur={dur_s}s)"
            )
            continue

        # ── Extração do pace ──────────────────────────────────────
        # Strava raw: average_speed em m/s
        speed_ms = lap.get("average_speed")
        if speed_ms is not None:
            pace_str = _speed_to_pace_str(speed_ms)
        else:
            # Laps processados: avg_pace já como string
            pace_str = lap.get("avg_pace")

        if not pace_str or pace_str == "N/D":
            continue
        if "/100m" in pace_str:           # natação — ignora
            continue
        p = _pace_to_minutes(pace_str)
        if p is None or p <= 0:
            continue
        if p > 15:                        # pausa — ignora
            continue
        if min_pace_min is None or p < min_pace_min:
            min_pace_min = p
            min_pace_str = pace_str

    return min_pace_str


def _training_to_activity(d: dict, iso_date: str):
    """Adapta training_data → session_matcher.Activity para o matcher."""
    from src.session_matcher import Activity, RunningDynamics
    duration_s = d.get("total_timer_time_s")
    duration_min = (duration_s / 60) if duration_s else None

    # Fallback: ler duração do campo formatado ("HH:MM:SS" ou "MM:SS")
    if duration_min is None and d.get("duration_formatted"):
        from src.session_matcher import _duration_str_to_min
        duration_min = _duration_str_to_min(d["duration_formatted"])

    aid = d.get("activity_id") or d.get("filename") or f"{iso_date}_{d.get('source','?')}"
    rd = RunningDynamics(
        ground_contact_time_ms=d.get("avg_ground_contact_time_ms"),
        vertical_oscillation_cm=d.get("avg_vertical_oscillation_cm"),
        vertical_ratio_pct=d.get("avg_vertical_ratio_pct"),
        stride_length_m=d.get("avg_stride_length_m"),
        gct_balance_pct=d.get("avg_gct_balance_pct"),
    )

    # Pace do lap mais rápido: permite detectar qualidade em sessões estruturadas
    # onde avg_pace é diluído pelo warmup/cooldown (ex.: SERIES Z1+Z3+Z1)
    min_lap_pace = _extract_min_lap_pace(d)

    return Activity(
        id=str(aid),
        date=iso_date,
        duration_min=duration_min,
        distance_km=d.get("total_distance_km"),
        avg_hr=d.get("avg_heart_rate"),
        avg_pace=d.get("avg_pace"),
        min_lap_pace=min_lap_pace,
        source=d.get("source", "garmin"),
        sport=(d.get("sport") or "run").lower(),
        is_treadmill=bool(d.get("is_treadmill", False)),
        running_dynamics=rd,
    )


def match_training_against_plan(training_data: dict, plan: dict | None):
    """
    Ponto de entrada robusto para o matching MPR × executado.

    Retorna `session_matcher.MatchResult`. Também escreve a
    classificação de volta em `training_data` (chaves
    `mpr_classification`, `mpr_matched_session_id`,
    `extra_session_evaluation`) para o history_manager persistir.
    """
    from src.session_matcher import SessionMatcher

    iso_date = _extract_iso_date(training_data)
    activity = _training_to_activity(training_data, iso_date)

    # ── Guarda de cobertura: não reclassificar fora do intervalo do plano ──
    # Se a atividade é anterior ao início do plano ou posterior ao seu fim
    # +7 dias de tolerância, o matcher não tem sessões para casar e cairia
    # em extra_session por falta de opções. Em vez disso: preservamos a
    # classificação já gravada no histórico (se houver) ou marcamos unknown.
    if plan is not None:
        plan_sessions = plan.get("sessions", [])
        plan_dates = [s.get("date", "") for s in plan_sessions if s.get("date")]
        if plan_dates:
            plan_start = min(plan_dates)
            plan_end   = max(plan_dates)
            from datetime import date as _date, timedelta
            try:
                act_dt   = _date.fromisoformat(iso_date)
                start_dt = _date.fromisoformat(plan_start)
                end_dt   = _date.fromisoformat(plan_end)
                out_of_range = act_dt < start_dt or act_dt > end_dt + timedelta(days=7)
            except ValueError:
                out_of_range = False
            if out_of_range:
                logger.info(
                    f"Cobertura MPR: {iso_date} fora do intervalo "
                    f"{plan_start}–{plan_end} (+7d). Classificação preservada."
                )
                # Tentar preservar classificação existente do histórico
                existing_entries = _load_existing_activities()
                existing_cls = None
                current_pid = training_data.get("provider_activity_id")
                for ea in existing_entries:
                    if ea.matched_session_id and ea.matched_session_id != "unknown":
                        if ea.id == _make_activity_id(training_data, iso_date):
                            existing_cls = "planned_match"
                            break
                training_data["mpr_classification"] = existing_cls or "unknown"
                training_data["mpr_matched_session_id"] = None
                training_data["mpr_match_confidence"] = "low"
                training_data["mpr_match_confidence_reasons"] = [
                    f"fora da cobertura do plano MPR ({plan_start}–{plan_end})"
                ]
                training_data["mpr_match_score"] = None
                training_data["extra_session_evaluation"] = None
                from src.session_matcher import MatchResult
                return MatchResult(
                    activity=activity,
                    planned_session=None,
                    extra_eval=None,
                    classification=training_data["mpr_classification"],
                    confidence="low",
                    confidence_reasons=list(training_data["mpr_match_confidence_reasons"]),
                    match_score=None,
                )

    # Exclui o entry da própria atividade ao carregar o histórico.
    # Isso evita que um reprocessamento auto-bloqueie o match correto:
    # sem essa exclusão, o entry antigo reclamaria a prescrição antes
    # do matcher ter chance de escolher o candidato certo.
    current_id  = _make_activity_id(training_data, iso_date)
    current_pid = training_data.get("provider_activity_id")
    matcher = SessionMatcher(
        plan,
        _load_existing_activities(exclude_id=current_id, exclude_pid=current_pid),
    )
    result = matcher.match(activity)

    # Anexa estado para persistência no JSON do dashboard.
    training_data["mpr_classification"] = result.classification
    training_data["mpr_matched_session_id"] = activity.matched_session_id
    # Confiança do matcher (Prioridade 4) — vai para o dashboard como badge
    # e entra no prompt do Claude para que ele saiba se pode confiar no match.
    training_data["mpr_match_confidence"] = result.confidence
    training_data["mpr_match_confidence_reasons"] = list(result.confidence_reasons)
    training_data["mpr_match_score"] = (
        round(result.match_score, 3) if result.match_score is not None else None
    )
    if result.extra_eval is not None:
        training_data["extra_session_evaluation"] = {
            "activity_id":             result.extra_eval.activity_id,
            "date":                    result.extra_eval.date,
            "classification":          result.extra_eval.classification,
            "reason":                  result.extra_eval.reason,
            "impact_on_next_sessions": result.extra_eval.impact_on_next_sessions,
        }
    else:
        training_data["extra_session_evaluation"] = None

    logger.info(
        f"MPR match → {result.classification} "
        f"(session={activity.matched_session_id}, "
        f"confidence={result.confidence}, "
        f"extra={result.extra_eval.classification if result.extra_eval else '-'})"
    )
    return result


def _fmt_session_brief(ps, zones: dict) -> str:
    """Formata uma PlannedSession em uma linha resumida para o contexto."""
    if ps is None:
        return "N/D"
    dur_str  = f"{ps.duration_min} min" if ps.duration_min else "N/D"
    d_min    = ps.distance_min_km
    d_max    = ps.distance_max_km
    dist_str = f"{d_min}–{d_max} km" if d_min else ""
    z_str    = ", ".join(ps.target_zone) if ps.target_zone else "N/D"
    zone_paces = []
    for z in ps.target_zone:
        zd = zones.get(z) or {}
        if zd.get("pace_min"):
            zone_paces.append(f"{z}: {zd['pace_min']}–{zd['pace_max']}/km")
    pace_str = " | ".join(zone_paces) if zone_paces else z_str
    dist_part = f" / {dist_str}" if dist_str else ""
    return f"{ps.date} — {ps.type} {dur_str}{dist_part} ({pace_str})"


def build_mpr_context(session: dict | None, plan: dict | None, *, match_result=None) -> str:
    """
    Constrói o bloco de contexto MPR para incluir no prompt do Claude.

    `match_result` (opcional): instância de session_matcher.MatchResult.
    Quando presente, é a fonte de verdade — o `session` do parâmetro
    legado é ignorado em favor do `match_result.planned_session`.

    Inclui:
    - Classificação da atividade (planned_match / completed_delayed /
      completed_early / partially_completed / extra_session)
    - Sessão prescrita pareada
    - Próxima sessão pendente da MPR (evita sugestões genéricas)
    - Tabela de zonas e comparativo Daniels × MPR

    Retorna string vazia se não houver plano.
    """
    if not plan:
        return ""

    zones = plan.get("zones_mpr", {})
    zone_lines = []
    for z in ["Z1", "Z2", "Z3", "Z4", "Z5"]:
        if z in zones:
            zd = zones[z]
            zone_lines.append(
                f"  {z} ({zd['label']:12}): {zd['pace_min']}–{zd['pace_max']}/km"
            )
    zones_block = "\n".join(zone_lines)

    # ── Reconcilia parâmetros: MatchResult é a fonte de verdade ──
    if match_result is not None:
        ps = match_result.planned_session
        session = None
        if ps is not None:
            session = {
                "date":          ps.date,
                "day_of_week":   "",
                "type":          ps.type,
                "duration_min":  ps.duration_min,
                "distance_min_km": ps.distance_min_km,
                "distance_max_km": ps.distance_max_km,
                "structure":     ps.structure,
                "notes":         ps.notes,
                "zones":         ps.target_zone,
            }

    # ── Próxima sessão pendente MPR ───────────────────────────────
    # Informa o Claude sobre o que vem a seguir para evitar sugestões
    # que contradizem ou ignoram o calendário do treinador.
    next_session_line = ""
    if match_result is not None and match_result.next_pending_session is not None:
        nps = match_result.next_pending_session
        next_session_line = (
            f"\nPRÓXIMA SESSÃO MPR PENDENTE: "
            f"{_fmt_session_brief(nps, zones)}\n"
            f"⚠️ OBRIGATÓRIO: A sugestão de próximo treino DEVE referenciar esta "
            f"prescrição da MPR, não inventar sessão genérica. Se houver adaptação "
            f"necessária (fadiga, calor), sugerir discussão com o treinador."
        )

    # ── Confiança do matcher (Prioridade 4) ──────────────────────
    # Injetada no prompt para que o Claude saiba se pode confiar no match
    # ou se deve mencionar a ambiguidade nas observações.
    confidence_note = ""
    if match_result is not None:
        conf = getattr(match_result, "confidence", None)
        reasons = list(getattr(match_result, "confidence_reasons", []) or [])
        if conf == "high":
            confidence_note = (
                "Confiança do matcher: ALTA — o pareamento prescrição×execução é "
                "robusto; você pode basear a aderência diretamente nele."
            )
        elif conf == "medium":
            why = f" Motivo: {reasons[0]}" if reasons else ""
            confidence_note = (
                "Confiança do matcher: MÉDIA — pareamento razoável mas com "
                f"ressalvas.{why} "
                "Mencione a margem de interpretação se relevante para a aderência."
            )
        elif conf == "low":
            why = f" Motivo: {reasons[0]}" if reasons else ""
            confidence_note = (
                "Confiança do matcher: BAIXA — o pareamento é marginal e pode "
                f"estar errado.{why} "
                "TRATE A CLASSIFICAÇÃO COM CETICISMO: sinalize ao atleta que a "
                "categorização é uma estimativa, não baseie penalidades fortes nela."
            )

    # ── Bloco de classificação ────────────────────────────────────
    classif_block = ""
    if match_result is not None:
        c = match_result.classification

        if c == "planned_match":
            classif_block = (
                "Classificação desta atividade: PLANNED_MATCH — executada exatamente "
                "no dia da prescrição. Avalie aderência normalmente."
            )

        elif c == "adjusted_within_microcycle":
            orig_date = session["date"] if session else "?"
            act_date  = (match_result.activity.date if match_result.activity else "?")
            act_dur   = (
                f"{match_result.activity.duration_min:.0f} min"
                if match_result.activity and match_result.activity.duration_min
                else "?"
            )
            plan_dur  = f"{session['duration_min']} min" if session and session.get("duration_min") else "?"
            classif_block = (
                f"Classificação desta atividade: ADJUSTED_WITHIN_MICROCYCLE\n"
                f"  → Treino prescrito para {orig_date} ({session['type'] if session else '?'}, "
                f"{plan_dur}) realizado em {act_date} ({act_dur}) — ajuste normal de agenda "
                f"dentro do microciclo semanal.\n"
                f"  → O deslocamento de dias é fisiologicamente irrelevante: o objetivo do "
                f"estímulo, o volume semanal e a sequência lógica de cargas foram preservados.\n"
                f"  → ADERÊNCIA: considere este treino CONCLUÍDO com sucesso. NÃO use "
                f"linguagem de atraso, falha ou inconsistência.\n"
                f"  → O restante da semana MPR permanece inalterado e pendente conforme prescrito.\n"
                f"  → Analise a qualidade do treino (pace, FC, RPE) em relação à prescrição "
                f"de {orig_date}, não à data de execução."
            )

        elif c == "completed_delayed":
            orig_date = session["date"] if session else "?"
            act_date  = (match_result.activity.date if match_result.activity else "?")
            act_dur   = (
                f"{match_result.activity.duration_min:.0f} min"
                if match_result.activity and match_result.activity.duration_min
                else "?"
            )
            plan_dur  = f"{session['duration_min']} min" if session and session.get("duration_min") else "?"
            classif_block = (
                f"Classificação desta atividade: COMPLETED_DELAYED\n"
                f"  → Treino prescrito para {orig_date} ({session['type'] if session else '?'}, "
                f"{plan_dur}) executado em {act_date} ({act_dur}) com deslocamento que "
                f"ultrapassa a janela de tolerância fisiológica para este tipo de sessão, "
                f"ou foi detectado acúmulo inadequado de estímulos (stacking).\n"
                f"  → Avalie se houve compressão de recuperação, superposição de cargas "
                f"duras ou perda de estímulo específico.\n"
                f"  → A aderência deve ser avaliada sobre a prescrição de {orig_date}.\n"
                f"  → O longão ou qualquer outra prescrição futura da semana permanece "
                f"PENDENTE e NÃO foi afetado por este treino.\n"
                f"  → NÃO classifique este treino como longão interrompido."
            )

        elif c == "completed_early":
            orig_date = session["date"] if session else "?"
            act_date  = (match_result.activity.date if match_result.activity else "?")
            classif_block = (
                f"Classificação desta atividade: COMPLETED_EARLY\n"
                f"  → Treino prescrito para {orig_date} ({session['type'] if session else '?'}) "
                f"antecipado para {act_date} — reorganização válida dentro do microciclo.\n"
                f"  → Avalie se a antecipação comprometeu a recuperação de sessões anteriores "
                f"ou criou acúmulo inadequado de estímulos (stacking).\n"
                f"  → Avalie aderência sobre a prescrição de {orig_date}."
            )

        elif c == "partially_completed":
            orig_date = session["date"] if session else "?"
            act_dur   = (
                f"{match_result.activity.duration_min:.0f} min"
                if match_result.activity and match_result.activity.duration_min
                else "?"
            )
            plan_dur  = f"{session['duration_min']} min" if session and session.get("duration_min") else "?"
            pct       = ""
            if match_result.activity and match_result.activity.duration_min and session and session.get("duration_min"):
                p = match_result.activity.duration_min / session["duration_min"] * 100
                pct = f" (~{p:.0f}% do prescrito)"
            classif_block = (
                f"Classificação desta atividade: PARTIALLY_COMPLETED\n"
                f"  → Longão prescrito: {orig_date} ({plan_dur}). Executado: {act_dur}{pct}.\n"
                f"  → Cobertura abaixo de 70% do longão, sem outro candidato mais curto "
                f"pendente na semana. Pode indicar interrupção por calor, fadiga ou decisão "
                f"consciente — pergunte ao atleta o motivo.\n"
                f"  → Nota de aderência deve refletir o volume parcial entregue."
            )

        elif c in ("rescheduled",):
            # Alias legado — tratar como adjusted_within_microcycle
            orig_date = session["date"] if session else "?"
            act_date  = (match_result.activity.date if match_result.activity else "?")
            classif_block = (
                f"Classificação desta atividade: ADJUSTED_WITHIN_MICROCYCLE (reposição legado)\n"
                f"  → Prescrição de {orig_date} executada em {act_date} dentro do microciclo. "
                f"Aderência conta sobre o plano reposto, sem penalidade de consistência."
            )

        elif c == "extra_session":
            ev         = match_result.extra_eval
            ev_class   = ev.classification if ev else "neutral"
            ev_reason  = ev.reason if ev else ""
            ev_impact  = ev.impact_on_next_sessions if ev else ""
            classif_block = (
                f"Classificação desta atividade: EXTRA_SESSION — não há prescrição MPR "
                f"para o dia. Avaliação isolada: {ev_class.upper()}.\n"
                f"  {ev_reason}\n"
                f"  Impacto nas próximas sessões: {ev_impact}\n"
                f"IMPORTANTE: NÃO reduza a aderência à planilha por causa deste treino "
                f"extra; ele é avaliado em separado."
            )

    # ── Sem prescrição pareada (treino extra) ────────────────────
    if not session:
        return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PLANO DO TREINADOR (MPR) — {plan.get('month', '?')}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{classif_block}
{confidence_note}
{next_session_line}
Zonas de referência MPR:
{zones_block}

Sessão planejada: NENHUMA — treino extra (não prescrito pelo MPR).
Avalie como sessão adicional e considere se complementa ou sobrepõe o plano."""

    # ── Sessão prescrita pareada ─────────────────────────────────
    z_list   = ", ".join(session.get("zones", ["Z1"]))
    dur      = session.get("duration_min")
    dur_str  = f"{dur} min" if dur else "N/D"
    d_min    = session.get("distance_min_km")
    d_max    = session.get("distance_max_km")
    dist_str = f"{d_min}–{d_max} km" if d_min else "N/D"

    prescribed_zones = []
    for z in session.get("zones", []):
        if z in zones:
            zd = zones[z]
            prescribed_zones.append(f"{z} ({zd['label']}): {zd['pace_min']}–{zd['pace_max']}/km")
    prescribed_str = " | ".join(prescribed_zones) if prescribed_zones else z_list

    return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PLANO DO TREINADOR (MPR) — {plan.get('month', '?')}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{classif_block}
{confidence_note}
{next_session_line}
Sessão prescrita pareada — {session['date']} ({session.get('day_of_week', '')}):
  Tipo:       {session['type']}
  Duração:    {dur_str}
  Distância:  {dist_str}
  Zonas MPR:  {prescribed_str}
  Estrutura:  {session.get('structure', 'N/D')}
{('  Obs: ' + session['notes']) if session.get('notes') else ''}

Tabela de zonas MPR (referência):
{zones_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMPARATIVO DANIELS (VDOT 39) × MPR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Zonas Daniels VDOT 39:
  E  (Fácil):       5:58–6:40/km   ←→ MPR Z1 Leve (6:29–7:28) — sobreposição parcial
  M  (Maratona):    ~5:36/km       ←→ MPR Z2 Confortável (6:05–6:29) — próximo
  T  (Limiar):      5:05–5:15/km   ←→ MPR Z4 Firme (5:20–5:39) — levemente mais forte
  I  (Intervalado): 4:45–4:55/km   ←→ MPR Z5–Z6 (5:01–5:18) — mais conservador MPR

Ao avaliar o treino, mencione explicitamente:
1) Aderência ao plano MPR (zona e duração prescritas pelo treinador)
2) Posicionamento nas zonas Daniels (VDOT 39)
3) Se houver divergência entre os dois sistemas, qual prevalece e por quê
   (regra: MPR define a sessão; Daniels calibra a análise fisiológica)"""


# ── Carregamento do perfil ─────────────────────────────────────

def load_athlete_profile() -> str:
    """
    Lê o arquivo user_profile.md e retorna o conteúdo.
    Retorna string vazia se o arquivo não existir (sem quebrar o pipeline).
    """
    if not PROFILE_PATH.exists():
        logger.warning(f"Perfil do atleta não encontrado em: {PROFILE_PATH}")
        return ""
    try:
        content = PROFILE_PATH.read_text(encoding="utf-8")
        logger.debug(f"Perfil carregado: {PROFILE_PATH.name} ({len(content)} chars)")
        return content
    except Exception as e:
        logger.warning(f"Erro ao ler perfil: {e}")
        return ""


def extract_profile_summary(profile_md: str) -> str:
    """
    Extrai as informações mais relevantes do perfil markdown
    para incluir no prompt de forma concisa.
    """
    if not profile_md:
        return "Perfil não disponível."

    # Extrai seções chave do markdown
    lines = profile_md.split("\n")
    relevant = []
    capture_sections = {
        "## Informações Pessoais",
        "## VDOT Atual",
        "## Plano e Objetivos",
        "## Estado de Saúde",
        "## Princípios de Treino",
    }
    capturing = False

    for line in lines:
        stripped = line.strip()

        # Inicia captura em seções relevantes
        if any(stripped.startswith(s) for s in capture_sections):
            capturing = True
            relevant.append(line)
            continue

        # Para de capturar em nova seção de nível 2 não relevante
        if stripped.startswith("## ") and capturing:
            if not any(stripped.startswith(s) for s in capture_sections):
                capturing = False
            else:
                relevant.append(line)
            continue

        # Captura linhas da seção ativa (até 25 linhas por seção)
        if capturing and stripped:
            relevant.append(line)

    return "\n".join(relevant[:80])  # Limita para não estourar o contexto


# ── Extratores do perfil (restaurados em 10/06/2026 — hotfix) ──
# Estas funções foram acidentalmente removidas numa edição de 09/06.
# Contrato preservado: _get_vdot/_get_fcmax leem o user_profile.md;
# _hr_artifact_warning gera o aviso "POSSÍVEL ARTEFATO" citado nas
# regras do SYSTEM_PROMPT (regra 4 da seção FCmáx).

def _get_vdot(profile_md: str) -> str:
    """
    Extrai o VDOT atual do perfil (ex.: '## VDOT Atual — 39' → '39').
    Retorna 'N/D' se não encontrado.
    """
    if profile_md:
        m = re.search(r"##\s*VDOT\s+Atual\s*[—–-]\s*(\d{2})", profile_md)
        if m:
            return m.group(1)
        m = re.search(r"VDOT\s*(?:atual|=|:)?\s*[~≈]?\s*(\d{2})\b", profile_md, re.IGNORECASE)
        if m:
            return m.group(1)
    return "N/D"


def _get_fcmax(profile_md: str):
    """
    Extrai a FC Máx de referência calibrada do perfil
    (ex.: '| **FC Máx (referência)** | **190 bpm**' → 190).
    Retorna None se não encontrada (formatadores tratam como N/D).
    """
    if profile_md:
        m = re.search(
            r"FC\s*M[áa]x[^|\n]*\(refer[êe]ncia\)[^|\n]*\|\s*\**\s*(\d{3})\s*bpm",
            profile_md, re.IGNORECASE,
        )
        if m:
            return int(m.group(1))
        m = re.search(r"FC\s*M[áa]x[^0-9\n]{0,40}(1[5-9]\d|20\d)\s*bpm", profile_md, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def _hr_artifact_warning(d: dict, fcmax) -> str:
    """
    Gera aviso de possível artefato de FC quando o pico registrado é
    fisiologicamente implausível em relação à FC Máx Referência e à FC média.

    Critério: max_hr >= 98% da FCmáx referência E (max_hr - avg_hr) >= 25 bpm
    (pico isolado destoante da média → provável ruído de fita/sensor óptico).
    Retorna string vazia quando não há suspeita ou dados insuficientes.
    """
    if not isinstance(fcmax, (int, float)) or fcmax <= 0:
        return ""
    max_hr = d.get("max_heart_rate")
    avg_hr = d.get("avg_heart_rate")
    if not max_hr:
        return ""
    if max_hr >= 0.98 * fcmax and (not avg_hr or (max_hr - avg_hr) >= 25):
        return (
            f"\n⚠️ POSSÍVEL ARTEFATO: pico de FC {max_hr:.0f} bpm "
            f"(≥98% da FC Máx Referência {fcmax:.0f} bpm)"
            + (f" destoa da FC média {avg_hr:.0f} bpm" if avg_hr else "")
            + " — tratar como provável ruído de sensor; focar a análise na FC média."
        )
    return ""


# ── Formatadores ───────────────────────────────────────────────

def _fmt(value, suffix: str = "", default: str = "N/D") -> str:
    if value is None:
        return default
    return f"{value}{suffix}"


def _fmt_swim_laps(laps: list) -> str:
    """Formata voltas de natação para o prompt."""
    if not laps:
        return "  Não disponível"
    lines = []
    for lap in laps[:20]:  # Até 20 voltas para natação
        parts = [f"  Volta {lap['lap_number']}:"]
        if lap.get("distance_km"):
            parts.append(str(lap["distance_km"]))   # já está como "25m" ou similar
        if lap.get("duration"):
            parts.append(f"| {lap['duration']}")
        if lap.get("avg_pace"):
            parts.append(f"| {lap['avg_pace']}")
        if lap.get("avg_heart_rate"):
            parts.append(f"| {lap['avg_heart_rate']} bpm")
        if lap.get("avg_cadence_spm"):
            parts.append(f"| {lap['avg_cadence_spm']} braç/min")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _fmt_laps(laps: list) -> str:
    """
    Formata lista de laps para o prompt do Claude.

    Suporta laps brutos do Strava (campos: lap_index, average_speed,
    average_heartrate, average_cadence, distance, moving_time, pace_zone)
    e laps processados (campos legados: lap_number, avg_pace, avg_heart_rate).

    Cap: 20 voltas (suficiente para séries com aquecimento + tiros + descanso).
    """
    if not laps:
        return "  Não disponível"
    lines = []
    for lap in laps[:20]:
        # ── Índice / número da volta ─────────────────────────────
        lap_num = lap.get("lap_index") or lap.get("lap_number") or lap.get("split", "?")
        parts = [f"  Volta {lap_num}:"]

        # ── Distância ────────────────────────────────────────────
        dist_raw = lap.get("distance")          # metros (Strava raw)
        dist_km  = lap.get("distance_km")       # km (processado)
        if dist_raw is not None and dist_km is None:
            dist_km = dist_raw / 1000.0
        if dist_km is not None:
            parts.append(f"{dist_km:.2f} km")

        # ── Duração ──────────────────────────────────────────────
        dur_s  = lap.get("moving_time") or lap.get("elapsed_time")   # segundos (Strava)
        dur_fmt = lap.get("duration")                                  # string (processado)
        if dur_s is not None and dur_fmt is None:
            m, s = divmod(int(dur_s), 60)
            dur_fmt = f"{m}:{s:02d}"
        if dur_fmt:
            parts.append(f"| {dur_fmt}")

        # ── Pace ─────────────────────────────────────────────────
        speed_ms = lap.get("average_speed")     # m/s (Strava raw)
        pace_str = lap.get("avg_pace")          # string (processado)
        if speed_ms is not None and pace_str is None:
            pace_str = _speed_to_pace_str(speed_ms)
        if pace_str:
            parts.append(f"| {pace_str}")

        # ── FC ───────────────────────────────────────────────────
        hr = lap.get("average_heartrate") or lap.get("avg_heart_rate")
        if hr:
            parts.append(f"| {hr:.0f} bpm")

        # ── Cadência ─────────────────────────────────────────────
        cad = lap.get("average_cadence") or lap.get("avg_cadence_spm")
        if cad:
            parts.append(f"| {cad:.0f} spm")

        # ── Pace zone (Strava) ───────────────────────────────────
        pz = lap.get("pace_zone")
        if pz:
            parts.append(f"| Z{pz}")

        lines.append(" ".join(parts))
    return "\n".join(lines)


def _parse_mpr_prescription(structure_str: str) -> dict:
    """
    Parses an MPR structure string like:
      "• 15:00 Z1 | • 7x(0:45 Z4 + 0:45 Z1) | • 4x(4:00 Z4 + 2:00 Z1) | • 10:00 Z1"

    Returns dict with keys (all optional):
      warmup_min, cooldown_min,
      short_reps_count, short_rep_hard_s, short_rep_easy_s,
      long_reps_count,  long_rep_hard_s,  long_rep_easy_s
    """
    import re
    result: dict = {}
    if not structure_str:
        return result

    parts = [p.strip().lstrip("•").strip() for p in structure_str.split("|")]

    def _to_min(t: str) -> float:
        try:
            m, s = t.strip().split(":")
            return int(m) + int(s) / 60.0
        except Exception:
            return 0.0

    def _to_s(t: str) -> int:
        try:
            m, s = t.strip().split(":")
            return int(m) * 60 + int(s)
        except Exception:
            return 0

    simple_pat = re.compile(r"^(\d+:\d+)\s+Z\d+$")
    rep_pat    = re.compile(r"^(\d+)x\((\d+:\d+)\s+Z\d+\s*\+\s*(\d+:\d+)\s+Z\d+\)$")

    for i, part in enumerate(parts):
        s_m = simple_pat.match(part)
        r_m = rep_pat.match(part)
        if s_m:
            dur = _to_min(s_m.group(1))
            if i == 0:
                result["warmup_min"] = dur
            elif i == len(parts) - 1:
                result["cooldown_min"] = dur
        elif r_m:
            count   = int(r_m.group(1))
            hard_s  = _to_s(r_m.group(2))
            easy_s  = _to_s(r_m.group(3))
            key = "short" if hard_s <= 90 else "long"
            result.setdefault(f"{key}_reps_count",   count)
            result.setdefault(f"{key}_rep_hard_s",   hard_s)
            result.setdefault(f"{key}_rep_easy_s",   easy_s)

    return result


def _build_structured_execution_summary(laps: list, mpr_structure: str = "") -> str:
    """
    Builds a deterministic STRUCTURED EXECUTION SUMMARY from ALL FIT laps.

    Uses time/phase-based segmentation (NOT km splits) to determine:
      • Warmup   — laps before first fast lap
      • Tiros curtos — fast laps with dur ≤ 90s
      • Tiros longos — fast laps with dur > 90s
      • Desaquecimento — laps after last fast lap

    Compares executed durations against MPR prescription when available.
    Returns a formatted block to prepend to the Claude prompt.
    """
    if not laps:
        return ""

    # Normalize all laps (no cap — entire workout)
    normalized = []
    for lap in laps:
        speed_ms = lap.get("average_speed")
        pace_str = lap.get("avg_pace")
        if speed_ms is not None:
            pace_str = _speed_to_pace_str(speed_ms) or pace_str
        pace_min = None
        if pace_str and "/km" in pace_str:
            try:
                p, s = pace_str.replace("/km", "").split(":")
                pace_min = int(p) + int(s) / 60.0
            except Exception:
                pass

        # Distance: Strava → "distance" (metres), fit_reader → "distance_km"
        dist_m = lap.get("distance") or (lap.get("distance_km", 0) * 1000)

        # Duration: Strava → "moving_time"/"elapsed_time" (seconds),
        #           fit_reader → "duration" formatted "M:SS" or "H:MM:SS"
        dur_s = lap.get("moving_time") or lap.get("elapsed_time") or 0
        if not dur_s:
            dur_str = lap.get("duration") or ""
            if dur_str:
                parts = dur_str.split(":")
                try:
                    if len(parts) == 2:
                        dur_s = int(parts[0]) * 60 + int(parts[1])
                    elif len(parts) == 3:
                        dur_s = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                except Exception:
                    dur_s = 0

        hr = lap.get("average_heartrate") or lap.get("avg_heart_rate")

        normalized.append({
            "dist_m":   dist_m or 0,
            "dur_s":    dur_s  or 0,
            "pace_min": pace_min,
            "hr":       hr,
        })

    valid_paces = [n["pace_min"] for n in normalized if n["pace_min"] and n["pace_min"] < 15]
    if not valid_paces:
        return ""

    pace_min_all   = min(valid_paces)
    fast_threshold = pace_min_all * 1.20

    is_fast = [
        (n["pace_min"] is not None and n["pace_min"] <= fast_threshold and n["dist_m"] >= 100)
        for n in normalized
    ]

    def _meaningful(i: int) -> bool:
        return is_fast[i] and normalized[i]["dist_m"] >= 100

    first_fast = next((i for i in range(len(is_fast)) if _meaningful(i)), None)
    last_fast  = next((i for i in range(len(is_fast) - 1, -1, -1) if _meaningful(i)), 0)

    if first_fast is None:
        return ""

    warmup_laps    = [normalized[i] for i in range(first_fast)]
    cooldown_laps  = [normalized[i] for i in range(last_fast + 1, len(normalized))]
    fast_laps      = [normalized[i] for i in range(first_fast, last_fast + 1) if is_fast[i]]

    short_fast = [l for l in fast_laps if l["dur_s"] <= 90]
    long_fast  = [l for l in fast_laps if l["dur_s"] > 90]

    def _agg(laps_list: list) -> dict | None:
        if not laps_list:
            return None
        dist  = sum(l["dist_m"] for l in laps_list) / 1000
        secs  = sum(l["dur_s"]  for l in laps_list)
        paces = [l["pace_min"] for l in laps_list if l["pace_min"]]
        hrs   = [l["hr"]       for l in laps_list if l["hr"]]
        avg_p = ""
        avg_h = ""
        if paces:
            ap = sum(paces) / len(paces)
            pm = int(ap); ps = int(round((ap - pm) * 60))
            avg_p = f"{pm}:{ps:02d}/km"
        if hrs:
            avg_h = f"{sum(hrs)/len(hrs):.0f} bpm"
        m, s = divmod(int(secs), 60)
        return {"dist": dist, "m": m, "s": s, "secs": secs, "pace": avg_p, "hr": avg_h, "count": len(laps_list)}

    wu = _agg(warmup_laps)
    cd = _agg(cooldown_laps)
    sh = _agg(short_fast)
    lo = _agg(long_fast)

    rx = _parse_mpr_prescription(mpr_structure)

    lines = ["  EXECUÇÃO ESTRUTURADA — segmentos derivados de laps Garmin (NÃO de splits de km):"]
    lines.append("  " + "─" * 70)

    # Warmup
    if wu:
        rx_wu_min = rx.get("warmup_min", 0)
        ex_str    = f"{wu['m']}:{wu['s']:02d}"
        rx_str    = f"{int(rx_wu_min)}:00" if rx_wu_min else ""
        status    = "✅" if not rx_wu_min or wu["secs"] >= rx_wu_min * 60 * 0.80 else "⚠️"
        detail    = f"[prescrito {rx_str} / executado {ex_str}]" if rx_str else f"[executado {ex_str}]"
        lines.append(
            f"    Aquecimento:    {wu['dist']:.2f} km | {ex_str}"
            + (f" | {wu['pace']}" if wu["pace"] else "")
            + (f" | FC {wu['hr']}" if wu["hr"] else "")
            + f"  {detail}  {status}"
        )

    # Short reps
    if sh:
        rx_n   = rx.get("short_reps_count", 0)
        status = "✅" if not rx_n or sh["count"] >= rx_n else f"⚠️ ({sh['count']}/{rx_n})"
        lines.append(
            f"    Tiros curtos:   {sh['count']} tiros"
            + (f" (prescrito {rx_n})" if rx_n else "")
            + (f" | pace médio {sh['pace']}" if sh["pace"] else "")
            + (f" | FC média {sh['hr']}" if sh["hr"] else "")
            + f"  {status}"
        )

    # Long reps
    if lo:
        rx_n   = rx.get("long_reps_count", 0)
        status = "✅" if not rx_n or lo["count"] >= rx_n else f"⚠️ ({lo['count']}/{rx_n})"
        lines.append(
            f"    Tiros longos:   {lo['count']} tiros"
            + (f" (prescrito {rx_n})" if rx_n else "")
            + (f" | pace médio {lo['pace']}" if lo["pace"] else "")
            + (f" | FC média {lo['hr']}" if lo["hr"] else "")
            + f"  {status}"
        )

    # Cooldown
    if cd:
        rx_cd_min  = rx.get("cooldown_min", 0)
        ex_str     = f"{cd['m']}:{cd['s']:02d}"
        rx_str     = f"{int(rx_cd_min)}:00" if rx_cd_min else ""
        pct        = (cd["secs"] / (rx_cd_min * 60)) if rx_cd_min else 1.0
        if pct >= 0.80:
            status, warn = "✅", ""
        elif pct >= 0.50:
            status, warn = "⚠️", " — abaixo do prescrito"
        else:
            status, warn = "❌", " — insuficiente"
        detail = f"[prescrito {rx_str} / executado {ex_str}]" if rx_str else f"[executado {ex_str}]"
        lines.append(
            f"    Desaquecimento: {cd['dist']:.2f} km | {ex_str}"
            + (f" | {cd['pace']}" if cd["pace"] else "")
            + (f" | FC {cd['hr']}" if cd["hr"] else "")
            + f"  {detail}{warn}  {status}"
        )
    else:
        lines.append("    Desaquecimento: não detectado  ⚠️")

    lines.append("  " + "─" * 70)
    lines.append(
        "  REGRA ABSOLUTA: Claude deve usar EXCLUSIVAMENTE os segmentos acima para avaliar "
        "aquecimento/tiros/desaquecimento. Cooldown_status e warmup_status vêm desta tabela. "
        "NÃO inferir fases por splits de km — splits destroem a semântica de treinos estruturados."
    )
    return "\n".join(lines)


def _fmt_structured_blocks(laps: list, mpr_prescription: str = "") -> str:
    """
    Reconstrói a semântica de blocos de um treino estruturado a partir dos laps.

    Classifica cada volta em:
      • AQUECIMENTO  — paces lentos no início (Z1 ou pace_zone ≤ 1)
      • BLOCO FORTE  — paces rápidos (zona 2+ ou pace_zone ≥ 2)
      • RECUPERAÇÃO  — lap curto (<200m ou <60s) entre blocos fortes
      • DESAQUECIMENTO — paces lentos no final

    Retorna texto formatado para o prompt do Claude, com:
      - Tabela de blocos detectados
      - Estatísticas de cada fase (pace médio, FC média, nº de tiros)
      - Prescrição original do MPR para comparação inline

    Suporta laps brutos do Strava (average_speed, average_heartrate, etc.)
    """
    if not laps:
        return "  Laps não disponíveis — verifique se a atividade tem dados de intervalo."

    # ── 1. Normalizar laps ───────────────────────────────────────
    normalized = []
    for lap in laps[:60]:   # BUG FIX: was [:25] — truncated cooldown laps in 28-lap structured workouts
        speed_ms  = lap.get("average_speed")
        pace_str  = lap.get("avg_pace")
        if speed_ms is not None:
            pace_str = _speed_to_pace_str(speed_ms) or pace_str
        pace_min  = None
        if pace_str and "/km" in pace_str:
            try:
                p, s = pace_str.replace("/km", "").split(":")
                pace_min = int(p) + int(s) / 60.0
            except Exception:
                pass

        dist_m   = lap.get("distance") or (lap.get("distance_km", 0) * 1000)
        # Duration: Strava → moving_time/elapsed_time (sec); fit_reader → "duration" "M:SS"/"H:MM:SS"
        dur_s    = lap.get("moving_time") or lap.get("elapsed_time") or 0
        if not dur_s:
            _ds = lap.get("duration") or ""
            if _ds:
                _p = _ds.split(":")
                try:
                    dur_s = int(_p[0])*3600+int(_p[1])*60+int(_p[2]) if len(_p)==3 else int(_p[0])*60+int(_p[1])
                except Exception:
                    dur_s = 0
        hr       = lap.get("average_heartrate") or lap.get("avg_heart_rate")
        cad      = lap.get("average_cadence") or lap.get("avg_cadence_spm")
        pz       = lap.get("pace_zone") or 0
        idx      = lap.get("lap_index") or lap.get("lap_number") or lap.get("split", 0)

        normalized.append({
            "idx": idx, "dist_m": dist_m or 0, "dur_s": dur_s,
            "pace_min": pace_min, "pace_str": pace_str, "hr": hr,
            "cad": cad, "pace_zone": pz,
        })

    if not normalized:
        return "  Não foi possível normalizar laps."

    # ── 2. Classificar cada lap em fase ──────────────────────────
    # Heurística: calcula limiares de pace a partir dos próprios laps
    valid_paces = [n["pace_min"] for n in normalized if n["pace_min"] and n["pace_min"] < 15]
    if not valid_paces:
        return "  Dados de pace insuficientes nos laps."

    pace_min_all = min(valid_paces)
    pace_max_all = max(valid_paces)
    # Threshold: laps com pace dentro de 20% do mais rápido são "fortes"
    fast_threshold = pace_min_all * 1.20

    # Identifica blocos fortes e recuperações
    is_fast = [
        (n["pace_min"] is not None and n["pace_min"] <= fast_threshold)
        or (n["pace_zone"] >= 2 and n["dist_m"] > 200)
        for n in normalized
    ]

    # Determina início e fim do bloco principal
    # Ignora laps com distância < 100m (GPS artifacts no final) ao calcular last_fast
    def _is_meaningful(i: int) -> bool:
        return is_fast[i] and normalized[i]["dist_m"] >= 100

    first_fast = next((i for i in range(len(is_fast)) if _is_meaningful(i)), None)
    last_fast  = next((i for i in range(len(is_fast) - 1, -1, -1) if _is_meaningful(i)), 0)

    # Categorias
    phases = []
    for i, n in enumerate(normalized):
        if first_fast is None:
            phase = "CONTÍNUO"
        elif i < first_fast:
            phase = "AQUECIMENTO"
        elif i > last_fast:
            phase = "DESAQUECIMENTO"
        elif is_fast[i]:
            phase = "BLOCO FORTE"
        elif (
            # Lap lento entre dois laps rápidos = recuperação ativa
            i > 0 and i < len(normalized) - 1
            and any(is_fast[j] for j in range(max(0, i-2), i))
            and any(is_fast[j] for j in range(i+1, min(len(is_fast), i+3)))
        ) or n["dist_m"] < 300 or n["dur_s"] < 90:
            phase = "RECUPERAÇÃO"
        else:
            phase = "MODERADO"
        phases.append(phase)

    # ── 3. Formatar tabela de laps ────────────────────────────────
    lines = []
    if mpr_prescription:
        lines.append(f"  Prescrição MPR: {mpr_prescription}")
        lines.append("")

    lines.append(f"  {'Volta':<6} {'Fase':<16} {'Dist':>7} {'Tempo':>7} {'Pace':>10} {'FC':>7} {'Cad':>6} {'PZ':>4}")
    lines.append("  " + "─" * 68)

    for n, phase in zip(normalized, phases):
        dist_str  = f"{n['dist_m']/1000:.2f}km" if n["dist_m"] else "  ?"
        m, s      = divmod(int(n["dur_s"]), 60) if n["dur_s"] else (0, 0)
        dur_str   = f"{m}:{s:02d}" if n["dur_s"] else "  ?"
        pace_disp = n["pace_str"] or "  ?"
        hr_disp   = f"{n['hr']:.0f}" if n["hr"] else "  ?"
        cad_disp  = f"{n['cad']:.0f}" if n["cad"] else "  ?"
        pz_disp   = f"Z{int(n['pace_zone'])}" if n["pace_zone"] else "  ?"
        lines.append(
            f"  {n['idx']:<6} {phase:<16} {dist_str:>7} {dur_str:>7} "
            f"{pace_disp:>10} {hr_disp:>7} {cad_disp:>6} {pz_disp:>4}"
        )

    # ── 4. Resumo por fase ────────────────────────────────────────
    lines.append("")
    lines.append("  RESUMO POR FASE:")
    for phase_label in ["AQUECIMENTO", "BLOCO FORTE", "RECUPERAÇÃO", "MODERADO", "DESAQUECIMENTO", "CONTÍNUO"]:
        group = [n for n, p in zip(normalized, phases) if p == phase_label]
        if not group:
            continue
        tot_dist = sum(g["dist_m"] for g in group) / 1000
        tot_time = sum(g["dur_s"] for g in group)
        m, s     = divmod(int(tot_time), 60)
        avg_paces = [g["pace_min"] for g in group if g["pace_min"]]
        avg_hrs   = [g["hr"] for g in group if g["hr"]]
        avg_p_str = ""
        avg_h_str = ""
        if avg_paces:
            ap = sum(avg_paces) / len(avg_paces)
            pm = int(ap); ps = int(round((ap - pm) * 60))
            avg_p_str = f" | pace médio {pm}:{ps:02d}/km"
        if avg_hrs:
            avg_h_str = f" | FC média {sum(avg_hrs)/len(avg_hrs):.0f} bpm"
        count_str = f"({len(group)} laps)" if len(group) > 1 else "(1 lap)"
        lines.append(
            f"    {phase_label:<16} {count_str:<10} "
            f"{tot_dist:.2f} km | {m}:{s:02d}{avg_p_str}{avg_h_str}"
        )

    return "\n".join(lines)


# ── Builder principal ──────────────────────────────────────────

def build_user_message(training_data: dict) -> str:
    """
    Monta a mensagem do usuário para o Claude com:
    - Dados completos do treino
    - Perfil real do atleta (VDOT, zonas, objetivo, fase)
    Detecta automaticamente se é corrida ou natação (cross-training).
    """
    sport = training_data.get("sport", "run").lower()
    if sport == "swim":
        return _build_swim_message(training_data)
    return _build_run_message(training_data)


def _build_run_message(d: dict) -> str:
    """Monta prompt para análise de corrida."""
    # Temperatura
    temp = d.get("avg_temperature_c")
    temp_str = f"{temp}°C" if temp is not None else "N/D"

    # Umidade / Dew Point / Heat Index (vindos do weather_client)
    humidity    = d.get("humidity_pct")
    dew_point   = d.get("dew_point_c")
    heat_index  = d.get("heat_index_c")
    weather_src = d.get("weather_source")  # "open-meteo" | "cache" | "fallback" | None

    clima_linhas = []
    if humidity is not None:
        clima_linhas.append(f"Umidade Relativa: {humidity}%")
    if dew_point is not None:
        clima_linhas.append(f"Ponto de Orvalho: {dew_point}°C")
    if heat_index is not None:
        clima_linhas.append(f"Sensação Térmica (Heat Index): {heat_index}°C")
    clima_block = ("\n" + "\n".join(clima_linhas)) if clima_linhas else ""

    # Training Effect / Suffer Score
    extra_metrics = ""
    aero = d.get("aerobic_training_effect")
    anae = d.get("anaerobic_training_effect")
    tss  = d.get("training_stress_score")
    if aero is not None:
        extra_metrics += f"\nTraining Effect Aeróbico:    {aero:.1f}/5.0"
    if anae is not None:
        extra_metrics += f"\nTraining Effect Anaeróbico:  {anae:.1f}/5.0"
    if tss is not None:
        extra_metrics += f"\nSuffer Score / TSS:          {tss:.0f}"

    # Fonte dos dados
    source = d.get("source", "garmin")
    source_label  = "Strava" if source == "strava" else "Garmin"
    activity_name = d.get("activity_name", "")
    activity_line = f"\nNome da Atividade: {activity_name}" if activity_name else ""
    is_treadmill  = d.get("is_treadmill", False)

    # ── Potência (Stryd) ──────────────────────────────────────
    avg_w   = d.get("avg_watts")
    max_w   = d.get("max_watts")
    np_w    = d.get("weighted_avg_watts")   # normalized power equivalente
    kj      = d.get("kilojoules")
    dev_w   = d.get("device_watts", False)  # True = sensor real

    if avg_w is not None:
        watts_source = "sensor real (Stryd/Garmin)" if dev_w else "estimado pelo Strava"
        _sm      = d.get("stryd_metrics") or {}
        _re      = _sm.get("running_effectiveness")
        _vi_pct  = _sm.get("power_variability_pct")
        _vi_cls  = _sm.get("vi_classification")
        _form_w  = d.get("avg_form_power_w")
        _pw_hr_w = (d.get("drift_analysis") or {}).get("pw_hr_decoupling_pct")

        _vi_line = ""
        if _vi_pct is not None:
            _vi_cls_txt = f" — {_vi_cls}" if _vi_cls else ""
            _vi_line = f"VI (Variab.Índex):     {_vi_pct:.1f}%{_vi_cls_txt}\n"

        watts_block = (
            f"\n── Potência (Running Power) ─────\n"
            f"Potência Média:        {avg_w:.0f} W  [{watts_source}]\n"
            f"Potência Máxima:       {_fmt(max_w, ' W')}\n"
            f"Potência NP*:          {_fmt(np_w, ' W')}  (* weighted avg — análogo ao NP)\n"
            f"Trabalho Total:        {_fmt(kj, ' kJ')}\n"
            + (f"Running Effectiv.(RE): {_re:.4f} m/s/W\n" if _re is not None else "")
            + _vi_line
            + (f"Form Power:            {_form_w:.0f} W  ({100*_form_w/avg_w:.0f}% do total)\n" if _form_w is not None and avg_w else "")
            + (f"Decoupling W/FC:       {_pw_hr_w:+.1f}%\n" if _pw_hr_w is not None else "")
        )
    else:
        watts_block = ""

    # ── Altimetria ────────────────────────────────────────────
    ascent  = d.get("total_ascent_m")
    elev_hi = d.get("elev_high_m")
    elev_lo = d.get("elev_low_m")

    if ascent is not None and ascent > 0:
        alt_range = ""
        if elev_hi is not None and elev_lo is not None:
            alt_range = f"  (alt. mín {elev_lo:.0f}m — máx {elev_hi:.0f}m)"
        altimetria_block = (
            f"\n── Altimetria ───────────────────\n"
            f"Ganho de Elevação: {ascent:.0f} m{alt_range}\n"
            f"Obs: Ganho de elevação impacta pace e FC — considere ao comparar com VDOT puro."
        )
    else:
        altimetria_block = ""

    # ── Drift / Fadiga (drift_analyzer) ──────────────────────
    drift = d.get("drift_analysis")
    if drift and drift.get("pattern") != "dados_insuficientes":
        pa_hr  = drift.get("pa_hr_decoupling_pct")
        pw_hr  = drift.get("pw_hr_decoupling_pct")
        d_pow  = drift.get("delta_power_pct")
        d_pac  = drift.get("delta_pace_pct")
        d_cad  = drift.get("delta_cadence_pct")
        h1     = drift.get("first_half", {})
        h2     = drift.get("second_half", {})
        notes_drift = "; ".join(drift.get("notes", []))

        def _fmt_pct(v, label, positive_is_bad=True):
            if v is None:
                return f"  {label}: N/D"
            sign = "▲" if v > 0 else "▼"
            flag = " ⚠️" if (positive_is_bad and v > 5) or (not positive_is_bad and v < -5) else ""
            return f"  {label}: {sign}{abs(v):.1f}%{flag}"

        drift_block = (
            f"\n── Análise de Drift / Fadiga ────\n"
            f"  Padrão detectado: {drift.get('pattern')} (confiança: {drift.get('confidence')})\n"
            + _fmt_pct(pa_hr, "Decoupling Pace/FC", positive_is_bad=True) + "\n"
            + (_fmt_pct(pw_hr, "Decoupling Watts/FC", positive_is_bad=True) + "\n" if pw_hr is not None else "")
            + _fmt_pct(d_pac, "Δ Pace 1ª→2ª metade (+lento)", positive_is_bad=True) + "\n"
            + (_fmt_pct(d_pow, "Δ Potência 1ª→2ª metade", positive_is_bad=True) + "\n" if d_pow is not None else "")
            + _fmt_pct(d_cad, "Δ Cadência 1ª→2ª metade", positive_is_bad=True) + "\n"
            + f"  1ª metade: FC {h1.get('avg_hr', 'N/D'):.0f}bpm | "
              f"Pace {h1.get('avg_pace_min_km', 0):.2f}min/km"
              + (f" | {h1.get('avg_watts', 0):.0f}W" if h1.get("avg_watts") else "") + "\n"
            + f"  2ª metade: FC {h2.get('avg_hr', 'N/D'):.0f}bpm | "
              f"Pace {h2.get('avg_pace_min_km', 0):.2f}min/km"
              + (f" | {h2.get('avg_watts', 0):.0f}W" if h2.get("avg_watts") else "") + "\n"
            + (f"  Obs: {notes_drift}" if notes_drift else "")
        )
        drift_block += (
            "\nRef: Decoupling <5% = excelente controle aeróbico. "
            ">10% = drift significativo. Potência estável com FC crescente = drift cardiovascular clássico."
        )
    else:
        drift_block = ""

    # Perfil do atleta
    profile_md      = load_athlete_profile()
    profile_summary = extract_profile_summary(profile_md)

    # Plano MPR — usa o session_matcher (corrige BUG 1).
    # O matcher consulta o histórico (analyses.json) para garantir que
    # a mesma prescrição não seja reclamada por duas atividades, e
    # diferencia planned_match × rescheduled × extra_session.
    mpr_plan   = load_mpr_plan()
    mpr_match  = match_training_against_plan(d, mpr_plan)
    mpr_block  = build_mpr_context(None, mpr_plan, match_result=mpr_match)

    # ── Ajuste de calor pré-calculado (determinístico) ───────────
    heat_adj_sec, pace_cold_equiv = calculate_heat_adjustment(
        temp_c=temp,
        dew_point_c=dew_point,
        avg_pace=d.get("avg_pace"),
        is_treadmill=is_treadmill,
    )
    heat_adj_line = (
        f"\n── Ajuste de Calor (pré-calculado) ─\n"
        f"Ajuste:                   {heat_adj_sec} seg/km\n"
        f"Pace equiv. frio (~10°C): {pace_cold_equiv}\n"
        f"OBRIGATÓRIO: Use exatamente estes valores nos campos "
        f"'ajuste_calor_segundos_km' e 'pace_equivalente_frio_min_km' do bloco METRICS."
    )

    # Contexto ambiental: esteira vs rua em Recife
    if is_treadmill:
        ambiente_block = (
            f"── Ambiente ─────────────────────\n"
            f"Temperatura:      {temp_str} (ambiente indoor controlado)\n"
            f"Local de Treino:  Esteira indoor — Recife (PE)\n"
            f"Obs: Pace na esteira reflete condição real sem impacto do calor. "
            f"Comparação direta com zonas VDOT {_get_vdot(profile_md)} é válida."
            f"{extra_metrics}{heat_adj_line}"
        )
        modalidade_label = "Corrida / Esteira (Treadmill)"
    else:
        # Observação de calor — destaca Heat Index quando disponível
        if heat_index is not None and heat_index >= 32:
            calor_obs = (
                f"Obs: Heat Index {heat_index}°C indica sobrecarga térmica significativa. "
                f"Aplicar ajuste de pace conforme Heat_Humidity_Adjustments.md "
                f"(tipicamente +10 a +30s/km no T+DP°F) antes de comparar com VDOT puro."
            )
        elif heat_index is not None and heat_index >= 27:
            calor_obs = (
                f"Obs: Heat Index {heat_index}°C já exige ajuste leve de pace "
                f"(~+5 a +15s/km) para comparar com zonas VDOT."
            )
        else:
            calor_obs = "Obs: Paces e FC no calor são naturalmente elevados vs. VDOT puro"

        fonte_clima = f" [fonte: {weather_src}]" if weather_src else ""

        ambiente_block = (
            f"── Ambiente ─────────────────────\n"
            f"Temperatura:      {temp_str}{clima_block}\n"
            f"Local de Treino:  Recife (PE) — clima quente e úmido, treino às 04:40h{fonte_clima}\n"
            f"{calor_obs}{extra_metrics}{heat_adj_line}"
        )
        modalidade_label = f"{d.get('sport', 'run').title()} / {d.get('sub_sport', 'generic').title()}"

    # ── Laps: structured vs. continuous routing ──────────────────
    # Para treinos estruturados (SERIES, FARTLEK, TEMPO, INTERVALADO,
    # LIMIAR, PROGRESSIVO) usamos _fmt_structured_blocks() que reconstrói
    # a semântica de aquecimento/tiros/recuperação/desaquecimento a partir
    # dos laps brutos. Para treinos contínuos usamos _fmt_laps() simples.
    # REGRA CRÍTICA: NÃO analisar treino estruturado por splits de km.
    _STRUCTURED_TYPES = {
        "SERIES", "FARTLEK", "TEMPO", "INTERVALADO",
        "LIMIAR", "PROGRESSIVO", "TIROS",
    }
    _matched_type = ""
    if mpr_match and mpr_match.planned_session:
        _matched_type = (mpr_match.planned_session.type or "").upper()
    _is_structured = any(t in _matched_type for t in _STRUCTURED_TYPES)
    _exec_summary  = ""   # populated below only for structured workouts

    if _is_structured:
        _mpr_prescription = ""
        if mpr_match and mpr_match.planned_session:
            ps = mpr_match.planned_session
            _mpr_prescription = ps.structure or ps.notes or ""

        # ── Deterministic structured execution summary (prepended) ──────────
        # Built from ALL FIT laps (no cap). Gives Claude authoritative
        # warmup / reps / cooldown segment data so it NEVER has to infer
        # these from km splits. See _build_structured_execution_summary().
        _exec_summary = _build_structured_execution_summary(
            d.get("laps", []), mpr_structure=_mpr_prescription
        )

        laps_text = _fmt_structured_blocks(d.get("laps", []), mpr_prescription=_mpr_prescription)
        _laps_section_label = "Análise de Blocos Estruturados"
        _laps_instruction = (
            f"ATENÇÃO — TREINO ESTRUTURADO ({_matched_type}): "
            "analise BLOCO POR BLOCO. A seção 'EXECUÇÃO ESTRUTURADA' acima contém os segmentos "
            "reais derivados dos laps Garmin — use-a como fonte de verdade para aquecimento, "
            "tiros e desaquecimento. A tabela de fases abaixo mostra o detalhamento por volta. "
            "PROIBIDO inferir fase ou duração de aquecimento/desaquecimento por splits de km."
        )
    else:
        laps_text = _fmt_laps(d.get("laps", []))
        _laps_section_label = "Splits por Volta"
        _laps_instruction = (
            f"Use o VDOT atual ({_get_vdot(profile_md)}) e as zonas de pace do perfil para "
            f"avaliar se a intensidade foi adequada. "
            + ("Na esteira, o pace é comparável diretamente ao VDOT puro." if is_treadmill
               else "Considere o ajuste de calor ao interpretar o pace.")
            + (" Dados de potência (Stryd) disponíveis — use-os para análise de eficiência e decoupling." if avg_w is not None else "")
            + " Considere a fase atual do treinamento e o objetivo de prova ao fazer sugestões."
        )

    message = (
        f"Analise o treino abaixo usando o perfil completo do atleta para personalizar o feedback.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"PERFIL DO ATLETA\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{profile_summary}\n"
        f"{mpr_block}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"DADOS DO TREINO ({source_label})\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Arquivo:          {d.get('filename', 'N/D')}{activity_line}\n"
        f"Data/Hora:        {d.get('start_time_local', 'N/D')}\n"
        f"Modalidade:       {modalidade_label}\n\n"
        f"── Dados Principais ─────────────\n"
        f"Distância:        {_fmt(d.get('total_distance_km'), ' km')}\n"
        f"Duração (mov.):   {d.get('duration_formatted', 'N/D')}\n"
        f"Pace Médio:       {d.get('avg_pace', 'N/D')}\n"
        f"Pace Máximo:      {d.get('max_pace', 'N/D')}\n\n"
        f"── Frequência Cardíaca ──────────\n"
        f"FC Média:         {_fmt(d.get('avg_heart_rate'), ' bpm')}\n"
        f"FC Máxima:        {_fmt(d.get('max_heart_rate'), ' bpm')}\n"
        f"FC Máx Referência: {_get_fcmax(profile_md)} bpm (do perfil — calibrada estatisticamente)"
        f"{_hr_artifact_warning(d, _get_fcmax(profile_md))}\n\n"
        f"── Cadência ─────────────────────\n"
        f"Cadência Média:   {_fmt(d.get('avg_cadence_spm'), ' spm')}\n"
        f"{watts_block}{altimetria_block}{drift_block}\n\n"
        f"{ambiente_block}\n\n"
        + (
            f"── EXECUÇÃO ESTRUTURADA ──────────\n"
            f"{_exec_summary}\n\n"
            if _is_structured and _exec_summary else ""
        )
        + f"── {_laps_section_label} ─────────────\n"        f"{laps_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{_laps_instruction}"
    )

    return message


def _build_swim_message(d: dict) -> str:
    """Monta prompt para análise de natação (cross-training)."""
    source       = d.get("source", "strava")
    source_label = "Strava" if source == "strava" else "Garmin"
    activity_name = d.get("activity_name", "Natação")

    dist_m  = d.get("total_distance_m") or (d.get("total_distance_km", 0) * 1000)
    dist_km = d.get("total_distance_km")
    pool    = d.get("pool_length_m")
    pool_str = f"{int(pool)}m" if pool else "N/D"

    tss = d.get("training_stress_score")
    tss_str = f"{tss:.0f}" if tss else "N/D"

    laps_text = _fmt_swim_laps(d.get("laps", []))

    # Perfil do atleta
    profile_md      = load_athlete_profile()
    profile_summary = extract_profile_summary(profile_md)

    message = (
        f"Analise este treino de natação como cross-training aeróbico complementar "
        f"à preparação de maratona do atleta.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"PERFIL DO ATLETA (Corredor)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{profile_summary}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"DADOS DO TREINO ({source_label})\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Atividade:        {activity_name}\n"
        f"Distância:        {f'{dist_km:.2f} km' if dist_km else f'{dist_m:.0f} m'}\n"
        f"Piscina:          {pool_str}\n"
        f"TSS:              {tss_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Séries / Voltas\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{laps_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Avalie a natação como estímulo aeróbico complementar: volume, intensidade relativa "
        f"ao perfil do corredor, e possível impacto na recuperação para os treinos de corrida "
        f"dos próximos dias."
    )

    return message
