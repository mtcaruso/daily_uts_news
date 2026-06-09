"""Wrapper resiliente pro Gemini — compatível com free tier.

Centraliza a chamada ao Gemini com:
  - Throttle: intervalo mínimo entre chamadas (~13 RPM, sob o teto free ~15 RPM)
  - Backoff + retry em rate-limit transitório (RPM) e 503
  - Circuit breaker: quando créditos esgotam OU rate-limit dia (RPD) persiste,
    abre o circuito → para de chamar a API pelo resto do run (não desperdiça).
  - Fallback extrativo: primeiras frases do texto, pra cards não ficarem vazios.

Uso nos scrapers:
    import gemini_util
    try:
        summary = gemini_util.generate(prompt, max_output_tokens=500)
    except gemini_util.GeminiCircuitOpen:
        summary = gemini_util.extractive_summary(body)  # degrada suave
"""
import os
import re
import sys
import time

MODEL = "gemini-2.5-flash"

# Estado por processo/run
_CLIENT = None
_CIRCUIT_OPEN = False     # True quando créditos/quota-dia esgotaram
_LAST_CALL_TS = 0.0       # pra throttle
_MIN_INTERVAL = 4.5       # segundos entre chamadas → ~13 RPM (free tier ~15 RPM)


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
    """Reset manual (útil em testes)."""
    global _CIRCUIT_OPEN
    _CIRCUIT_OPEN = False


def _is_credits_depleted(err: str) -> bool:
    e = err.lower()
    return ("credit" in e or "billing" in e or "prepay" in e) and (
        "exhausted" in e or "429" in e or "depleted" in e
    )


def _is_rate_limit(err: str) -> bool:
    e = err.lower()
    return (
        "429" in e
        or "resource_exhausted" in e
        or "rate" in e
        or "quota" in e
    )


def generate(prompt, max_output_tokens=500, temperature=0.2, max_retries=4):
    """Gera texto com Gemini, resiliente a free tier.

    Retorna str (sucesso) ou None (falha pontual de 1 item — caller pode pular).
    Levanta GeminiCircuitOpen quando o circuito abre (créditos/RPD) — o caller
    deve então usar extractive_summary() e idealmente parar de chamar.
    """
    global _CIRCUIT_OPEN, _LAST_CALL_TS

    if _CIRCUIT_OPEN:
        raise GeminiCircuitOpen("circuito já aberto neste run")

    client = _client()
    if not client:
        return None

    from google.genai import types

    for attempt in range(max_retries):
        # Throttle pra respeitar RPM
        elapsed = time.time() - _LAST_CALL_TS
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)

        try:
            r = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
            _LAST_CALL_TS = time.time()
            return (r.text or "").strip() or None

        except Exception as e:
            _LAST_CALL_TS = time.time()
            err = str(e)

            # 1) Créditos esgotados → circuito aberto, sem retry (é persistente)
            if _is_credits_depleted(err):
                _CIRCUIT_OPEN = True
                print(
                    f"[gemini] CRÉDITOS ESGOTADOS — circuito aberto, resto do run "
                    f"usa fallback extrativo: {err[:120]}",
                    file=sys.stderr,
                )
                raise GeminiCircuitOpen(err)

            # 2) Rate-limit (RPM/RPD) ou 503 → backoff e retry
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
