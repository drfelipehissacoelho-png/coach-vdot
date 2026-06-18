"""
history_manager.py
Salva cada análise do Claude em dois formatos:
  1. analyses/YYYY-MM-DD_nome.md  → arquivo markdown individual (legível)
  2. web/data/analyses.json       → JSON agregado para o dashboard web

O JSON é lido pelo dashboard online publicado no Vercel.
"""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from src.heat_utils import calculate_heat_adjustment
from src.schemas import metrics_as_dict, validate_metrics

logger = logging.getLogger("garmin_coach.history")

BASE_DIR      = Path(__file__).parent.parent.parent   # raiz do Running Trainer
ANALYSES_DIR  = BASE_DIR / "garmin_coach" / "analyses"
WEB_DATA_FILE = Path(os.getenv(
    "WEB_DATA_PATH",
    str(BASE_DIR / "web" / "data" / "analyses.json")
))


# ── Salvar análise individual em Markdown ──────────────────────

def save_analysis_markdown(training_data: dict, analysis_text: str) -> Path:
    """Salva análise completa como arquivo .md individual."""
    ANALYSES_DIR.mkdir(parents=True, exist_ok=True)

    date_str = (training_data.get("start_time_local") or "")[:10].replace("/", "-")
    if not date_str or len(date_str) < 8:
        date_str = datetime.now().strftime("%Y-%m-%d")
    else:
        # Converte DD-MM-YYYY para YYYY-MM-DD se necessário
        parts = date_str.split("-")
        if len(parts[0]) == 2:
            date_str = f"{parts[2]}-{parts[1]}-{parts[0]}"

    nome = training_data.get("activity_name") or training_data.get("filename", "treino")
    nome_limpo = "".join(c if c.isalnum() or c in " -_" else "_" for c in nome)[:40].strip()
    filename = f"{date_str}_{nome_limpo}.md"
    filepath = ANALYSES_DIR / filename

    # Extrai bloco METRICS para exibir formatado no MD (auditoria)
    metrics = _parse_metrics_block(analysis_text)
    analysis_clean = _strip_metrics_block(analysis_text)
    metrics_md = ""
    if metrics:
        metrics_json = json.dumps(metrics, indent=2, ensure_ascii=False)
        metrics_md = f"\n## Métricas Estruturadas (JSON)\n\n```json\n{metrics_json}\n```\n"

    # Bloco de clima: só aparece se temos dados
    clima_rows = ""
    if training_data.get("humidity_pct") is not None:
        clima_rows += f"| Umidade | {training_data.get('humidity_pct')}% |\n"
    if training_data.get("dew_point_c") is not None:
        clima_rows += f"| Ponto de Orvalho | {training_data.get('dew_point_c')}°C |\n"
    if training_data.get("heat_index_c") is not None:
        clima_rows += f"| Sensação Térmica | {training_data.get('heat_index_c')}°C |\n"

    content = f"""# {nome} — {training_data.get('start_time_local', date_str)}

## Dados do Treino
| Métrica | Valor |
|---------|-------|
| Distância | {training_data.get('total_distance_km', 'N/D')} km |
| Duração | {training_data.get('duration_formatted', 'N/D')} |
| Pace Médio | {training_data.get('avg_pace', 'N/D')} |
| FC Média | {training_data.get('avg_heart_rate', 'N/D')} bpm |
| FC Máxima | {training_data.get('max_heart_rate', 'N/D')} bpm |
| Cadência | {training_data.get('avg_cadence_spm', 'N/D')} spm |
| Temperatura | {training_data.get('avg_temperature_c', 'N/D')}°C |
{clima_rows}| Fonte | {training_data.get('source', 'garmin').title()} |

## Análise do Coach VDOT

{analysis_clean}
{metrics_md}
---
*Gerado automaticamente em {datetime.now().strftime('%d/%m/%Y %H:%M')}*
"""
    _atomic_write_text(filepath, content)
    logger.info(f"Análise salva: {filename}")
    return filepath


# ── Atualizar JSON agregado para o dashboard web ───────────────

