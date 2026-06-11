"""Wrapper resiliente pro Gemini — compatível com free tier.

Centraliza a chamada ao Gemini com:
  - Throttle: intervalo mínimo entre chamadas (~13 RPM, sob o teto free ~15 RPM)
  - Backoff + retry em rate-limit transitório (RPM) e 503
  - Circuit breaker: quando créditos esgotam OU a cota DIÁRIA (RPD) bate, abre o
    circuito → para de chamar a API pelo resto do run (não desperdiça).
  - Guarda de cota PERSISTIDA: ao detectar RPD esgotada, grava um bloqueio em
    disco (.gemini_quota_state.json) até a meia-noite PT (reset do free tier).
    No próximo processo, o circuito já abre no início SEM gastar chamada — antes,
    CADA run redescobria a cota esgotada queimando ~5 chamadas 429 + 75s de
    backoff. (No self-host, que roda no mesmo checkout a cada 30min, isso vale
    pro dia todo; em GHA cada run é checkout novo, mas o ganho do "RPD = sem
    retry" abaixo já corta o desperdício de ~5 chamadas/run pra ~1.)
  - Fallback extrativo: primeiras frases do texto, pra cards não ficarem vazios.
  - generate_batch: resume N itens numa ÚNICA chamada (corta o nº de chamadas
    5-10x — principal alavanca de cota em backlog/recuperação).

Uso nos scrapers:
    import gemini_util
    try:
        summary = gemini_util.generate(prompt, max_output_tokens=500)
    except gemini_util.GeminiCircuitOpen:
        summary = gemini_util.extractive_summary(body)  # degrada suave
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODEL = "gemini-2.5-flash"

# Estado por processo/run
_CLIENT = None
_CIRCUIT_OPEN = False     # True quando créditos/quota-dia esgotaram
_LAST_CALL_TS = 0.0       # pra throttle
_MIN_INTERVAL = 4.5       # segundos entre chamadas → ~13 RPM (free tier ~15 RPM)

# Guarda de cota diária persistida (NÃO commitar — é estado local; ver .gitignore)
_QUOTA_STATE_FILE = Path(".gemini_quota_state.json")

try:
    from zoneinfo import ZoneInfo
    _PT = ZoneInfo("America/Los_Angeles")  # free tier reseta à meia-noite PT
except Exception:
    _PT = None


class GeminiCircuitOpen(Exception):
    """Gemini indisponível pelo resto do run (créditos esgotados ou RPD batido).
    O caller deve parar de tentar e usar fallback extrativo."""


def _client():
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    try:
        from google import genai
        _CLIENT = genai.Client(api_key=key)
        return _CLIENT
    except Exception as e:
        print(f"[gemini] init falhou: {e}", file=sys.stderr)
        return None


def circuit_open() -> bool:
    return _CIRCUIT_OPEN


def reset_circuit():
    """Reset manual (útil em testes). NÃO apaga o bloqueio RPD persistido."""
    global _CIRCUIT_OPEN
    _CIRCUIT_OPEN = False


# ============== GUARDA DE COTA DIÁRIA (RPD) PERSISTIDA ==============

def _next_pacific_midnight_iso() -> str:
    """ISO da próxima meia-noite PT (quando o free tier reseta o RPD)."""
    if _PT is not None:
        now_pt = datetime.now(_PT)
        d = (now_pt + timedelta(days=1)).date()
        reset = datetime(d.year, d.month, d.day, 0, 0, tzinfo=_PT)
        return reset.isoformat()
    # Fallback sem tzdata: próximo 08:00 UTC (≈ meia-noite PST, lado conservador)
    now = datetime.now(timezone.utc)
    reset = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    return reset.isoformat()


def _read_block_until():
    try:
        if _QUOTA_STATE_FILE.exists():
            return json.loads(_QUOTA_STATE_FILE.read_text(encoding="utf-8")).get("rpd_blocked_until")
    except Exception:
        pass
    return None


def _is_blocked_now() -> bool:
    """True se há bloqueio RPD persistido e ainda não passou da hora de reset.
    Compara datetimes tz-aware (não strings) pra não errar por offset."""
    until = _read_block_until()
    if not until:
        return False
    try:
        until_dt = datetime.fromisoformat(until)
        now = datetime.now(until_dt.tzinfo) if until_dt.tzinfo else datetime.now()
        return now < until_dt
    except Exception:
        return False


def _write_block() -> str:
    until = _next_pacific_midnight_iso()
    try:
        _QUOTA_STATE_FILE.write_text(
            json.dumps({"rpd_blocked_until": until, "set_at": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
    except Exception:
        pass
    return until


def _init_circuit_from_block():
    """No início do processo: se a cota diária já estava bloqueada, abre o
    circuito de cara — assim a 1a chamada já levanta GeminiCircuitOpen sem
    bater na API (evita o desperdício de redescobrir a cota esgotada)."""
    global _CIRCUIT_OPEN
    if _is_blocked_now():
        _CIRCUIT_OPEN = True
        print(
            f"[gemini] cota diária (RPD) ainda bloqueada até {_read_block_until()} "
            f"(estado persistido) — circuito aberto no início do run",
            file=sys.stderr,
        )


def _is_credits_depleted(err: str) -> bool:
    e = err.lower()
    return ("credit" in e or "billing" in e or "prepay" in e) and (
        "exhausted" in e or "429" in e or "depleted" in e
    )


def _is_rpd_exhausted(err: str) -> bool:
    """Detecta cota DIÁRIA (per-day) esgotada — persistente até reset PT.
    Distingue de RPM (per-minute), que é transitório e merece retry."""
    e = err.lower()
    compact = e.replace(" ", "")
    if "perminute" in compact:  # explicitamente por-minuto → NÃO é diário
        return False
    return "perday" in compact or "per day" in e or "requestsperday" in compact


def _is_rate_limit(err: str) -> bool:
    e = err.lower()
    return (
        "429" in e
        or "resource_exhausted" in e
        or "rate" in e
        or "quota" in e
    )


def generate(prompt, max_output_tokens=500, temperature=0.2, max_retries=4, response_json=False):
    """Gera texto com Gemini, resiliente a free tier.

    Retorna str (sucesso) ou None (falha pontual de 1 item — caller pode pular).
    Levanta GeminiCircuitOpen quando o circuito abre (créditos/RPD) — o caller
    deve então usar extractive_summary() e idealmente parar de chamar.

    response_json=True força saída JSON (usado pelo generate_batch).
    """
    global _CIRCUIT_OPEN, _LAST_CALL_TS

    if _CIRCUIT_OPEN:
        raise GeminiCircuitOpen("circuito já aberto neste run")

    client = _client()
    if not client:
        return None

    from google.genai import types

    cfg_kwargs = dict(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    if response_json:
        cfg_kwargs["response_mime_type"] = "application/json"
    config = types.GenerateContentConfig(**cfg_kwargs)

    for attempt in range(max_retries):
        # Throttle pra respeitar RPM
        elapsed = time.time() - _LAST_CALL_TS
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)

        try:
            r = client.models.generate_content(model=MODEL, contents=prompt, config=config)
            _LAST_CALL_TS = time.time()
            return (r.text or "").strip() or None

        except Exception as e:
            _LAST_CALL_TS = time.time()
            err = str(e)

            # 1) Persistente (créditos esgotados OU cota DIÁRIA/RPD) → circuito
            #    aberto SEM retry. RPD persiste em disco até o reset PT.
            if _is_credits_depleted(err) or _is_rpd_exhausted(err):
                _CIRCUIT_OPEN = True
                if _is_rpd_exhausted(err):
                    until = _write_block()
                    print(
                        f"[gemini] COTA DIÁRIA (RPD) ESGOTADA — bloqueado até {until}, "
                        f"circuito aberto, resto usa fallback extrativo: {err[:100]}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"[gemini] CRÉDITOS ESGOTADOS — circuito aberto: {err[:120]}",
                        file=sys.stderr,
                    )
                raise GeminiCircuitOpen(err)

            # 2) Rate-limit transitório (RPM) ou 503 → backoff e retry
            if _is_rate_limit(err) or "503" in err or "unavailable" in err.lower():
                if attempt < max_retries - 1:
                    wait = min(60, 5 * (2 ** attempt))  # 5, 10, 20, 40
                    print(
                        f"[gemini] rate-limit/503 — aguardando {wait}s "
                        f"(tentativa {attempt + 1}/{max_retries}): {err[:80]}",
                        file=sys.stderr,
                    )
                    time.sleep(wait)
                    continue
                # Persistiu após retries → trata como indisponível pro run
                _CIRCUIT_OPEN = True
                print(
                    f"[gemini] rate-limit persistente após {max_retries} tentativas "
                    f"— circuito aberto: {err[:120]}",
                    file=sys.stderr,
                )
                raise GeminiCircuitOpen(err)

            # 3) Outro erro → falha pontual desse item (não abre circuito)
            print(f"[gemini] erro pontual: {err[:160]}", file=sys.stderr)
            return None

    return None


# ============== BATCHING (N itens por chamada) ==============

def generate_batch(items, instruction, max_output_tokens=1200, temperature=0.2):
    """Resume VÁRIOS itens numa ÚNICA chamada Gemini — economiza cota/RPD.

    items: lista de dicts {"id": str, "title": str, "text": str}.
    instruction: como resumir cada item (mesmo estilo do prompt single).
    Retorna dict {id: summary_str}. Ids ausentes/ruins ficam de fora — o caller
    faz fallback (extractive ou retry single) para os que faltarem.
    Levanta GeminiCircuitOpen se o circuito abrir (caller para e usa extractive).
    """
    items = [it for it in items if (it.get("text") or "").strip()]
    if not items:
        return {}

    payload = [
        {"id": str(it["id"]), "title": (it.get("title") or "")[:300], "text": (it.get("text") or "")[:4000]}
        for it in items
    ]
    prompt = (
        f"{instruction}\n\n"
        "Você vai resumir VÁRIOS itens de uma vez. Responda APENAS um objeto JSON "
        'válido no formato {"id1": "resumo do item 1", "id2": "resumo do item 2", ...} '
        "— uma entrada por item, usando EXATAMENTE o id fornecido. Nada fora do JSON.\n\n"
        "ITENS (JSON):\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )

    raw = generate(prompt, max_output_tokens=max_output_tokens, temperature=temperature, response_json=True)
    if not raw:
        return {}
    return _parse_batch_json(raw, {str(it["id"]) for it in items})


def _parse_batch_json(raw: str, valid_ids: set) -> dict:
    """Parse robusto da resposta-batch. Aceita só ids esperados, valores string."""
    data = None
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)  # tenta extrair o bloco {...}
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        if str(k) in valid_ids and isinstance(v, str) and v.strip():
            out[str(k)] = v.strip()
    return out


# ============== FALLBACK EXTRATIVO ==============

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def extractive_summary(text, max_sentences=2, max_chars=400):
    """Resumo extrativo simples: primeiras N frases do texto.
    Usado quando o Gemini está indisponível (circuito aberto)."""
    if not text:
        return None
    clean = " ".join(text.split()).strip()
    if len(clean) < 40:
        return None
    sentences = _SENT_SPLIT.split(clean)
    out = " ".join(sentences[:max_sentences]).strip()
    if len(out) > max_chars:
        cut = out.rfind(" ", 0, max_chars)
        out = (out[:cut] if cut > 80 else out[:max_chars]).rstrip(",;: ") + "…"
    return out or None


# Inicializa o circuito a partir do bloqueio RPD persistido (import-time).
_init_circuit_from_block()
