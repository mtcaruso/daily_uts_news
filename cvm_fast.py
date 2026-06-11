"""Poller RÁPIDO de Fato Relevante / Comunicado — via B3 (near-real-time).

EFICIENTE: em vez de pollar 40 endpoints da CVM RAD (1 por código), faz UM
request ao endpoint oficial da B3 (`GetMaterialFacts`) que lista os documentos
de TODAS as empresas listadas por período. Filtra client-side pras NOSSAS 18
empresas + categorias FR/Comunicado. 1 request leve (~0,4s) → dá pra pollar de
~1min, latência de FR ~1min (o mais perto da publicação que dá sem feed pago).

Fonte: B3 sistemaswebb3-listados → reportsPeriodProxy/ReportsPeriodCall/
GetMaterialFacts/<base64(params)>. Params: language, pageNumber, pageSize,
dateInitial, dateFinal. Retorna results[] com company.codeCVM, category,
deliveryDateTime, urlSearch (link do PDF) e numProtocolo (no urlDownload).

Dedup por protocolo na MESMA chave alerts_state.json['cvm'] do cvm_realtime —
não duplica. Cada ciclo faz git pull pra ver o que os outros já alertaram.
Commita só quando há FR novo (raro → sem spam de git). O cvm_realtime (15min)
segue de fallback (e dono do histórico cvm_recent.json).

Roda em loop pelo cvm-fast.yml.
"""
import base64
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import notify
from cvm_realtime import ALERTS_STATE_FILE, ALERT_CATEGORIES, COMPANIES_CODIGOS, PDF_URL

BRT = ZoneInfo("America/Sao_Paulo")
B3_URL = ("https://sistemaswebb3-listados.b3.com.br/"
          "reportsPeriodProxy/ReportsPeriodCall/GetMaterialFacts/{}")
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# codeCVM normalizado (sem zeros à esquerda) → label da empresa
_CODE_TO_EMPRESA = {
    str(int(c)): emp
    for emp, cods in COMPANIES_CODIGOS.items()
    for c in cods
}


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _fetch_b3(date_ini: str, date_fim: str, page_size: int = 2000) -> list:
    params = {"language": "pt-br", "pageNumber": 1, "pageSize": page_size,
              "dateInitial": date_ini, "dateFinal": date_fim}
    b64 = base64.b64encode(json.dumps(params).encode()).decode()
    r = requests.get(B3_URL.format(b64), headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.json().get("results", []) or []


def _load_cvm_alerted() -> set:
    if ALERTS_STATE_FILE.exists():
        try:
            return set(json.loads(ALERTS_STATE_FILE.read_text(encoding="utf-8")).get("cvm", []))
        except Exception:
            pass
    return set()


def poll_once() -> int:
    # sincroniza pra dedup com cvm_realtime/outros writers
    _git("pull", "--rebase", "-X", "theirs", "--autostash", "origin", "main")
    cvm_alerted = _load_cvm_alerted()

    hoje = datetime.now(BRT)
    ontem = hoje - timedelta(days=1)  # janela 2 dias cobre FR perto da meia-noite
    try:
        results = _fetch_b3(ontem.strftime("%Y-%m-%d"), hoje.strftime("%Y-%m-%d"))
    except Exception as e:
        print(f"[cvm_fast] B3 erro: {e}", file=sys.stderr)
        return 0

    novos = []
    for x in results:
        if x.get("category") not in ALERT_CATEGORIES:
            continue
        try:
            code_norm = str(int(x.get("company", {}).get("codeCVM", "")))
        except Exception:
            continue
        empresa = _CODE_TO_EMPRESA.get(code_norm)
        if not empresa:
            continue
        m = re.search(r"numProtocolo=(\d+)", x.get("urlDownload", "") or "")
        proto = m.group(1) if m else None
        if not proto or proto in cvm_alerted:
            continue
        novos.append((proto, empresa, x))

    if not novos:
        print("[cvm_fast] nada novo (B3)", file=sys.stderr)
        return 0

    for proto, empresa, x in novos:
        is_fr = x["category"] == "Fato Relevante"
        emoji = "🚨" if is_fr else "📋"
        title = f"{emoji} {empresa} · {x['category']}"
        body = (x.get("type") or x.get("subject") or "").strip() or "Documento publicado — toque pra ver"
        click = x.get("urlSearch") or PDF_URL.format(proto)
        notify.send(title, body, click=click,
                    priority="high" if is_fr else "default", tags=["scroll"])
        cvm_alerted.add(proto)

    # persiste + commita SÓ quando há novo (raro)
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
        _git("commit", "-m", "cvm_fast(B3): FR/Comunicado novo [skip ci]")
        _git("pull", "--rebase", "-X", "theirs", "--autostash", "origin", "main")
        _git("push")
    print(f"[cvm_fast] {len(novos)} novos FR/Comunicado (B3)", file=sys.stderr)
    return len(novos)


if __name__ == "__main__":
    poll_once()