def update_web_json(training_data: dict, analysis_text: str):
    """
    Adiciona a análise ao JSON do dashboard web.
    Mantém as últimas 100 análises para não deixar o arquivo muito grande.
    """
    WEB_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Carrega histórico existente
    existing = _load_web_json()

    # Extrai nota de aderência do texto da análise
    score = _extract_score(analysis_text)

    # Extrai bloco METRICS (JSON estruturado que o Claude anexa no final)
    # — agora com validação Pydantic (Prioridade 2): se o LLM emitir um
    # campo extra, um enum fora do vocabulário ou um número fora de range,
    # registramos no entry e seguimos com o dict cru (não regride dashboard).
    sport = training_data.get("sport", "run")
    metrics, metrics_status, metrics_errors = _parse_metrics_block_validated(
        analysis_text, sport=sport,
    )
    analysis_clean = _strip_metrics_block(analysis_text)

    # ── BUG 7: sanitiza '<' solto na narrativa do LLM ────────────
    # Ex.: "FC subiu em <8 min" — o '<' nao escapado e lido como inicio
    # de tag e, ao injetar via innerHTML, funde todos os cards num no so
    # (lista some, dado intacto). Escapa '<' que NAO inicia tag permitida.
    analysis_clean = re.sub(
        r'<(?!/?(?:strong|em|b|i|br|p|ul|ol|li|span|div|a|h[1-6])\b)',
        '&lt;', analysis_clean, flags=re.IGNORECASE,
    )

    # ── Override determinístico: ajuste de calor ─────────────────
    # Garante que ajuste_calor_segundos_km e pace_equivalente_frio_min_km
    # sejam sempre calculados em Python (não dependem do LLM).
    if metrics is not None and training_data.get("sport", "run").lower() != "swim":
        _adj_sec, _pace_frio = calculate_heat_adjustment(
            temp_c      = training_data.get("avg_temperature_c"),
            dew_point_c = training_data.get("dew_point_c"),
            avg_pace    = training_data.get("avg_pace"),
            is_treadmill= training_data.get("is_treadmill", False),
        )
        metrics["ajuste_calor_segundos_km"]    = _adj_sec
        metrics["pace_equivalente_frio_min_km"] = _pace_frio

    # Monta entrada nova
    entry = {
        "id":            _make_id(training_data),
        # Identificadores estáveis do provider externo (Prioridade 5).
        # Permite dedup cruzado mesmo se o ID legado mudar de formato.
        "provider":             training_data.get("provider") or training_data.get("source", "garmin"),
        "provider_activity_id": training_data.get("provider_activity_id"),
        "date":          _normalize_date(training_data.get("start_time_local", "")),
        "date_display":  training_data.get("start_time_local", "")[:10],
        "source":        training_data.get("source", "garmin"),
        "sport":         training_data.get("sport", "run"),
        "is_treadmill":  training_data.get("is_treadmill", False),
        "activity_name": training_data.get("activity_name") or training_data.get("filename", "Treino"),
        "distance_km":   training_data.get("total_distance_km"),
        "duration":      training_data.get("duration_formatted"),
        "avg_pace":      training_data.get("avg_pace"),
        "avg_heart_rate": training_data.get("avg_heart_rate"),
        "max_heart_rate": training_data.get("max_heart_rate"),
        "avg_cadence_spm": training_data.get("avg_cadence_spm"),
        "temperature_c": training_data.get("avg_temperature_c"),
        "humidity_pct":  training_data.get("humidity_pct"),
        "dew_point_c":   training_data.get("dew_point_c"),
        "heat_index_c":  training_data.get("heat_index_c"),
        "weather_source": training_data.get("weather_source"),
        "total_ascent_m": training_data.get("total_ascent_m"),
        "total_descent_m": training_data.get("total_descent_m"),
        "training_stress_score": training_data.get("training_stress_score"),
        "aerobic_training_effect": training_data.get("aerobic_training_effect"),
        "anaerobic_training_effect": training_data.get("anaerobic_training_effect"),
        "laps":          training_data.get("laps"),
        "metrics":       metrics,           # JSON estruturado vindo do Claude
        # Auditoria do bloco METRICS (Prioridade 2):
        #   'valid'   → passou no schema Pydantic
        #   'invalid' → bateu, mas o LLM emitiu algo fora do contrato
        #   'missing' → não havia bloco METRICS
        "metrics_validation": metrics_status,
        # Lista de mensagens de erro (vazia se valid/missing). Mantida na
        # análise para inspeção rápida pelo dashboard / pelo log de auditoria.
        "metrics_errors": metrics_errors,
        "score":         score,
        "analysis_html": analysis_clean,    # versão limpa (sem bloco METRICS) para o dashboard

        # ── BUG 1 / matching MPR (session_matcher) ───────────────
        "classification":           training_data.get("mpr_classification"),
        "matched_session_id":       training_data.get("mpr_matched_session_id"),
        "extra_session_evaluation": training_data.get("extra_session_evaluation"),

        # ── Prioridade 4: Confiança do matcher ───────────────────
        # alta/media/baixa + score 0-1 + razões textuais. Renderizado
        # como badge colorido no dashboard e injetado no prompt do Claude.
        "match_confidence":         training_data.get("mpr_match_confidence"),
        "match_confidence_reasons": training_data.get("mpr_match_confidence_reasons"),
        "match_score":              training_data.get("mpr_match_score"),

        # ── Melhoria 4: arquitetura para FIT/TCX running dynamics ──
        # Campos preenchidos pelo fit_reader/strava_reader quando o
        # arquivo trouxer Running Dynamics. Hoje normalmente None.
        "running_dynamics": {
            "ground_contact_time_ms":  training_data.get("avg_ground_contact_time_ms"),
            "vertical_oscillation_cm": training_data.get("avg_vertical_oscillation_cm"),
            "vertical_ratio_pct":      training_data.get("avg_vertical_ratio_pct"),
            "stride_length_m":         training_data.get("avg_stride_length_m"),
            "gct_balance_pct":         training_data.get("avg_gct_balance_pct"),
        },

        # ── Melhoria 3: cardiac drift (Strava streams) ───────────
        # Preenchido pelo drift_analyzer quando há streams disponíveis.
        "drift_analysis": training_data.get("drift_analysis"),

        # ── Stryd: potência bruta e métricas derivadas ────────────
        # avg_watts / weighted_avg_watts vêm do strava_reader (já parseados
        # mas não persistidos até agora). stryd_metrics é calculado pelo
        # stryd_analyzer (running_effectiveness, power_variability_pct, etc.).
        # Todos ficam None para atividades sem sensor de potência.
        "avg_watts":          training_data.get("avg_watts"),
        "weighted_avg_watts": training_data.get("weighted_avg_watts"),
        "stryd_metrics":      training_data.get("stryd_metrics"),

        "generated_at":  datetime.now().isoformat(),
    }

    # Evita duplicata: por ID OU por (provider, provider_activity_id).
    # O check duplo é essencial durante a transição entre os esquemas de
    # ID antigo ('2026-05-13_morning_run') e novo ('2026-05-13_morning_run_18487...'):
    # uma reanálise da mesma atividade Strava deve SUBSTITUIR o entry antigo,
    # não criar duplicata.
    new_pid = entry.get("provider_activity_id")
    new_provider = entry.get("provider")

    def _is_same_activity(a: dict) -> bool:
        if a.get("id") == entry["id"]:
            return True
        # Match cruzado por provider_activity_id quando ambos tiverem
        a_pid = a.get("provider_activity_id")
        if new_pid and a_pid and a_pid == new_pid and a.get("provider") == new_provider:
            return True
        return False

    existing["analyses"] = [a for a in existing["analyses"] if not _is_same_activity(a)]

    # Insere e re-ordena por data DESC (desempate: generated_at DESC).
    # Garante que reprocessamentos não baguncem a ordem do dashboard.
    existing["analyses"].insert(0, entry)
    existing["analyses"].sort(
        key=lambda a: (a.get("date") or "", a.get("generated_at") or ""),
        reverse=True,
    )

    # Mantém últimas 100
    existing["analyses"] = existing["analyses"][:100]

    # Atualiza metadata
    existing["last_updated"] = datetime.now().isoformat()
    existing["total_analyses"] = len(existing["analyses"])

    # Salva (gravação atômica — evita corrupção se o processo for interrompido
    # ou se houver concorrência com watcher/publisher)
    _atomic_write_json(WEB_DATA_FILE, existing)
    logger.info(f"Dashboard JSON atualizado: {len(existing['analyses'])} análises")


