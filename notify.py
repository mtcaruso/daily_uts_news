"""Notificação unificada — WhatsApp (CallMeBot) + ntfy, com retry e prioridade.

Substitui as 4 implementações divergentes de envio que existiam (alerts.py,
cvm_realtime.py, sei_monitor.py, mme_legislacao.py). Envia pros DOIS canais
(redundância): se um falhar, o outro entrega — era fire-and-forget, agora tem
retry + fallback.

Canais (envia em TODOS os configurados, por redundância):
  - ntfy.sh — push direto, INSTANTÂNEO (sempre, topic em NTFY_TOPIC).
  - Telegram — bot, INSTANTÂNEO e grátis, se TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID.
  - WhatsApp via CallMeBot — grátis/pessoal, mas enfileira (~até 1min, beta deles),
    se CALLMEBOT_PHONE + CALLMEBOT_APIKEY.

Env vars:
  NTFY_TOPIC          topic do ntfy (default: utl-mtc-621qmvsd).
  TELEGRAM_BOT_TOKEN  token do bot (@BotFather). Sem ele, pula Telegram.
  TELEGRAM_CHAT_ID    seu chat id (veja getUpdates). Sem ele, pula Telegram.
  CALLMEBOT_PHONE     número com DDI, ex: +5511999998888. Sem ele, pula WhatsApp.
  CALLMEBOT_APIKEY    a APIKEY que o CallMeBot devolve na ativação.

CallMeBot tem rate-limit (free, ~1 msg a cada poucos segundos) — ok pro volume
normal de alertas (0-3/run); o retry com backoff cobre throttle eventual.
"""
import os
import sys
import time
import urllib.parse

import requests

NTFY_BASE = "https://ntfy.sh"
DEFAULT_NTFY_TOPIC = "utl-mtc-621qmvsd"
CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"

# nome de prioridade → nível do ntfy (1=min … 5=urgent/max)
_NTFY_PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


def _ntfy_topic() -> str:
    return os.environ.get("NTFY_TOPIC") or DEFAULT_NTFY_TOPIC


def _send_ntfy(title, message, click=None, priority="default", tags=None, retries=2) -> bool:
    """POST JSON (suporta UTF-8 no título). Retry com backoff curto."""
    payload = {
        "topic": _ntfy_topic(),
        "title": title,
        "message": message,
        "priority": _NTFY_PRIORITY.get(priority, 3),
    }
    if click:
        payload["click"] = click
    if tags:
        payload["tags"] = tags
    for attempt in range(retries):
        try:
            r = requests.post(NTFY_BASE, json=payload, timeout=10)
            if r.status_code == 200:
                return True
            print(f"[notify/ntfy] HTTP {r.status_code} (tentativa {attempt+1})", file=sys.stderr)
        except Exception as e:
            print(f"[notify/ntfy] tentativa {attempt+1} falhou: {e}", file=sys.stderr)
        time.sleep(1.5 * (attempt + 1))
    return False


def _send_whatsapp(title, message, click=None, retries=2) -> bool:
    """WhatsApp via CallMeBot. No-op silencioso se não configurado (ntfy cobre)."""
    phone = os.environ.get("CALLMEBOT_PHONE", "").strip()
    apikey = os.environ.get("CALLMEBOT_APIKEY", "").strip()
    if not phone or not apikey:
        return False
    # WhatsApp não tem campo de link separado — junta título+corpo+link no texto.
    text = f"{title}\n{message}"
    if click:
        text += f"\n{click}"
    url = CALLMEBOT_URL + "?" + urllib.parse.urlencode(
        {"phone": phone, "text": text, "apikey": apikey}
    )
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=15)
            body = (r.text or "").lower()
            # CallMeBot devolve 200 mesmo em alguns erros — checa o corpo.
            if r.status_code == 200 and "error" not in body[:120] and "apikey" not in body[:120]:
                return True
            print(f"[notify/whatsapp] resposta inesperada (tentativa {attempt+1}): {body[:80]}", file=sys.stderr)
        except Exception as e:
            print(f"[notify/whatsapp] tentativa {attempt+1} falhou: {e}", file=sys.stderr)
        time.sleep(2 * (attempt + 1))
    return False


def _send_telegram(title, message, click=None, retries=2) -> bool:
    """Telegram Bot API — INSTANTÂNEO e grátis. No-op se não configurado.
    TELEGRAM_CHAT_ID aceita VÁRIOS destinos separados por vírgula (ex.: seu
    privado + um grupo com colegas, ou vários ids). Retorna True se ao menos um
    destino recebeu."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not token or not chat_ids:
        return False
    text = f"{title}\n{message}"
    if click:
        text += f"\n{click}"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    any_ok = False
    for chat_id in chat_ids:
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        for attempt in range(retries):
            try:
                r = requests.post(url, json=payload, timeout=10)
                if r.status_code == 200:
                    any_ok = True
                    break
                print(f"[notify/telegram] chat {chat_id} HTTP {r.status_code}: {(r.text or '')[:100]}", file=sys.stderr)
            except Exception as e:
                print(f"[notify/telegram] chat {chat_id} tentativa {attempt+1}: {e}", file=sys.stderr)
            time.sleep(1.5 * (attempt + 1))
    return any_ok


def send(title, message, click=None, priority="default", tags=None) -> bool:
    """Envia pros canais configurados, por redundância. Retorna True se PELO
    MENOS UM entregou. Ordem: INSTANTÂNEOS primeiro (ntfy, Telegram), WhatsApp
    (CallMeBot) por último — ele enfileira e atrasa ~até 1min (beta deles)."""
    nt = _send_ntfy(title, message, click=click, priority=priority, tags=tags)
    tg = _send_telegram(title, message, click=click)
    wa = _send_whatsapp(title, message, click=click)
    if not (nt or tg or wa):
        print(f"[notify] FALHA em TODOS os canais: {title!r}", file=sys.stderr)
    return nt or tg or wa
