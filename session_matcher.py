"""
session_matcher.py — v4.0
Matching baseado em semana + score ponderado entre treinos prescritos (MPR)
e treinos executados (Garmin/Strava).

═══════════════════════════════════════════════════════════════════════
BUG CORRIGIDO (v3.0)
═══════════════════════════════════════════════════════════════════════
Versões anteriores faziam um "match exato por data" incondicional: se havia
prescrição no mesmo dia que o treino executado, o pareamento era imediato,
independente de compatibilidade.

Isso causava o bug relatado em 08/05/2026:
  - Prescrito 07/05: PERCURSO PLANO 51 min Z2   (treino não realizado)
  - Prescrito 08/05: PERCURSO MISTO 90 min Z2   (longão do dia)
  - Executado 08/05: 48 min / 8 km              (treino do dia anterior feito atrasado)
  → Sistema pareou com o longão de 90 min e acusou "55% do prescrito".
  → Correto: parear com o treino de 07/05 (completed_delayed).

═══════════════════════════════════════════════════════════════════════
NOVA LÓGICA — Week-Level Scored Matching (v3.0)
═══════════════════════════════════════════════════════════════════════
1. Para cada atividade, busca TODOS os treinos prescritos na semana ISO
   (seg–dom) que ainda não foram pareados.
2. Calcula um score ponderado para cada par (atividade × prescrição):
      duration : 35%  — proximidade de duração
      type     : 25%  — categoria do treino (curto/longo/qualidade)
      zone     : 25%  — intensidade prescrita vs executada
      date     : 15%  — proximidade da data
3. Escolhe o melhor match. Se score < MIN_MATCH_SCORE → extra_session.
4. Proteção de longão: se a prescrição exige ≥ LONG_RUN_MIN_DURATION_MIN
   e a atividade cobre < LONG_RUN_MIN_COVERAGE_RATIO da duração, e existe
   outro candidato menor ainda pendente → prefere o candidato menor.
5. Classificação pelo delta de dias → ver seção MICROCICLO abaixo.
   Atividade cobre < 70% de longão como único candidato → partially_completed.

Regras de aderência semanal (weekly_adherence_status):
  - "missed" só é atribuído APÓS o fim da semana ISO.
  - Durante a semana: pending/at_risk/behind_schedule, nunca "failed".

═══════════════════════════════════════════════════════════════════════
NOVA CLASSIFICAÇÃO DE MICROCICLO (v4.0)
═══════════════════════════════════════════════════════════════════════
Problema corrigido: o sistema rotulava como "completed_delayed" qualquer
treino executado em dia diferente do prescrito, mesmo quando o atleta
apenas reorganizou a semana de forma fisiologicamente coerente.

A nova lógica pensa como um treinador humano experiente:

  1. ADJUSTED_WITHIN_MICROCYCLE (ajuste normal de agenda):
     - Treino executado na mesma semana ISO
     - Delta dentro da janela de tolerância por tipo de sessão:
         short_easy  : até 3 dias  (Z2 curto/regenerativo — alta tolerância)
         medium_easy : até 2 dias  (Z2 médio — tolerância moderada)
         long_easy   : até 2 dias  (longão — moderada; preserva intenção)
         quality     : até 1 dia   (intervalado/limiar — baixa tolerância)
     - SEM acúmulo indevido de estímulos duros detectado
     - Usa linguagem de "ajuste de agenda", não de "atraso"

  2. COMPLETED_DELAYED (atraso real com impacto fisiológico):
     - Delta EXCEDE a janela de tolerância do tipo, OU
     - Detectado stacking inadequado (duro + duro sem recuperação), OU
     - Semana diferente da prescrição

  3. COMPLETED_EARLY (antecipação):
     - Mantém lógica atual (delta < 0 dentro da semana = antecipação válida)

Detecção de stacking:
  - Verifica atividades nos dias adjacentes (dia anterior e posterior)
  - Se sessão de qualidade adjacente a outra sessão de qualidade → stacking
  - Se longão adjacente a qualidade ou outro longão → stacking
  - Stacking detectado → eleva classificação para completed_delayed mesmo
    dentro da janela de tolerância
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Literal, Optional

logger = logging.getLogger("garmin_coach.session_matcher")


# ── Constantes de matching ──────────────────────────────────────

SCORE_WEIGHTS: dict[str, float] = {
    "duration": 0.35,
    "type":     0.25,
    "zone":     0.25,
    "date":     0.15,
}

# Abaixo deste score, a atividade é classificada como extra_session.
MIN_MATCH_SCORE: float = 0.25

# Prescrição com duração >= este valor é tratada como longão.
LONG_RUN_MIN_DURATION_MIN: int = 70

# Se a atividade executada cobre menos que este percentual de um longão
# prescrito E existe outro candidato mais curto pendente, não parear com o
# longão (evita marcar 48 min como "longão interrompido a 55%").
LONG_RUN_MIN_COVERAGE_RATIO: float = 0.70

# ── Janelas de tolerância para ADJUSTED_WITHIN_MICROCYCLE ──────
# Máximo de dias de deslocamento (abs(delta)) para classificar um treino
# como "ajuste de agenda" em vez de "atraso real".
# Lógica: quanto mais intenso o estímulo, menor a tolerância ao deslocamento.
MICROCYCLE_TOLERANCE_DAYS: dict[str, int] = {
    "short_easy":  3,   # Z2 curto / regenerativo — atleta pode reorganizar livremente
    "medium_easy": 2,   # Z2 médio — tolerância moderada
    "long_easy":   2,   # Longão — moderada (intenção preservada dentro da semana)
    "quality":     1,   # Intervalado / limiar / VO2 — baixa tolerância ao deslocamento
}

# ── Same-day priority lock (v4.1) ──────────────────────────────
# Se existe prescrição no mesmo dia E o score mínimo for atingido,
# a prescrição do dia SEMPRE vence qualquer candidato de outro dia.
# Valor muito baixo (0.20) garante que só falha para sessões totalmente
# incompatíveis (ex.: longão de 2h vs corrida de 20 min de nadador).
SAME_DAY_MIN_SCORE: float = 0.20


# ── Tipos públicos ──────────────────────────────────────────────

PlannedStatus = Literal[
    "pending",
    "completed",
    "adjusted_within_microcycle",   # v4.0 — deslocamento normal sem impacto
    "completed_delayed",
    "completed_early",
    "partially_completed",
    "missed",
    "rescheduled",   # alias legado — mantido para compat. com dados antigos
    "replaced",
    "modified_load",
]

ActivityClassification = Literal[
    "planned_match",
    "adjusted_within_microcycle",   # v4.0 — ajuste de agenda, sem prejuízo fisiológico
    "completed_delayed",
    "completed_early",
    "partially_completed",
    "extra_session",
    "rescheduled",   # alias legado
    "recovery",
    "unknown",
]

WeeklyAdherenceStatus = Literal[
    "on_track",
    "at_risk",
    "behind_schedule",
    "ahead_of_schedule",
    "unknown",
]

ExtraImpact = Literal["beneficial", "neutral", "harmful"]

# Confiança do matcher (Prioridade 4 da auditoria).
# 'high'   = score forte (≥0.70), sem fallbacks (same-day lock, longão swap)
# 'medium' = match razoável mas com uma ressalva (score médio, delta>1, fallback usado)
# 'low'    = match marginal — escolhido por pouco, ou sem prescrição alguma da semana
MatchConfidence = Literal["high", "medium", "low"]

# Categoria interna de intensidade/tipo (não exposta diretamente na API)
_SessionCategory = Literal["short_easy", "medium_easy", "long_easy", "quality"]

# Limiares de confidence (calibrados contra os scores observados em prod).
# 'high'   exige score forte E ausência de fallbacks ativados.
# 'medium' = score "decente" OU score forte com fallback (same-day lock / longão swap).
CONFIDENCE_HIGH_SCORE:   float = 0.70
CONFIDENCE_MEDIUM_SCORE: float = 0.45


# ── Data classes ────────────────────────────────────────────────

@dataclass
class RunningDynamics:
    """
    Métricas de Running Dynamics (GCT, oscilação vertical, etc.).
    Na maioria dos treinos virá com campos None até que os leitores
    FIT/TCX sejam estendidos para extrair esses dados.
    """
    ground_contact_time_ms: Optional[float] = None
    vertical_oscillation_cm: Optional[float] = None
    vertical_ratio_pct: Optional[float] = None
    stride_length_m: Optional[float] = None
    gct_balance_pct: Optional[float] = None


@dataclass
class PlannedSession:
    """Sessão prescrita pelo treinador (origem: mpr_plan.json)."""
    id: str
    date: str                        # ISO YYYY-MM-DD
    type: str                        # ex.: RODAGEM | PERCURSO MISTO | SERIES
    duration_min: Optional[int]
    target_zone: list[str] = field(default_factory=list)   # ['Z1', 'Z2', ...]
    distance_min_km: Optional[float] = None
    distance_max_km: Optional[float] = None
    structure: Optional[str] = None
    notes: Optional[str] = None
    matched_activity_id: Optional[str] = None
    status: PlannedStatus = "pending"


@dataclass
class Activity:
    """Atividade executada (oriunda do fit_reader / strava_reader)."""
    id: str
    date: str                                         # ISO YYYY-MM-DD
    duration_min: Optional[float] = None
    distance_km: Optional[float] = None
    avg_hr: Optional[float] = None
    avg_pace: Optional[str] = None                    # "5:32/km"
    # Pace do bloco mais rápido (menor valor em min/km) entre todos os laps.
    # Preenchido pelo payload_builder a partir de laps de SERIES/estruturado.
    # Permite detectar qualidade mesmo quando avg_pace é diluído por warmup/cooldown.
    min_lap_pace: Optional[str] = None               # "5:24/km" (lap mais rápido)
    source: str = "garmin"                            # garmin | strava
    sport: str = "run"
    is_treadmill: bool = False
    classification: ActivityClassification = "unknown"
    matched_session_id: Optional[str] = None
    running_dynamics: RunningDynamics = field(default_factory=RunningDynamics)


@dataclass
class ExtraSessionEvaluation:
    """Avaliação de um treino extra (não prescrito)."""
    activity_id: str
    date: str
    classification: ExtraImpact
    reason: str
    impact_on_next_sessions: str


@dataclass
class MatchResult:
    """Resultado do matching — payload consumido pelo payload_builder."""
    activity: Activity
    planned_session: Optional[PlannedSession]
    extra_eval: Optional[ExtraSessionEvaluation]
    classification: ActivityClassification
    # Próxima sessão pendente da MPR (após a data da atividade).
    # Usado pelo payload_builder para informar o Claude sobre o que vem a seguir.
    next_pending_session: Optional[PlannedSession] = None

    # ── Auditoria do matching (Prioridade 4) ─────────────────────
    # Score 0–1 do par escolhido. None quando classification ∈
    # {extra_session sem candidatos, unknown}.
    match_score: Optional[float] = None

    # Confiança final (alta/média/baixa) — vai para o dashboard e
    # entra no prompt do Claude para que ele saiba se pode confiar
    # no matching ou se deve mencionar a ambiguidade.
    confidence: MatchConfidence = "low"

    # Lista de motivos textuais que justificam a confidence.
    # Útil para auditoria do dashboard e debugging.
    confidence_reasons: list[str] = field(default_factory=list)

    # Flags de fallback que rebaixam a confiança quando ativadas.
    same_day_lock_used: bool = False    # match forçado pela regra same-day
    long_run_swap_used: bool = False    # longão protection escolheu candidato curto

    @property
    def is_planned(self) -> bool:
        return self.classification in (
            "planned_match", "adjusted_within_microcycle",
            "completed_delayed", "completed_early",
            "partially_completed", "rescheduled",
        )

    @property
    def is_extra(self) -> bool:
        return self.classification == "extra_session"


# ── Helpers de conversão ────────────────────────────────────────

def _to_iso(s: str) -> Optional[str]:
    """Converte 'YYYY-MM-DD…' ou 'DD/MM/YYYY…' → 'YYYY-MM-DD'."""
    if not s:
        return None
    s = s.strip()
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        pass
    from datetime import datetime as _dt
    for fmt in ("%d/%m/%Y", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            return _dt.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _pace_to_minutes(pace: Optional[str]) -> Optional[float]:
    """'5:32/km' → 5.533 min. Retorna None se inválido."""
    if not pace or pace == "N/D":
        return None
    raw = pace.replace("/km", "").replace("/100m", "").strip()
    if ":" not in raw:
        return None
    try:
        mm, ss = raw.split(":")
        return int(mm) + int(ss) / 60
    except (ValueError, TypeError):
        return None


def _duration_str_to_min(dur: Optional[str]) -> Optional[float]:
    """'01:08:09' → 68.15 min ; '40:00' → 40.0 ; None → None."""
    if not dur:
        return None
    parts = str(dur).split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 60 + nums[1] + nums[2] / 60
    if len(nums) == 2:
        return nums[0] + nums[1] / 60
    if len(nums) == 1:
        return nums[0]
    return None


def session_to_planned(session: dict) -> PlannedSession:
    """Adapta um item de mpr_plan.json['sessions'] em PlannedSession.

    Lê o campo 'status' do plano e normaliza:
      "FALTOU" / "missed" → "missed"  (sessão não realizada — não pode ser claimed)
      qualquer outro valor  → mantido como-está; default "pending"
    """
    sid = session.get("id") or session["date"]

    raw_status = (session.get("status") or "pending").strip()
    # Normaliza variantes em português e inglês de "sessão perdida"
    if raw_status.upper() in ("FALTOU", "MISSED"):
        status: PlannedStatus = "missed"
    elif raw_status in (
        "completed", "adjusted_within_microcycle", "completed_delayed",
        "completed_early", "partially_completed", "rescheduled",
        "replaced", "modified_load", "pending",
    ):
        status = raw_status  # type: ignore[assignment]
    else:
        status = "pending"

    return PlannedSession(
        id=sid,
        date=session["date"],
        type=session.get("type", "GENÉRICO"),
        duration_min=session.get("duration_min"),
        target_zone=list(session.get("zones", [])),
        distance_min_km=session.get("distance_min_km"),
        distance_max_km=session.get("distance_max_km"),
        structure=session.get("structure"),
        notes=session.get("notes"),
        status=status,
    )


def existing_to_activity(entry: dict) -> Activity:
    """
    Adapta um item de analyses.json['analyses'] em Activity.
    Suporta tanto o schema novo (mpr_classification / mpr_matched_session_id)
    quanto o schema legado (classification / matched_session_id).
    """
    # Campo de classificação: preferência para 'mpr_classification' (schema novo)
    raw_classif = (
        entry.get("mpr_classification")
        or entry.get("classification")
        or "unknown"
    )
    classif = str(raw_classif).lower()
    valid_classifs = {
        "planned_match", "adjusted_within_microcycle",
        "completed_delayed", "completed_early",
        "partially_completed", "extra_session", "rescheduled",
        "recovery", "unknown",
    }
    if classif not in valid_classifs:
        classif = "unknown"

    # Campo matched_session_id: preferência para 'mpr_matched_session_id'
    matched_id = (
        entry.get("mpr_matched_session_id")
        or entry.get("matched_session_id")
    )

    duration_min = _duration_str_to_min(entry.get("duration"))
    return Activity(
        id=entry.get("id") or f"{entry.get('date','?')}_{entry.get('source','?')}",
        date=entry.get("date") or "",
        duration_min=duration_min,
        distance_km=entry.get("distance_km"),
        avg_hr=entry.get("avg_heart_rate"),
        avg_pace=entry.get("avg_pace"),
        source=entry.get("source", "garmin"),
        sport=entry.get("sport", "run"),
        is_treadmill=bool(entry.get("is_treadmill", False)),
        classification=classif,   # type: ignore[arg-type]
        matched_session_id=matched_id,
    )


# ══════════════════════════════════════════════════════════════════
# Núcleo: SessionMatcher
# ══════════════════════════════════════════════════════════════════

class SessionMatcher:
    """
    Faz o matching entre o plano MPR e as atividades.
    É stateless por design: recebe tudo no construtor e expõe
    métodos puros que retornam estruturas novas.
    """

    def __init__(
        self,
        plan: Optional[dict],
        existing_activities: Iterable[Activity] = (),
    ) -> None:
        self.plan = plan or {}

        # Índice data ISO → PlannedSession
        self._planned_by_date: dict[str, PlannedSession] = {}
        # Índice UUID → PlannedSession  (v4.2 — fix Priority 1 UUID lookup)
        self._planned_by_id: dict[str, PlannedSession] = {}
        for s in self.plan.get("sessions", []):
            ps = session_to_planned(s)
            self._planned_by_date[ps.date] = ps
            self._planned_by_id[ps.id] = ps

        # session_id → activity_id  (sessões já reclamadas)
        self._claimed_sessions: dict[str, str] = {}
        # data ISO → lista de atividades existentes nesse dia
        self._activities_by_date: dict[str, list[Activity]] = {}

        for a in existing_activities:
            if not a.date:
                continue
            self._activities_by_date.setdefault(a.date, []).append(a)

            # CLAIMING HÍBRIDO COM VALIDAÇÃO DE PLAUSIBILIDADE (v4.2+)
            #
            # Regra:
            #   1. UUID plausível (signed_delta em [0, tol+1]): claim pelo UUID.
            #      Preserva completed_delayed/adjusted corretos (ativ. D+1 → sess. D).
            #   2. UUID implausível (delta negativo = atividade ANTES da sessão,
            #      ou delta muito grande): fallback por data da atividade.
            #   3. extra_session nunca reclama nada.
            #
            # Delta NEGATIVO não é aceito para evitar que o padrão "poison"
            # (completed_early em D com matched_session_id de D+1) bloqueie
            # o Tier 1 same-day lock do dia seguinte.
            if a.classification == "extra_session":
                continue

            claimed = False

            if a.matched_session_id:
                ps_uuid = self._planned_by_id.get(a.matched_session_id)
                if ps_uuid and ps_uuid.id not in self._claimed_sessions:
                    try:
                        act_d  = date.fromisoformat(a.date)
                        plan_d = date.fromisoformat(ps_uuid.date)
                        signed_delta = (act_d - plan_d).days  # positivo = atrasado
                        cat       = self._session_category(ps_uuid)
                        tolerance = MICROCYCLE_TOLERANCE_DAYS.get(cat, 1)
                        plausible = 0 <= signed_delta <= tolerance + 1
                    except ValueError:
                        plausible = False

                    if plausible:
                        self._claimed_sessions[ps_uuid.id] = a.id
                        claimed = True
                        logger.debug(
                            f"__init__ claim UUID: act={a.id} ({a.date}) "
                            f"-> sess={ps_uuid.id} ({ps_uuid.date}) "
                            f"delta={signed_delta}"
                        )

            if not claimed:
                ps_date = self._planned_by_date.get(a.date)
                if ps_date and ps_date.id not in self._claimed_sessions:
                    self._claimed_sessions[ps_date.id] = a.id
                    logger.debug(
                        f"__init__ claim DATE: act={a.id} ({a.date}) "
                        f"-> sess={ps_date.id} ({ps_date.date})"
                    )

    # ── Public snapshot helpers ──────────────────────────────────

    def planned_snapshot(
        self, reference_date: Optional[str] = None
    ) -> list[PlannedSession]:
        """
        Retorna snapshot das sessões planejadas.

        Para cada sessão com status "pending" não reclamada cuja semana ISO
        já terminou (reference_date > domingo da semana da sessão), o status
        retornado é derivado como "missed" — sem modificar o estado interno.

        Sessões já com status "missed" explícito (vindas do plano: FALTOU)
        são mantidas como "missed" independentemente.

        Parâmetros:
            reference_date: data ISO de referência (padrão: hoje).
        """
        import copy
        try:
            ref = (date.fromisoformat(reference_date)
                   if reference_date else date.today())
        except ValueError:
            ref = date.today()

        result = []
        for ps in self._planned_by_date.values():
            ps_out = copy.copy(ps)

            if ps_out.status == "pending" and ps_out.id not in self._claimed_sessions:
                try:
                    plan_d = date.fromisoformat(ps_out.date)
                    # Domingo da semana ISO da sessão
                    week_end = plan_d + timedelta(days=(6 - plan_d.weekday()))
                    if ref > week_end:
                        ps_out.status = "missed"
                except ValueError:
                    pass

            result.append(ps_out)

        return result

    # ── Confiança do matching (Prioridade 4) ─────────────────────

    @staticmethod
    def _compute_confidence(
        classification: ActivityClassification,
        score: Optional[float],
        *,
        same_day_lock_used: bool = False,
        long_run_swap_used: bool = False,
        is_partial: bool = False,
        delta_days: int = 0,
        has_plan: bool = True,
        nearest_planned_days: Optional[int] = None,
    ) -> tuple[MatchConfidence, list[str]]:
        """
        Calcula a confiança do match e retorna a justificativa textual.

        Regras (calibradas contra cenários observados em produção):
          * extra_session sem plano disponível → low (não há referência)
          * extra_session com prescrição próxima (≤2 dias) e score borderline → low
            (é "quase um match" — analista humano poderia discordar)
          * extra_session com prescrição distante (>3 dias) → medium
            (geograficamente claro que é extra)
          * planned_match score≥0.70 sem fallbacks → high
          * planned_match score≥0.45 OU score≥0.70 com fallback → medium
          * partially_completed → no máximo medium (cobertura baixa por definição)
          * delta>1 day e classification=delayed/early → rebaixa um nível
          * unknown → low
        """
        reasons: list[str] = []

        # Casos terminais óbvios
        if classification == "unknown":
            return "low", ["classificação 'unknown' — atividade sem data"]

        if classification == "extra_session":
            if not has_plan:
                return "low", ["sem plano MPR carregado"]
            if nearest_planned_days is not None and nearest_planned_days <= 2:
                reasons.append(
                    f"prescrição mais próxima a {nearest_planned_days} dia(s) — "
                    f"borderline com extra"
                )
                return "low", reasons
            reasons.append("nenhuma prescrição disponível na janela próxima")
            return "medium", reasons

        # Daqui em diante: temos um match (planned_match, adjusted_…, etc.)
        if score is None:
            return "low", ["match sem score (caminho legado)"]

        # Base por score
        if score >= CONFIDENCE_HIGH_SCORE:
            level: MatchConfidence = "high"
            reasons.append(f"score forte ({score:.2f} ≥ {CONFIDENCE_HIGH_SCORE})")
        elif score >= CONFIDENCE_MEDIUM_SCORE:
            level = "medium"
            reasons.append(
                f"score moderado ({score:.2f} em "
                f"[{CONFIDENCE_MEDIUM_SCORE}, {CONFIDENCE_HIGH_SCORE}))"
            )
        else:
            level = "low"
            reasons.append(f"score baixo ({score:.2f} < {CONFIDENCE_MEDIUM_SCORE})")

        # Penalidades — cada flag rebaixa no máximo um nível
        def _downgrade(current: MatchConfidence) -> MatchConfidence:
            return {"high": "medium", "medium": "low", "low": "low"}[current]

        if same_day_lock_used:
            reasons.append("same-day lock ativado (regra forçou prescrição do dia)")
            level = _downgrade(level)

        if long_run_swap_used:
            reasons.append("longão swap ativado (atividade muito curta p/ o longão)")
            level = _downgrade(level)

        if is_partial:
            reasons.append("cobertura parcial (<70% da duração prescrita)")
            # Partial não pode ser high mesmo com score alto
            if level == "high":
                level = "medium"

        if abs(delta_days) >= 2 and classification not in ("planned_match",):
            reasons.append(
                f"deslocamento de {abs(delta_days)} dia(s) entre prescrição e execução"
            )
            level = _downgrade(level)

        return level, reasons

    # ── HARD-COMPATIBILITY (Tier 1 & 2) ──────────────────────────
    # Helpers determinísticos: respondem yes/no, sem fração de score.

    @staticmethod
    def _duration_compatible_hard(
        a_dur: Optional[float],
        p_dur: Optional[float],
    ) -> bool:
        """
        Duração compatível para hard same-day match.
        Aceita ratio >= 0.65 OU diferença <= 10min em valor absoluto.
        Atende o caso 14/05: prescrito 49min, executado 61min → ratio 0.80 → ok.
        Atende o caso 13/05: prescrito 50min, executado 51min → ratio 0.98 → ok.
        Rejeita: prescrito 90min vs executado 35min (longão interrompido) — vai pra Tier 3 partial.
        """
        if a_dur is None or p_dur is None:
            return False
        if a_dur <= 0 or p_dur <= 0:
            return False
        ratio = min(a_dur, p_dur) / max(a_dur, p_dur)
        diff = abs(a_dur - p_dur)
        return ratio >= 0.65 or diff <= 10.0

    @staticmethod
    def _distance_compatible_hard(
        a_dist: Optional[float],
        p_dist_min: Optional[float],
        p_dist_max: Optional[float],
    ) -> bool:
        """
        Distância compatível: dentro de ±30% da range prescrita.
        Atende o caso 14/05: 10000m prescrito, 10010m executado → exato.
        """
        if a_dist is None:
            return False
        p_dist = p_dist_max or p_dist_min
        if not p_dist or p_dist <= 0:
            return False
        ratio = min(a_dist, p_dist) / max(a_dist, p_dist)
        return ratio >= 0.70

    def _types_compatible_hard(
        self,
        activity: Activity,
        planned: PlannedSession,
    ) -> bool:
        """
        Compatibilidade DURA de tipo para Tier 1.
        Decisão binária: este executado é plausivelmente este prescrito?

        Regras:
        - Mesma categoria interna (easy×easy, quality×quality, long×long): True
        - short_easy ↔ medium_easy: True (tolerância adjacente)
        - PERCURSO PLANO (Z2): aceita avg_pace em Z1/Z2/Z3 (range plano em Recife)
        - SERIES/INTERVALADO/FARTLEK: True se há min_lap_pace muito mais rápido
          que avg_pace (sinal de estrutura interna preservada)
        - long_easy (longão): exige duração executada >= 70% (senão é parcial,
          vai pra Tier 3)
        """
        a_cat = self._activity_category(activity)
        p_cat = self._session_category(planned)

        if a_cat == p_cat:
            # Caso especial: longão prescrito com cobertura baixa → não-compatível
            if p_cat == "long_easy":
                a_dur = activity.duration_min or 0
                p_dur = planned.duration_min or 0
                if p_dur > 0 and a_dur / p_dur < LONG_RUN_MIN_COVERAGE_RATIO:
                    return False
            return True

        # short_easy ↔ medium_easy: variação de duração, mesmo conceito
        if {a_cat, p_cat} <= {"short_easy", "medium_easy"}:
            return True

        p_type_upper = (planned.type or "").upper()

        # PERCURSO PLANO / RODAGEM: prescrição é "easy contínuo" — aceita
        # qualquer atividade short/medium easy mas NÃO longão (longão é p_cat).
        # MISTO é tratado como long_easy (longão), não como plano.
        if "PLANO" in p_type_upper or "RODAGEM" in p_type_upper:
            if a_cat in ("short_easy", "medium_easy"):
                return True

        # Estruturado (intervalos/séries com tiros): aceita se há blocos quality
        if p_type_upper in ("SERIES", "INTERVALADO", "FARTLEK", "TEMPO", "LIMIAR", "PROGRESSIVO"):
            if self._has_quality_blocks(activity):
                return True
            # Aceita também se a categoria detectada for quality (avg_pace rápido)
            if a_cat == "quality":
                return True

        return False

    @staticmethod
    def _has_quality_blocks(activity: Activity) -> bool:
        """
        Detecta se a atividade tem blocos de quality (estrutura interna)
        comparando min_lap_pace com avg_pace.

        Se o lap mais rápido é >= 30s/km mais rápido que o avg, é estruturada.
        Cobre o caso 12/05: SERIES Z1+Z3+Z1 com avg 5:53 mas tiros Z3 a ~5:40
        e blocos Z1 a ~6:30. min_lap << avg_pace → True.
        """
        avg_min = _pace_to_minutes(activity.avg_pace)
        min_lap_min = _pace_to_minutes(activity.min_lap_pace)
        if avg_min is None or min_lap_min is None:
            return False
        return (avg_min - min_lap_min) >= 0.5   # 30s/km de diferença

    def _is_compatible_hard(
        self,
        activity: Activity,
        planned: PlannedSession,
    ) -> bool:
        """
        Tier 1 hard compatibility check. Retorna True se a atividade
        do MESMO DIA satisfaz TODOS os critérios duros:

            (duração compatível OR distância compatível) AND tipo compatível

        Decisão binária. Sem score, sem confidence.
        """
        dur_ok = self._duration_compatible_hard(
            activity.duration_min, planned.duration_min,
        )
        dist_ok = self._distance_compatible_hard(
            activity.distance_km,
            planned.distance_min_km,
            planned.distance_max_km,
        )
        if not (dur_ok or dist_ok):
            return False
        return self._types_compatible_hard(activity, planned)

    @staticmethod
    def _is_structured(planned: PlannedSession) -> bool:
        """
        Tier 2: sessão considerada estruturada (tem blocos prescritos com
        zonas diferentes — exige interpretação por bloco para análise correta).

        PERCURSO PLANO / PERCURSO MISTO / RODAGEM são treinos CONTÍNUOS
        em zona única → NÃO são structured (Tier 1 cobre eles).
        """
        t = (planned.type or "").upper()
        if t in ("SERIES", "INTERVALADO", "FARTLEK", "TEMPO", "LIMIAR", "PROGRESSIVO"):
            return True
        # Estrutura explícita com pipes (vários blocos com zonas diferentes)
        struct = planned.structure or ""
        if "|" in struct:
            # Verifica que tem zonas diferentes no mesmo treino (não só Z1+Z1+Z1)
            zones = set()
            for chunk in struct.split("|"):
                for z in ("Z1", "Z2", "Z3", "Z4", "Z5", "Z6", "Z7", "Z8", "Z9"):
                    if z in chunk:
                        zones.add(z)
            if len(zones) >= 2:
                return True
        return False

    def _is_compatible_structured_same_day(
        self,
        activity: Activity,
        planned: PlannedSession,
    ) -> bool:
        """
        Tier 2 — same-day estruturado.

        Critério mais permissivo que Tier 1: só exige duração compatível em
        tolerância expandida (±50% ou diff <= 15min) e estruturação ou tipo
        compatível. Isto cobre os casos onde o avg_pace cai fora do Z3
        prescrito porque os Z1 de aquecimento/desaquecimento dominam — mas
        a sessão ESTRUTURALMENTE foi executada.
        """
        a_dur = activity.duration_min
        p_dur = planned.duration_min
        if a_dur is None or p_dur is None:
            return False
        if a_dur <= 0 or p_dur <= 0:
            return False
        ratio = min(a_dur, p_dur) / max(a_dur, p_dur)
        diff = abs(a_dur - p_dur)
        if not (ratio >= 0.50 or diff <= 15.0):
            return False
        # Para estruturado: aceitamos qualquer atividade que case duração;
        # o tipo "compatível" aqui é a presença OBJETIVA da prescrição
        # no mesmo dia. O painel do dashboard e o prompt do Claude vão
        # explorar a estrutura na análise textual.
        return True

    def _commit_match(
        self,
        activity: Activity,
        planned_session: PlannedSession,
        *,
        classification: "ActivityClassification",
        plan_status: "PlannedStatus",
        match_score: Optional[float],
        tier: int,
    ) -> MatchResult:
        """
        Persiste o match no estado interno e produz o MatchResult com
        confidence determinada pelo tier:
          - Tier 1 ou 2: confidence sempre 'high' (decisão hard).
          - Tier 3: confidence calculada por score + flags.
        """
        planned_session.matched_activity_id = activity.id
        planned_session.status = plan_status
        self._claimed_sessions[planned_session.id] = activity.id
        activity.matched_session_id = planned_session.id
        activity.classification = classification

        next_pending = self.next_pending_session(activity.date)

        if tier in (1, 2):
            confidence: MatchConfidence = "high"
            reasons = [
                f"tier{tier} hard same-day lock — "
                f"{'compatibilidade dura' if tier == 1 else 'sessão estruturada'}"
            ]
        else:
            # Tier 3 path — usa o cálculo probabilístico padrão
            confidence, reasons = self._compute_confidence(
                classification,
                match_score,
                has_plan=True,
            )

        return MatchResult(
            activity=activity,
            planned_session=planned_session,
            extra_eval=None,
            classification=classification,
            next_pending_session=next_pending,
            match_score=match_score,
            confidence=confidence,
            confidence_reasons=reasons,
            same_day_lock_used=(tier in (1, 2)),
            long_run_swap_used=False,
        )

    # ── API principal ────────────────────────────────────────────

    def match(self, activity: Activity) -> MatchResult:
        """
        Matcher hierárquico (correção arquitetural pós-incidente 14/05/2026).

        Cascata determinística com hard constraints PRIMEIRO, scoring SEGUNDO:

            Tier 1 — HARD SAME-DAY LOCK
                ┃ Se há prescrição não-claimed no mesmo dia
                ┃ E (duração OU distância compatíveis)
                ┃ E tipo compatível
                ┃ → planned_match imediato, BYPASS scoring, BYPASS confidence
                ┃   ambiguity. Confidence = high (decisão determinística).
                ┃
            Tier 2 — STRUCTURED SAME-DAY LOCK
                ┃ Para SERIES/INTERVALADO/FARTLEK/TEMPO/PERCURSO PLANO no
                ┃ mesmo dia: aceita variação de pace dentro do range prescrito
                ┃ (Z1+Z3 com avg em Z2 ainda é mesmo dia). Critério mais leve
                ┃ de duração (±50%). Confidence = high.
                ┃
            Tier 3 — DELAYED / EARLY (probabilístico, AQUI é onde scoring vive)
                ┃ Se Tier 1+2 falharem, usa o scoring atual (duração×tipo×
                ┃ zona×data) para encontrar match em outro dia. Confidence
                ┃ reflete a ambiguidade real.
                ┃
            Tier 4 — EXTRA SESSION
                ┃ Última opção. Score < MIN_MATCH_SCORE em qualquer candidato
                ┃ ou semana esgotada.

        Regra de invariante: confidence é metadata SECUNDÁRIA. Em Tier 1/2,
        confidence nunca rebaixa a classificação — a decisão é hard.
        Confidence só carrega significado decisório em Tier 3/4.
        """
        if not activity.date:
            conf, reasons = self._compute_confidence("unknown", None)
            return MatchResult(
                activity=activity, planned_session=None,
                extra_eval=None, classification="unknown",
                match_score=None, confidence=conf, confidence_reasons=reasons,
            )

        if not self.plan:
            ev = self._evaluate_extra(activity, planned_for_window=None)
            activity.classification = "extra_session"
            conf, reasons = self._compute_confidence(
                "extra_session", None, has_plan=False,
            )
            return MatchResult(
                activity=activity, planned_session=None,
                extra_eval=ev, classification="extra_session",
                match_score=None, confidence=conf, confidence_reasons=reasons,
            )

        # Candidatos disponíveis na semana ISO
        # Exclui: já reclamadas E sessões com status != "pending" (ex.: FALTOU/missed).
        # Sessões missed nunca podem ser re-claimed: foram perdidas intencionalmente
        # e não devem atrair atividades fáceis posteriores como "execução tardia".
        week_sessions = self._get_week_sessions(activity.date)
        available = [
            s for s in week_sessions
            if s.id not in self._claimed_sessions and s.status == "pending"
        ]

        if not available:
            # Semana esgotada ou sem prescrições → extra
            nearest = self._nearest_planned(activity.date)
            ev = self._evaluate_extra(activity, planned_for_window=nearest)
            activity.classification = "extra_session"
            nearest_days = self._days_to_nearest(activity.date, nearest)
            conf, reasons = self._compute_confidence(
                "extra_session", None,
                has_plan=True, nearest_planned_days=nearest_days,
            )
            return MatchResult(
                activity=activity, planned_session=None,
                extra_eval=ev, classification="extra_session",
                match_score=None, confidence=conf, confidence_reasons=reasons,
            )

        # ══════════════════════════════════════════════════════════
        # TIER 1 — HARD SAME-DAY LOCK
        # ══════════════════════════════════════════════════════════
        same_day_candidates = [s for s in available if s.date == activity.date]
        for ps in same_day_candidates:
            if self._is_compatible_hard(activity, ps):
                # Lock determinístico. Não passa por scoring/confidence ambíguo.
                tier1_result = self._commit_match(
                    activity, ps,
                    classification="planned_match",
                    plan_status="completed",
                    match_score=None,  # N/A — Tier 1 é hard, não score-based
                    tier=1,
                )
                logger.info(
                    f"TIER 1 lock: {activity.date} ({activity.duration_min:.0f}min) "
                    f"→ {ps.date} '{ps.type}' [hard same-day compatibility]"
                )
                return tier1_result

        # ══════════════════════════════════════════════════════════
        # TIER 2 — STRUCTURED SAME-DAY LOCK
        # ══════════════════════════════════════════════════════════
        # Para sessões estruturadas (SERIES/TEMPO/INTERVALADO/FARTLEK/
        # PERCURSO PLANO/MISTO), critério de tipo é mais permissivo —
        # qualquer atividade que cubra duração loose tolerance no mesmo
        # dia conta como execução estruturada. Isso protege contra o
        # bug "15Z1+35Z3+10Z1 vira aerobic genérico".
        for ps in same_day_candidates:
            if self._is_structured(ps) and self._is_compatible_structured_same_day(activity, ps):
                tier2_result = self._commit_match(
                    activity, ps,
                    classification="planned_match",
                    plan_status="completed",
                    match_score=None,
                    tier=2,
                )
                logger.info(
                    f"TIER 2 lock: {activity.date} ({activity.duration_min:.0f}min) "
                    f"→ {ps.date} '{ps.type}' [structured same-day]"
                )
                return tier2_result

        # ══════════════════════════════════════════════════════════
        # TIER 3 — DELAYED / EARLY (scoring + confidence ENTRAM AQUI)
        # ══════════════════════════════════════════════════════════
        # AQUI é o domínio do scoring/confidence — ambiguidade real.
        same_day_lock_used = False  # legado; preservado p/ compat com tests
        long_run_swap_used = False

        # ── Filtro de expiração (v4.3) ──────────────────────────────
        # Sessões mais velhas que a janela de tolerância de sua categoria
        # não podem ser "recuperadas" via Tier 3 cross-day. Impede que um
        # SERIES de 5 dias atrás roube um RODAGEM do dia atual.
        # Sessões futuras (planned_date > activity_date) nunca expiram
        # (são candidatas a completed_early) — já garantido em _is_session_expired.
        available = [s for s in available if not self._is_session_expired(activity.date, s)]
        if not available:
            nearest = self._nearest_planned(activity.date)
            ev = self._evaluate_extra(activity, planned_for_window=nearest)
            activity.classification = "extra_session"
            nearest_days = self._days_to_nearest(activity.date, nearest)
            conf, reasons = self._compute_confidence(
                "extra_session", None,
                has_plan=True, nearest_planned_days=nearest_days,
            )
            logger.debug(
                f"Tier 3 expiration: todos os candidatos expirados para {activity.date} "
                f"(cat={self._activity_category(activity)}) → extra_session"
            )
            return MatchResult(
                activity=activity, planned_session=None,
                extra_eval=ev, classification="extra_session",
                match_score=None, confidence=conf, confidence_reasons=reasons,
            )

        # ── Filtro de tipo para Tier 3 (v4.2) ──────────────────────
        # TYPE GUARD: corridas fáceis NUNCA podem "recuperar" sessões
        # de qualidade. Z1/Z2 ≠ Z4/Z5 por definição — a intensidade
        # executada é inCompatível com a intenção prescrita.
        #
        # Cobre os casos: easy run → SERIES / FARTLEK / INTERVALADO
        # Não bloqueia: quality activity → quality session (cross-day é válido,
        # classificado como adjusted_within_microcycle ou completed_delayed
        # conforme o delta e a janela de tolerância).
        activity_cat = self._activity_category(activity)
        if activity_cat in ("short_easy", "medium_easy", "long_easy"):
            tier3_easy = [
                s for s in available
                if self._session_category(s) != "quality"
            ]
            if not tier3_easy:
                # Todos os candidatos restantes são de qualidade — extra_session
                nearest = self._nearest_planned(activity.date)
                ev = self._evaluate_extra(activity, planned_for_window=nearest)
                activity.classification = "extra_session"
                nearest_days = self._days_to_nearest(activity.date, nearest)
                conf, reasons = self._compute_confidence(
                    "extra_session", None,
                    has_plan=True, nearest_planned_days=nearest_days,
                )
                logger.debug(
                    f"Tier 3 type guard: {len(available)} candidatos disponíveis, "
                    f"todos quality — atividade fácil em {activity.date} "
                    f"(cat={activity_cat}) → extra_session"
                )
                return MatchResult(
                    activity=activity, planned_session=None,
                    extra_eval=ev, classification="extra_session",
                    match_score=None, confidence=conf, confidence_reasons=reasons,
                )
            available = tier3_easy

        best_session, best_score = self._best_match(activity, available)

        if best_score < MIN_MATCH_SCORE:
            nearest = self._nearest_planned(activity.date)
            ev = self._evaluate_extra(activity, planned_for_window=nearest)
            activity.classification = "extra_session"
            nearest_days = self._days_to_nearest(activity.date, nearest)
            conf, reasons = self._compute_confidence(
                "extra_session", None,
                has_plan=True, nearest_planned_days=nearest_days,
            )
            return MatchResult(
                activity=activity, planned_session=None,
                extra_eval=ev, classification="extra_session",
                match_score=None, confidence=conf, confidence_reasons=reasons,
            )

        # 3. Proteção de longão: se o melhor match é um longão mas a
        #    cobertura é insuficiente E existe candidato mais curto, prefere
        #    o candidato mais curto (evita o bug 08/05).
        if self._is_long_run(best_session):
            act_dur = activity.duration_min or 0
            plan_dur = best_session.duration_min or 0
            if plan_dur > 0 and act_dur / plan_dur < LONG_RUN_MIN_COVERAGE_RATIO:
                # Candidatos que NÃO são longão
                short_candidates = [
                    s for s in available
                    if not self._is_long_run(s) and s.id != best_session.id
                ]
                if short_candidates:
                    alt_session, alt_score = self._best_match(activity, short_candidates)
                    if alt_score >= MIN_MATCH_SCORE:
                        logger.debug(
                            f"Longão protection: trocando {best_session.id} "
                            f"(score={best_score:.3f}) → {alt_session.id} "
                            f"(score={alt_score:.3f})"
                        )
                        best_session = alt_session
                        best_score = alt_score
                        long_run_swap_used = True

        # 4. Determinar classificação: delta + análise de microciclo
        try:
            act_date  = date.fromisoformat(activity.date)
            plan_date = date.fromisoformat(best_session.date)
            delta = (act_date - plan_date).days
        except ValueError:
            delta = 0

        # Verificar cobertura parcial de longão (sem alternativa curta)
        act_dur  = activity.duration_min or 0
        plan_dur = best_session.duration_min or 0
        is_partial = (
            self._is_long_run(best_session)
            and plan_dur > 0
            and act_dur / plan_dur < LONG_RUN_MIN_COVERAGE_RATIO
        )

        if is_partial:
            classification: ActivityClassification = "partially_completed"
            plan_status: PlannedStatus = "partially_completed"
        elif delta == 0:
            classification = "planned_match"
            plan_status = "completed"
        else:
            # Análise inteligente de microciclo: distingue ajuste normal de atraso real
            classification, plan_status = self._classify_delta(
                activity, best_session, delta, available
            )

        # 5. Commitar o match
        best_session.matched_activity_id = activity.id
        best_session.status = plan_status
        self._claimed_sessions[best_session.id] = activity.id
        activity.matched_session_id = best_session.id
        activity.classification = classification

        # 6. Próxima sessão pendente após este treino
        next_pending = self.next_pending_session(activity.date)

        # 7. Calcular confiança final (Prioridade 4) ────────────────
        confidence, conf_reasons = self._compute_confidence(
            classification,
            best_score,
            same_day_lock_used=same_day_lock_used,
            long_run_swap_used=long_run_swap_used,
            is_partial=is_partial,
            delta_days=delta,
            has_plan=True,
        )

        logger.info(
            f"Match: {activity.date} ({activity.duration_min:.0f}min) "
            f"→ {best_session.date} '{best_session.type}' "
            f"(score={best_score:.3f}, class={classification}, "
            f"confidence={confidence})"
        )

        return MatchResult(
            activity=activity,
            planned_session=best_session,
            extra_eval=None,
            classification=classification,
            next_pending_session=next_pending,
            match_score=best_score,
            confidence=confidence,
            confidence_reasons=conf_reasons,
            same_day_lock_used=same_day_lock_used,
            long_run_swap_used=long_run_swap_used,
        )

    # ── Score engine ──────────────────────────────────────────────

    def _best_match(
        self,
        activity: Activity,
        candidates: list[PlannedSession],
    ) -> tuple[PlannedSession, float]:
        """Retorna (melhor candidato, seu score)."""
        best: Optional[PlannedSession] = None
        best_score = -1.0
        for ps in candidates:
            s = self._match_score(activity, ps)
            if s > best_score:
                best_score = s
                best = ps
        # candidates nunca é vazio quando chamado
        return best, best_score  # type: ignore[return-value]

    def _match_score(self, activity: Activity, planned: PlannedSession) -> float:
        """Score 0–1 para o par (atividade × prescrição)."""
        w = SCORE_WEIGHTS
        s = (
            w["duration"] * self._duration_score(activity, planned)
            + w["type"]    * self._type_score(activity, planned)
            + w["zone"]    * self._zone_score(activity, planned)
            + w["date"]    * self._date_score(activity, planned)
        )
        logger.debug(
            f"  score({activity.date} vs {planned.date} '{planned.type}'): "
            f"dur={self._duration_score(activity,planned):.2f} "
            f"typ={self._type_score(activity,planned):.2f} "
            f"zon={self._zone_score(activity,planned):.2f} "
            f"dat={self._date_score(activity,planned):.2f} "
            f"→ {s:.3f}"
        )
        return s

    def _duration_score(self, activity: Activity, planned: PlannedSession) -> float:
        """
        Proximidade de duração entre executado e prescrito.
        ratio = min/max → 1 quando idênticos, cai linearmente.
        Score = 0 quando ratio ≤ 0.30, 1 quando ratio = 1.
        """
        a_dur = activity.duration_min
        p_dur = float(planned.duration_min) if planned.duration_min else None

        if a_dur is None or p_dur is None:
            return 0.5  # neutro por falta de dados

        ratio = min(a_dur, p_dur) / max(a_dur, p_dur)
        # Interpolação linear: score 0 em ratio=0.30, score 1 em ratio=1.0
        return max(0.0, (ratio - 0.30) / 0.70)

    def _type_score(self, activity: Activity, planned: PlannedSession) -> float:
        """
        Compatibilidade de categoria:
          short_easy  ↔ short_easy  : 1.0
          medium_easy ↔ medium_easy : 1.0
          long_easy   ↔ long_easy   : 1.0
          quality     ↔ quality     : 1.0
          short/medium ↔ long       : 0.30  (duração incompatível)
          easy        ↔ quality     : 0.20  (intensidade incompatível)
          short       ↔ quality     : 0.15
        """
        a_cat = self._activity_category(activity)
        p_cat = self._session_category(planned)

        if a_cat == p_cat:
            return 1.0

        # Longão vs qualquer coisa curta/média — mismatch grave de duração
        if "long" in (a_cat, p_cat):
            return 0.30

        # Quality vs easy — mismatch de intensidade
        if "quality" in (a_cat, p_cat):
            return 0.20

        # short vs medium: divergência tolerável
        return 0.70

    def _zone_score(self, activity: Activity, planned: PlannedSession) -> float:
        """
        Compatibilidade de intensidade entre o pace executado e as zonas
        prescritas. Se não houver dados suficientes, retorna 0.5 (neutro).
        """
        if not planned.target_zone:
            return 0.5

        zones = self.plan.get("zones_mpr") or {}
        is_quality = self._session_category(planned) == "quality"

        # Numa sessão de QUALIDADE a intenção é a zona de TRABALHO (Z3-Z6).
        # Testamos o lap mais rápido (min_lap_pace) contra ela — um treino easy
        # cujo avg_pace casa só com a zona de recuperação (Z1/Z2) NÃO deve
        # pontuar alto (era o defeito: easy ganhava zona=1.0 vs treino de
        # qualidade só por bater na recuperação). Sessão easy: comportamento
        # inalterado (avg_pace contra as zonas prescritas).
        if is_quality:
            target = [z for z in planned.target_zone
                      if z.upper() in ("Z3", "Z4", "Z5", "Z6")] or list(planned.target_zone)
            test_pace = activity.min_lap_pace or activity.avg_pace
        else:
            target = list(planned.target_zone)
            test_pace = activity.avg_pace

        pace_min = _pace_to_minutes(test_pace)
        if pace_min is None:
            return 0.5

        # Verifica se o pace de teste cai dentro de alguma zona-alvo
        for z in target:
            zd = zones.get(z) or {}
            lo = _pace_to_minutes(zd.get("pace_min"))  # pace mais rápido (menor número)
            hi = _pace_to_minutes(zd.get("pace_max"))  # pace mais lento (maior número)
            if lo is None or hi is None:
                continue
            if lo <= pace_min <= hi:
                return 1.0                        # dentro da zona
            if abs(pace_min - lo) <= 0.5 or abs(pace_min - hi) <= 0.5:
                return 0.75                       # fora mas próximo (±30s/km)

        # Fora de todas as zonas-alvo — ainda pode ser o melhor match
        # se nenhum outro candidato existir; não zerar completamente.
        return 0.35

    def _date_score(self, activity: Activity, planned: PlannedSession) -> float:
        """
        Proximidade de data. Penaliza antecipações mais que atrasos
        (prefere completed_delayed sobre completed_early quando scores similares).

        delta = activity_date - planned_date (dias)
        delta == 0 → 1.0
        delta > 0  → atraso → 1.0 - 0.18 * delta  (cai mais devagar)
        delta < 0  → antecipação → 1.0 - 0.22 * abs(delta)  (cai mais rápido)
        """
        try:
            act_d  = date.fromisoformat(activity.date)
            plan_d = date.fromisoformat(planned.date)
            delta  = (act_d - plan_d).days
        except ValueError:
            return 0.5

        if delta == 0:
            return 1.0
        if delta > 0:
            return max(0.0, 1.0 - 0.18 * delta)
        else:
            return max(0.0, 1.0 - 0.22 * abs(delta))

    # ── Categorias de sessão / atividade ──────────────────────────

    def _session_category(self, planned: PlannedSession) -> _SessionCategory:
        """Classifica a prescrição em short_easy / medium_easy / long_easy / quality."""
        zones = [z.upper() for z in planned.target_zone]
        has_quality = any(z in ("Z3", "Z4", "Z5", "Z6") for z in zones)
        if has_quality:
            return "quality"
        dur = planned.duration_min or 0
        if dur >= LONG_RUN_MIN_DURATION_MIN:
            return "long_easy"
        if dur >= 55:
            return "medium_easy"
        return "short_easy"

    def _activity_category(self, activity: Activity) -> _SessionCategory:
        """
        Classifica a atividade executada em short_easy / medium_easy / long_easy / quality.

        v4.1 — usa min_lap_pace (pace do bloco mais rápido) antes do avg_pace.
        Isso permite detectar sessões SERIES (Z1+Z3+Z1) como "quality" mesmo
        quando o avg_pace geral fica diluído pelo warmup/cooldown.

        Hierarquia de detecção de qualidade:
          1. min_lap_pace < 5.5 min/km  (bloco mais rápido — alta precisão)
          2. avg_pace < 5.5 min/km      (pace médio geral — pode ser diluído)
          3. avg_hr > 168 bpm           (FC elevada)
        """
        # Pace do bloco mais rápido (lap) — fonte primária para sessões estruturadas
        min_pace_min = _pace_to_minutes(activity.min_lap_pace)
        avg_pace_min = _pace_to_minutes(activity.avg_pace)
        hr = activity.avg_hr or 0
        dur = activity.duration_min or 0

        # Qualidade: lap rápido OU avg_pace rápido OU FC elevada
        quality_threshold = 5.5  # min/km
        if (min_pace_min is not None and min_pace_min < quality_threshold):
            logger.debug(
                f"_activity_category: quality via min_lap_pace "
                f"({activity.min_lap_pace} < {quality_threshold} min/km)"
            )
            return "quality"
        if (avg_pace_min is not None and avg_pace_min < quality_threshold) or hr > 168:
            return "quality"

        if dur >= LONG_RUN_MIN_DURATION_MIN:
            return "long_easy"
        if dur >= 55:
            return "medium_easy"
        return "short_easy"

    def _is_long_run(self, planned: PlannedSession) -> bool:
        """True se a prescrição é um longão (duração >= LONG_RUN_MIN_DURATION_MIN)."""
        return bool(planned.duration_min and planned.duration_min >= LONG_RUN_MIN_DURATION_MIN)

    def _is_session_expired(self, activity_date: str, planned: PlannedSession) -> bool:
        """
        Retorna True se a sessão prescrita está velha demais para ser matched
        cross-day (Tier 3).

        Uma sessão expira quando:
            activity_date - planned_date > MICROCYCLE_TOLERANCE_DAYS[categoria]

        Garante que sessões de qualidade não fiquem abertas para sempre após
        serem perdidas — impedindo que um FARTLEK/SERIES de 5 dias atrás
        roube um RODAGEM do mesmo dia.

        Sessões futuras (planned_date > activity_date) nunca expiram —
        são candidatas válidas para completed_early.
        """
        try:
            act_d  = date.fromisoformat(activity_date)
            plan_d = date.fromisoformat(planned.date)
            delta  = (act_d - plan_d).days
        except ValueError:
            return False
        if delta <= 0:
            return False  # Mesmo dia ou futuro: nunca expirado
        p_cat     = self._session_category(planned)
        tolerance = MICROCYCLE_TOLERANCE_DAYS.get(p_cat, 1)
        expired   = delta > tolerance
        if expired:
            logger.debug(
                f"_is_session_expired: {planned.date} '{planned.type}' "
                f"(cat={p_cat}, tol={tolerance}) expirado para atividade em {activity_date} "
                f"(delta={delta})"
            )
        return expired

    # ── Classificação inteligente de microciclo (v4.0) ────────────

    def _classify_delta(
        self,
        activity: Activity,
        planned: PlannedSession,
        delta: int,
        week_available: list[PlannedSession],
    ) -> tuple[ActivityClassification, PlannedStatus]:
        """
        Decide se um deslocamento de `delta` dias é:
          - adjusted_within_microcycle  (ajuste normal de agenda, sem impacto)
          - completed_delayed           (atraso real com possível impacto fisiológico)
          - completed_early             (antecipação dentro da semana)

        Critérios para ADJUSTED_WITHIN_MICROCYCLE:
          1. Mesma semana ISO (seg–dom) entre prescrição e execução
          2. |delta| <= janela de tolerância do tipo de sessão
          3. Sem stacking inadequado de estímulos duros detectado

        Antecipações (delta < 0) dentro da semana com delta no limite:
          Tratadas com a mesma lógica mas retornam completed_early quando
          não configuram stacking.
        """
        try:
            act_date  = date.fromisoformat(activity.date)
            plan_date = date.fromisoformat(planned.date)
        except ValueError:
            # Datas inválidas: fallback conservador
            if delta > 0:
                return "completed_delayed", "completed"
            return "completed_early", "completed"

        abs_delta = abs(delta)
        session_cat = self._session_category(planned)
        tolerance   = MICROCYCLE_TOLERANCE_DAYS.get(session_cat, 1)

        # ── Verificar mesma semana ISO ──────────────────────────
        act_week_start  = act_date  - timedelta(days=act_date.weekday())
        plan_week_start = plan_date - timedelta(days=plan_date.weekday())
        same_week = (act_week_start == plan_week_start)

        # ── Verificar stacking inadequado ───────────────────────
        stacking = self._detect_stacking(activity, planned, act_date)

        # ── Tomada de decisão ───────────────────────────────────
        if not same_week:
            # Saiu da semana → atraso real sempre
            if delta > 0:
                logger.debug(
                    f"_classify_delta: {activity.date} vs {planned.date} "
                    f"→ semanas diferentes → completed_delayed"
                )
                return "completed_delayed", "completed"
            else:
                # Antecipação para semana anterior: raro, mas possível
                return "completed_early", "completed"

        if abs_delta <= tolerance and not stacking:
            # Deslocamento dentro da janela e sem stacking → ajuste de agenda
            classification: ActivityClassification = "adjusted_within_microcycle"
            logger.debug(
                f"_classify_delta: {activity.date} vs {planned.date} "
                f"delta={delta} cat={session_cat} tol={tolerance} "
                f"→ adjusted_within_microcycle"
            )
            return "adjusted_within_microcycle", "adjusted_within_microcycle"

        if abs_delta > tolerance or stacking:
            if delta > 0:
                reason = "stacking" if stacking else f"delta={delta}>{tolerance}"
                logger.debug(
                    f"_classify_delta: {activity.date} vs {planned.date} "
                    f"→ completed_delayed ({reason})"
                )
                return "completed_delayed", "completed"
            else:
                return "completed_early", "completed"

        # Fallback (não deve chegar aqui)
        if delta > 0:
            return "completed_delayed", "completed"
        return "completed_early", "completed"

    def _detect_stacking(
        self,
        activity: Activity,
        planned: PlannedSession,
        act_date: date,
    ) -> bool:
        """
        Detecta acúmulo indevido de estímulos duros (stacking).

        Regras:
        - Sessão de qualidade (intervalado/limiar) executada no dia
          adjacente a outra sessão de qualidade já registrada → stacking.
        - Longão executado no dia adjacente a qualidade ou outro longão → stacking.
        - Regenerativos e Z2 curtos: sem verificação de stacking.

        Retorna True se stacking detectado.
        """
        session_cat = self._session_category(planned)

        # Categorias que não geram stacking entre si
        if session_cat in ("short_easy",):
            return False

        # Dias vizinhos (anterior e posterior) para verificar
        for day_offset in (-1, 1):
            neighbor_date = (act_date + timedelta(days=day_offset)).isoformat()
            neighbor_acts = self._activities_by_date.get(neighbor_date, [])

            for neighbor in neighbor_acts:
                neighbor_cat = self._activity_category(neighbor)

                # Qualidade + qualidade adjacente → stacking
                if session_cat == "quality" and neighbor_cat == "quality":
                    logger.debug(
                        f"Stacking detectado: quality em {act_date} adjacente a "
                        f"quality em {neighbor_date}"
                    )
                    return True

                # Longão adjacente a qualidade ou outro longão → stacking
                if session_cat == "long_easy" and neighbor_cat in ("quality", "long_easy"):
                    logger.debug(
                        f"Stacking detectado: long_easy em {act_date} adjacente a "
                        f"{neighbor_cat} em {neighbor_date}"
                    )
                    return True

        return False

    # ── Helpers de semana e proximidade ──────────────────────────

    def _get_week_sessions(self, iso_date: str) -> list[PlannedSession]:
        """
        Retorna todas as PlannedSessions na semana ISO (seg–dom)
        que contém iso_date, ordenadas por data.
        """
        try:
            target = date.fromisoformat(iso_date)
        except ValueError:
            return []

        week_start = target - timedelta(days=target.weekday())  # segunda
        week_end   = week_start + timedelta(days=6)             # domingo

        result = []
        for ps in self._planned_by_date.values():
            try:
                ps_date = date.fromisoformat(ps.date)
            except ValueError:
                continue
            if week_start <= ps_date <= week_end:
                result.append(ps)
        return sorted(result, key=lambda s: s.date)

    def _nearest_planned(self, iso_date: str) -> Optional[PlannedSession]:
        """Retorna a PlannedSession mais próxima cronologicamente."""
        try:
            target = date.fromisoformat(iso_date)
        except ValueError:
            return None
        best: Optional[PlannedSession] = None
        best_delta = 10 ** 9
        for ps in self._planned_by_date.values():
            try:
                delta = abs((target - date.fromisoformat(ps.date)).days)
            except ValueError:
                continue
            if delta < best_delta:
                best_delta = delta
                best = ps
        return best

    @staticmethod
    def _days_to_nearest(
        iso_date: str,
        nearest: Optional[PlannedSession],
    ) -> Optional[int]:
        """Distância em dias (absoluta) entre `iso_date` e a sessão prescrita
        mais próxima. Usado pelo cálculo de confiança em extras."""
        if nearest is None:
            return None
        try:
            target = date.fromisoformat(iso_date)
            ps_date = date.fromisoformat(nearest.date)
            return abs((target - ps_date).days)
        except ValueError:
            return None

    # ── Próxima sessão pendente ────────────────────────────────────

    def next_pending_session(self, after_date: str) -> Optional[PlannedSession]:
        """
        Retorna a próxima PlannedSession não reclamada com data >= after_date.
        Usado pelo payload_builder para informar o Claude sobre o próximo treino
        da MPR sem que ele invente sugestões genéricas.
        """
        try:
            target = date.fromisoformat(after_date)
        except ValueError:
            return None

        candidates: list[PlannedSession] = []
        for ps in self._planned_by_date.values():
            if ps.id in self._claimed_sessions:
                continue
            try:
                ps_date = date.fromisoformat(ps.date)
            except ValueError:
                continue
            if ps_date >= target:
                candidates.append(ps)

        if not candidates:
            return None
        return min(candidates, key=lambda s: s.date)

    # ── Aderência semanal ──────────────────────────────────────────

    def weekly_adherence_status(self, iso_date: str) -> WeeklyAdherenceStatus:
        """
        Calcula o status de aderência para a semana ISO que contém iso_date.

        Regra: não marca "missed" antes do fim da semana.
        Retorna: on_track | at_risk | behind_schedule | ahead_of_schedule | unknown
        """
        week_sessions = self._get_week_sessions(iso_date)
        if not week_sessions:
            return "unknown"

        try:
            today = date.fromisoformat(iso_date)
        except ValueError:
            return "unknown"

        week_end = today - timedelta(days=today.weekday()) + timedelta(days=6)
        week_over = today >= week_end

        completed = 0
        pending_future = 0
        missed_count = 0

        for ps in week_sessions:
            try:
                ps_date = date.fromisoformat(ps.date)
            except ValueError:
                continue

            if ps.id in self._claimed_sessions:
                completed += 1
            elif ps_date > today:
                pending_future += 1
            else:
                # Data passada e não cumprido
                if week_over:
                    missed_count += 1
                else:
                    # Ainda dentro da semana: trata como "at risk"
                    pending_future += 1

        total = len(week_sessions)
        if total == 0:
            return "unknown"

        # Todos já completados
        if completed == total:
            return "on_track"

        # Ainda há sessões futuras na semana — não punir
        if pending_future > 0 and missed_count == 0:
            if completed >= total * 0.5:
                return "on_track"
            return "at_risk"

        # Semana fechada ou sessões passadas em aberto
        adherence = completed / total
        if adherence >= 0.85:
            return "on_track"
        if adherence >= 0.60:
            return "at_risk"
        return "behind_schedule"

    # ── Treino extra ──────────────────────────────────────────────

    def _evaluate_extra(
        self,
        activity: Activity,
        *,
        planned_for_window: Optional[PlannedSession],
    ) -> ExtraSessionEvaluation:
        """
        Classifica treino extra como beneficial / neutral / harmful.
        Default: neutral quando dados insuficientes.
        """
        pace_min = _pace_to_minutes(activity.avg_pace)
        avg_hr   = activity.avg_hr or 0

        is_easy = (pace_min is not None and pace_min >= 6.5) or (avg_hr and avg_hr <= 145)
        is_hard = (pace_min is not None and pace_min < 5.3) or avg_hr >= 175

        risky_neighbor = False
        if planned_for_window:
            t = planned_for_window.type.upper()
            if any(k in t for k in ("INTERVALADO", "LIMIAR", "PROGRESSIVO", "LONGO", "MISTO")):
                if is_hard:
                    risky_neighbor = True

        if is_hard or risky_neighbor:
            return ExtraSessionEvaluation(
                activity_id=activity.id,
                date=activity.date,
                classification="harmful",
                reason=(
                    "Treino extra em intensidade alta (pace agressivo ou FC ≥175 bpm)"
                    + (" próximo a uma sessão de qualidade prescrita." if risky_neighbor
                       else " sem prescrição correspondente.")
                ),
                impact_on_next_sessions=(
                    "Risco de fadiga cumulativa: considere reduzir o volume da próxima "
                    "sessão prescrita ou trocar por regenerativo."
                ),
            )

        if is_easy:
            return ExtraSessionEvaluation(
                activity_id=activity.id,
                date=activity.date,
                classification="beneficial",
                reason=(
                    "Sessão extra em zona aeróbica leve — coerente com 80/20 e útil "
                    "para volume de base sem agredir a prescrição."
                ),
                impact_on_next_sessions=(
                    "Pouco impacto fisiológico negativo. Boa janela para drills "
                    "Pose Method e cadência sem comprometer a próxima sessão MPR."
                ),
            )

        return ExtraSessionEvaluation(
            activity_id=activity.id,
            date=activity.date,
            classification="neutral",
            reason="Sessão extra em zona moderada/indeterminada — efeito neutro.",
            impact_on_next_sessions=(
                "Acompanhar deriva cardíaca e RPE no treino seguinte. "
                "Se RPE estiver alto, considerar redução de volume ou intensidade."
            ),
        )