def _atomic_write_text(path: Path, content: str) -> None:
    """
    Grava texto de forma atômica: escreve em arquivo .tmp, dá flush+fsync,
    e só então faz os.replace para o destino final.

    Crítico em pastas sincronizadas (OneDrive/Dropbox/iCloud), onde a sync
    do cliente pode pegar o arquivo no meio da gravação e produzir um arquivo
    truncado — bug observado em 25/04/2026 com 9 dos 13 .md cortados no meio
    do bloco JSON. os.replace é atômico no mesmo volume, então o sync só
    enxerga o arquivo final inteiro ou o arquivo antigo intacto.
    """
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, data: dict) -> None:
    """
    Wrapper sobre _atomic_write_text para gravar JSON formatado.

    Defesa pós-incidente 14/05/2026: VALIDA o arquivo recém-escrito
    parseando-o de volta. Se a leitura falhar (escrita corrompida,
    encoding errado, espaço em disco), aborta e o destino original
    fica intacto graças à escrita atômica via _atomic_write_text +
    os.replace.

    Sem essa validação, uma gravação parcial poderia subir um arquivo
    corrompido para produção, e o silent-fail de _load_existing_activities
    transformaria isso em "histórico vazio" → matching errado.
    """
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    _atomic_write_text(path, payload)

    # Validação pós-escrita: a leitura do destino deve parsear sem erro.
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        # Em caso de corrupção pós-escrita (raro, mas possível com
        # sincronização OneDrive/Dropbox), restauramos do backup mais recente.
        backup_dir = path.parent
        backups = sorted(backup_dir.glob(f"{path.stem}.backup_*.json"))
        if backups:
            shutil = __import__("shutil")
            shutil.copy2(backups[-1], path)
            raise RuntimeError(
                f"Pós-escrita: {path.name} ficou corrompido (char {e.pos}). "
                f"Restaurado do backup {backups[-1].name}. "
                "Pipeline ABORTADO para evitar cascade."
            ) from e
        raise RuntimeError(
            f"Pós-escrita: {path.name} ficou corrompido e não há backup. "
            "Pipeline ABORTADO."
        ) from e


