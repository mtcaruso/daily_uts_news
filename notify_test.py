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

print("\n=== notify.send (WhatsApp + ntfy) ===")
ok = notify.send("🧪 Teste 2", "diagnóstico de canal", priority="high")
print("notify.send entregou em ao menos 1 canal:", ok)
