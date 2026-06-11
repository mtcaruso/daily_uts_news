"""Poller RÁPIDO de Fato Relevante / Comunicado ao Mercado da CVM — near-real-time.

Faz UM ciclo de checagem (stateless). Rodado num LOOP apertado (~3min) pelo
workflow cvm-fast.yml (loop contínuo de ~5h, cron reinicia o job) — e também
dá pra rodar no self-host. Latência de FR cai de ~40min (mediana do
cvm_realtime de 15min) pra ~3min.

Foco em VELOCIDADE: só checa as categorias críticas (FR/Comunicado), dá push
IMEDIATO via notify (WhatsApp + ntfy), e commita alerts_state.json APENAS
quando há item novo — então commit é raro (= sem spam de git). O cvm_realtime.py
(15min) segue dono do histórico/cvm_recent.json e cobre subsidiárias/gaps.

Dedup compartilhado: alerts_state.json['cvm'] por protocolo. Cada ciclo faz um
`git pull --rebase` pra ver o que os outros writers (cvm_realtime/self-host) já
alertaram, evitando push duplicado.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import requests

import notify
from cvm_realtime import (ALERTS_STATE_FILE, ALERT_CATEGORIES, COMPANIES_CODIGOS,
                          scrape_codigo)

FAST_LOOKBACK_DAYS = 2  # só docs bem recentes — pega o FR na hora que sai


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _sync_state():
    """Pull rápido pra ver o que os outros writers já alertaram (dedup)."""
    _git("pull", "--rebase", "-X", "theirs", "--autostash", "origin", "main")


def _load_cvm_alerted() -> set:
    if ALERTS_STATE_FILE.exists():
        try:
            return set(json.loads(ALERTS_STATE_FILE.read_text(encoding="utf-8")).get("cvm", []))
        except Exception:
            pass
    return set()


def poll_once() -> int:
    _sync_state()
    cvm_alerted = _load_cvm_alerted()

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    pushed = 0
    for empresa, codigos in COMPANIES_CODIGOS.items():
        for codigo in codigos:
            try:
                items = scrape_codigo(codigo, session, days_back=FAST_LOOKBACK_DAYS)
            except Exception as e:
                print(f"[cvm_fast] {empresa}/{codigo}: {e}", file=sys.stderr)
                continue
            for item in items:
                if item["categoria"] not in ALERT_CATEGORIES:
                    continue
                if item["protocolo"] in cvm_alerted:
                    continue
                is_fr = item["categoria"] == "Fato Relevante"
                emoji = "🚨" if is_fr else "📋"
                title = f"{emoji} {empresa} · {item['categoria']}"
                body = (item.get("assunto") or item.get("tipo") or "(sem assunto)")[:200]
                # FR = prioridade ALTA (toca/vibra diferente); Comunicado = default.
                notify.send(title, body, click=item.get("link_pdf"),
                            priority="high" if is_fr else "default", tags=["scroll"])
                cvm_alerted.add(item["protocolo"])
                pushed += 1
            time.sleep(0.2)  # polidez com a CVM

    if pushed:
        # Persiste + commita SÓ quando houve item novo (raro). Reusa a mesma chave
        # 'cvm' do alerts_state.json (dedup com cvm_realtime/alerts).
        st = {"news": [], "dou": [], "cvm": [], "news_score": []}
        if ALERTS_STATE_FILE.exists():
            try:
                st = json.loads(ALERTS_STATE_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        st["cvm"] = list(cvm_alerted)[-2000:]
        ALERTS_STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        _git("add", str(ALERTS_STATE_FILE))
        if _git("diff", "--cached", "--quiet").returncode != 0:
            _git("commit", "-m", "cvm_fast: FR/Comunicado novo [skip ci]")
            _git("pull", "--rebase", "-X", "theirs", "--autostash", "origin", "main")
            _git("push")
        print(f"[cvm_fast] {pushed} novos FR/Comunicado pushados", file=sys.stderr)
    return pushed


if __name__ == "__main__":
    poll_once()
