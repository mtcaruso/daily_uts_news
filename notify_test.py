"""Diagnóstico do canal WhatsApp (CallMeBot) — rodado pelo notify-test.yml.

Imprime no log (sem expor o valor dos secrets — o GitHub mascara) se as
credenciais estão presentes e a RESPOSTA CRUA do CallMeBot, que diz o problema
exato (apikey inválida, telefone não ativado, formato errado, etc.).
"""
import os
import urllib.parse

import requests

import notify

phone = os.environ.get("CALLMEBOT_PHONE", "")
key = os.environ.get("CALLMEBOT_APIKEY", "")

print("=== Diagnóstico CallMeBot / WhatsApp ===")
print(f"CALLMEBOT_PHONE presente? {bool(phone)} (len={len(phone)}, tem '+'? {phone.startswith('+') if phone else 'n/a'})")
print(f"CALLMEBOT_APIKEY presente? {bool(key)} (len={len(key)})")

if not phone or not key:
    print("⚠️ Um dos secrets está VAZIO/ausente. Confira os NOMES EXATOS no GitHub:")
    print("   Settings → Secrets and variables → Actions → devem existir:")
    print("   CALLMEBOT_PHONE  e  CALLMEBOT_APIKEY")
else:
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
        {"phone": phone, "text": "Teste CallMeBot via GitHub Actions", "apikey": key}
    )
    try:
        r = requests.get(url, timeout=20)
        print(f"CallMeBot → HTTP {r.status_code}")
        print(f"Resposta do CallMeBot (diz o problema, se houver):\n  {r.text[:400]}")
    except Exception as e:
        print(f"Erro ao chamar CallMeBot: {e}")

print("\n=== Diagnóstico WhatsApp Cloud API (Meta, p/ time) ===")
_wt = os.environ.get("WHATSAPP_TOKEN", ""); _wp = os.environ.get("WHATSAPP_PHONE_ID", "")
_wr = [r for r in os.environ.get("WHATSAPP_RECIPIENTS", "").split(",") if r.strip()]
print(f"WHATSAPP_TOKEN presente? {bool(_wt)} | PHONE_ID? {bool(_wp)} | destinatários: {len(_wr)} | template: {os.environ.get('WHATSAPP_TEMPLATE','alerta_cvm')!r}")

print("\n=== PREVIEW dos 2 alertas (ntfy + Telegram + WhatsApp) ===")
# 1) Exemplo de Fato Relevante (CVM) — vai TAMBÉM pro WhatsApp Cloud (wa_cloud=True)
ok1 = notify.send(
    "🚨 Eletrobras · Fato Relevante",
    "[TESTE] Exemplo de como um Fato Relevante vai chegar — prioridade alta.",
    click="https://www.rad.cvm.gov.br/",
    priority="high", tags=["scroll"], wa_cloud=True,
)
# 2) Exemplo de notícia Top do Dia com nota alta
ok2 = notify.send(
    "🔥 Top notícia · nota 78",
    "[TESTE] Exemplo de notícia com nota >=60 (Top do Dia) — só as de alto sinal.",
    click="https://example.com",
    priority="high", tags=["fire"],
)
print("FR de teste entregue:", ok1, "| Top-notícia de teste entregue:", ok2)
