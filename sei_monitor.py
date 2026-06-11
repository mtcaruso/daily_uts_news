"""SEI ANEEL processo monitor (acesso público por link com hash).

Lê lista de processos em sei_processes.json e checa novos andamentos/documentos.
Notifica via ntfy quando aparece movimento novo.

NÃO requer login nem captcha — usa o endpoint público md_pesq_processo_exibir.php
com hash compartilhável.

Roda local (via local_refresh.bat) — Cloudflare bloqueia POST/captcha de datacenter
mas o GET com hash funciona de qualquer IP.
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from curl_cffi import requests as cf

import notify

PROCESSES_FILE = Path("sei_processes.json")
NTFY_TOPIC = "utl-mtc-621qmvsd"
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
MAX_RETRIES = 3


# ============== PARSER ==============

def _clean(s: str) -> str:
    """Strip HTML, collapse whitespace."""
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"&nbsp;", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_process(url: str) -> dict:
    """Fetch + parse processo SEI público.

    Retorna dict com:
      - processo, tipo, data_geracao, interessados (do cabeçalho)
      - documentos: lista de {protocolo, tipo, data, data_inclusao, unidade}
      - andamentos: lista de {datahora, unidade, descricao, hash}
    """
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            r = cf.get(url, impersonate="chrome120", timeout=30)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                time.sleep(2 ** attempt)
                continue
            break
        except Exception as e:
            last_err = str(e)
            time.sleep(2 ** attempt)
    else:
        raise RuntimeError(f"fetch failed: {last_err}")

    html = r.text
    result = {
        "url": url,
        "fetched_at": datetime.now().isoformat(),
        "processo": None,
        "tipo": None,
        "data_geracao": None,
        "interessados": None,
        "documentos": [],
        "andamentos": [],
    }

    # === Cabeçalho ===
    m_table = re.search(
        r'<table[^>]+id="tblCabecalho"[^>]*>(.*?)</table>', html, re.DOTALL
    )
    if m_table:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m_table.group(1), re.DOTALL)
        for row in rows:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
            if len(cells) >= 2:
                label = _clean(cells[0]).rstrip(":").lower()
                value = _clean(cells[1])
                if label == "processo":
                    result["processo"] = value
                elif label == "tipo":
                    result["tipo"] = value
                elif "data" in label:
                    result["data_geracao"] = value
                elif "interessado" in label:
                    result["interessados"] = value

    # === Documentos ===
    m_table = re.search(
        r'<table[^>]+id="tblDocumentos"[^>]*>(.*?)</table>', html, re.DOTALL
    )
    if m_table:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m_table.group(1), re.DOTALL)
        for row in rows[1:]:  # skip header
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
            clean = [_clean(c) for c in cells]
            # Colunas: '', protocolo, tipo, data, data_inclusao, unidade, ''
            if len(clean) >= 6 and clean[1]:
                result["documentos"].append({
                    "protocolo": clean[1],
                    "tipo": clean[2],
                    "data": clean[3],
                    "data_inclusao": clean[4],
                    "unidade": clean[5],
                })

    # === Andamentos ===
    m_table = re.search(
        r'<table[^>]+id="tblHistorico"[^>]*>(.*?)</table>', html, re.DOTALL
    )
    if m_table:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m_table.group(1), re.DOTALL)
        for row in rows[1:]:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
            clean = [_clean(c) for c in cells]
            if len(clean) >= 3 and clean[0]:
                datahora, unidade, descricao = clean[0], clean[1], clean[2]
                h = hashlib.md5(
                    f"{datahora}|{unidade}|{descricao}".encode("utf-8")
                ).hexdigest()[:12]
                result["andamentos"].append({
                    "datahora": datahora,
                    "unidade": unidade,
                    "descricao": descricao,
                    "hash": h,
                })

    return result


# ============== MONITOR ==============

def load_processes() -> list:
    if not PROCESSES_FILE.exists():
        return []
    return json.loads(PROCESSES_FILE.read_text(encoding="utf-8"))


def save_processes(processes: list):
    PROCESSES_FILE.write_text(
        json.dumps(processes, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def notify_ntfy(label: str, new_andamentos: list, processo: str):
    """Manda notificação pro canal. DESLIGADO a pedido do usuário (11/06/2026):
    só FR/Comunicado e Top do Dia >=60 notificam. O scraper segue rodando e
    atualizando sei_processes.json (o dashboard continua mostrando), só o push
    foi cortado. Pra reativar, remova o return abaixo."""
    return
    n = len(new_andamentos)
    title = f"📋 SEI {processo}: {n} movimento{'s' if n > 1 else ''}"
    # Body: até 3 andamentos mais recentes
    body_lines = [f"[{label}]"]
    for a in new_andamentos[:3]:
        body_lines.append(f"{a['datahora']} {a['unidade']}: {a['descricao'][:80]}")
    if n > 3:
        body_lines.append(f"...e mais {n - 3}")
    body = "\n".join(body_lines)
    notify.send(title, body, tags=["page_facing_up"])


def monitor_all():
    """Itera por todos os processos cadastrados, detecta novos andamentos."""
    processes = load_processes()
    if not processes:
        print("[sei_monitor] Nenhum processo cadastrado em sei_processes.json", file=sys.stderr)
        return

    print(f"[sei_monitor] Monitorando {len(processes)} processos…", file=sys.stderr)
    total_new = 0
    for p in processes:
        label = p.get("label") or p.get("processo") or "(sem label)"
        url = p.get("url")
        if not url:
            continue

        try:
            data = parse_process(url)
        except Exception as e:
            print(f"  ⚠️ [{label}] erro: {e}", file=sys.stderr)
            continue

        # Atualiza metadados (caso novo)
        if not p.get("processo"):
            p["processo"] = data["processo"]
        if not p.get("tipo"):
            p["tipo"] = data["tipo"]

        # Detecta novos andamentos (vs hashes já vistos)
        seen_hashes = set(p.get("andamentos_seen", []))
        all_hashes = [a["hash"] for a in data["andamentos"]]
        new_andamentos = [a for a in data["andamentos"] if a["hash"] not in seen_hashes]

        if new_andamentos and p.get("andamentos_seen"):
            # Tem histórico (não é primeira run) e tem coisa nova → alerta
            print(f"  ✨ [{label}] {len(new_andamentos)} andamento(s) novo(s)", file=sys.stderr)
            for a in new_andamentos[:5]:
                print(f"     {a['datahora']} {a['unidade']}: {a['descricao'][:80]}", file=sys.stderr)
            if p.get("ntfy_enabled", True):
                notify_ntfy(label, new_andamentos, data["processo"] or "?")
            total_new += len(new_andamentos)
        elif not p.get("andamentos_seen"):
            print(f"  + [{label}] primeira run — {len(all_hashes)} andamentos capturados (sem alerta)", file=sys.stderr)

        # Atualiza estado
        p["andamentos_seen"] = all_hashes
        p["andamento_count"] = len(data["andamentos"])
        p["documento_count"] = len(data["documentos"])
        p["last_check_at"] = datetime.now().isoformat()

        # Salva timeline completa pra UI ler
        p["last_andamentos_top10"] = data["andamentos"][:10]
        p["last_documentos_top10"] = data["documentos"][:10]

        time.sleep(2)  # gentle ao SEI

    save_processes(processes)
    print(f"[sei_monitor] DONE: {total_new} novos andamentos no total", file=sys.stderr)


if __name__ == "__main__":
    monitor_all()