# ── Helpers ────────────────────────────────────────────────────

def _load_web_json() -> dict:
    if WEB_DATA_FILE.exists():
        try:
            return json.loads(WEB_DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "athlete":        "Felipe Hissa Coelho",
        "vdot":           39,
        # Data oficial da Maratona de Osaka — atualizada para 28/02/2027.
        "goal_race":      "Osaka Marathon — 28/02/2027",
        "goal_race_date": "2027-02-28",
        "goal_time":      "Sub-3h50",
        "last_updated":   "",
        "total_analyses": 0,
        "analyses":       [],
    }


def _extract_score(analysis_text: str) -> int | None:
    """Extrai a nota X/10 do texto da análise."""
    match = re.search(r"Nota[:\s]+(\d{1,2})/10", analysis_text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


# Regex unificada para identificar o bloco <!-- METRICS { ... } -->
# Usa DOTALL para que '.' capture quebras de linha dentro do JSON.
_METRICS_BLOCK_RE = re.compile(
    r"<!--\s*METRICS\s*(\{.*?\})\s*-->",
    re.DOTALL | re.IGNORECASE,
)


def _parse_metrics_block(analysis_text: str) -> dict | None:
    """
    Extrai o JSON estruturado que o Claude anexa em <!-- METRICS { ... } -->.

    Retorna o dict parseado ou None se:
      - o bloco não existir
      - o JSON for inválido
      - o bloco estiver vazio

    Nunca lança exceção — falhas são silenciosas (com log) para não quebrar o pipeline.
    """
    if not analysis_text:
        return None
    match = _METRICS_BLOCK_RE.search(analysis_text)
    if not match:
        logger.debug("Bloco METRICS não encontrado na análise")
        return None
    raw = match.group(1).strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        logger.warning("Bloco METRICS não é um objeto JSON — ignorando")
        return None
    except json.JSONDecodeError as e:
        logger.warning(f"Bloco METRICS com JSON inválido: {e}")
        return None


def _parse_metrics_block_validated(
    analysis_text: str,
    sport: str = "run",
) -> tuple[dict | None, str, list[str]]:
    """
    Versão Pydantic-validada de _parse_metrics_block (Prioridade 2 da auditoria).

    Faz dois passos:
      1. Extrai o bloco bruto (mesmo regex de _parse_metrics_block).
      2. Valida contra RunMetrics/SwimMetrics (extra='forbid', enums fechados,
         ranges sanos).

    Retorna uma tripla (metrics_dict, status, errors):
      - metrics_dict: dict pronto para serializar no analyses.json. Quando a
        validação passa, é o dump do modelo (enums viram strings, ordem
        canonicalizada). Quando falha, é o dict cru — o dashboard não
        regride, mas o ENTRY ganha bandeira para auditoria.
      - status: 'valid' | 'invalid' | 'missing'
      - errors: lista de erros de validação (vazia se válido ou ausente)

    Nunca lança exceção: falha de validação é REGISTRADA, não fatal. Este é
    o ponto-chave do contrato — Pydantic existe para detectar drift do LLM,
    não para bloquear publicação no dashboard.
    """
    raw = _parse_metrics_block(analysis_text)
    if raw is None:
        return None, "missing", []

    model, errors = validate_metrics(raw, sport=sport)
    if errors:
        logger.warning(
            "METRICS falhou validação Pydantic (sport=%s): %d erro(s) — %s",
            sport, len(errors), "; ".join(errors[:3]),
        )
        # Retorna o dict cru para não regredir o dashboard
        return raw, "invalid", errors

    return metrics_as_dict(model), "valid", []


def _strip_metrics_block(analysis_text: str) -> str:
    """
    Remove o bloco <!-- METRICS ... --> do texto da análise,
    preservando o restante intacto. Útil para renderizar a análise
    no dashboard sem expor o bloco de métricas.
    """
    if not analysis_text:
        return analysis_text
    cleaned = _METRICS_BLOCK_RE.sub("", analysis_text)
    # Remove linhas em branco excedentes deixadas pela remoção
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).rstrip() + "\n"
    return cleaned


def _normalize_date(date_display: str) -> str:
    """Converte 'DD/MM/YYYY HH:MM' para 'YYYY-MM-DD'."""
    if not date_display:
        return datetime.now().strftime("%Y-%m-%d")
    try:
        parts = date_display[:10].replace("/", "-").split("-")
        if len(parts[0]) == 2:
            return f"{parts[2]}-{parts[1]}-{parts[0]}"
        return date_display[:10]
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _make_id(training_data: dict) -> str:
    """
    Gera ID único e estável para a entrada (Prioridade 5).

    Antes desta refatoração, o ID era `<date>_<slug_nome>` e duas atividades
    no mesmo dia com mesmo nome colidiam. Agora delega para
    activity_id.make_stable_id, que inclui o provider_activity_id quando
    disponível (Strava id / FIT serial+timestamp) e tem fallback hash
    determinístico para arquivos sem provider conhecido.

    DEVE permanecer em sincronia com payload_builder._make_activity_id —
    ambos delegam para o mesmo helper.
    """
    from src.activity_id import make_stable_id
    iso_date = _normalize_date(training_data.get("start_time_local", ""))
    return make_stable_id(training_data, iso_date)
