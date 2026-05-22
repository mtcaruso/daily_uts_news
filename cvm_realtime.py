"""
Scraper real-time da CVM RAD (Sistema Empresas.NET).

Cada run (30min via workflow):
  1. Pra cada empresa-alvo + seus codigoCVM (incluindo subsidiárias), POST direto
     ao endpoint /ListarDocumentos da CVM, recupera docs dos últimos 7 dias.
  2. Compara com cvm_recent.json (state commitado), identifica protocolos novos.
  3. Adiciona ao histórico (cap 30 dias).
  4. Envia push ntfy pra Fatos Relevantes e Comunicados ao Mercado novos.

Bypassa o lag de 4-7 dias do CSV anual da CVM. Latência: minutos.
"""
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

# Empresas alvo com seus codigoCVM (extraídos do CSV anual via cruzamento de nome).
# Primeiro código de cada lista é o "principal" (holding/main); demais são subsidiárias.
COMPANIES_CODIGOS = {
    "Eletrobras":    ["2437", "27472", "3328"],
    "CTEEP / ISA":   ["18376"],
    "Equatorial":    ["20010", "18309", "25577"],
    "Cemig":         ["2453", "20303", "20320"],
    "Copel":         ["14311", "26808", "24740"],
    "Light":         ["19879", "8036"],
    "Engie":         ["17329"],
    "EDP":           ["19763"],
    "Neoenergia":    ["15539"],
    "Taesa":         ["20257"],
    "Energisa":      ["15253", "14605", "5576"],
    "Auren":         ["26620", "26174", "25640"],
    "CPFL Energia":  ["18660", "3824", "3204"],
    "Sabesp":        ["14443"],
    "Copasa":        ["19445"],
    "Sanepar":       ["18627"],
}

# Categorias que disparam push notification quando aparecem novas
ALERT_CATEGORIES = {"Fato Relevante", "Comunicado ao Mercado"}

DAYS_LOOKBACK = 7           # quantos dias buscar a cada run
HISTORY_RETENTION_DAYS = 30  # mantém 30 dias no recent.json
HISTORY_FILE = Path("cvm_recent.json")
ALERTS_STATE_FILE = Path("alerts_state.json")  # reusa o state file dos alerts

GET_URL = "https://www.rad.cvm.gov.br/ENET/frmConsultaExternaCVM.aspx?codigoCVM={}"
LISTAR_URL = "https://www.rad.cvm.gov.br/ENET/frmConsultaExternaCVM.aspx/ListarDocumentos"
PDF_URL = "https://www.rad.cvm.gov.br/ENET/frmExibirArquivoIPEExterno.aspx?NumeroProtocoloEntrega={}"

NTFY_TOPIC = "utl-mtc-621qmvsd"  # mesmo topic dos alerts


def _clean_html(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def _parse_records(raw: str) -> list[dict]:
    """Parse formato custom: registros separados por '&*', campos por '$&'."""
    if not raw:
        return []
    out = []
    for rec in raw.split("&*"):
        if not rec.strip():
            continue
        fields = rec.split("$&")
        if len(fields) < 11:
            continue
        botao = fields[10] if len(fields) > 10 else ""
        protocolo_match = re.search(r"NumeroProtocoloEntrega=(\d+)", botao)
        protocolo = protocolo_match.group(1) if protocolo_match else None
        if not protocolo:
            continue
        out.append({
            "cnpj_short": fields[0].strip(),
            "empresa_nome": fields[1].strip(),
            "categoria": fields[2].strip(),
            "tipo": _clean_html(fields[3]),
            "especie": _clean_html(fields[4]),
            "data_referencia": _clean_html(fields[5]),
            "data_entrega": _clean_html(fields[6]),
            "status": fields[7].strip(),
            "versao": fields[8].strip(),
            "modalidade": fields[9].strip(),
            "assunto": _clean_html(fields[11]) if len(fields) > 11 else "",
            "protocolo": protocolo,
            "link_pdf": PDF_URL.format(protocolo),
        })
    return out


def scrape_codigo(codigo: str, session: requests.Session, days_back: int = DAYS_LOOKBACK) -> list[dict]:
    """Scrape docs recentes pra um codigoCVM."""
    codigo_padded = codigo.zfill(6)
    data_ate = datetime.now().strftime("%d/%m/%Y")
    data_de = (datetime.now() - timedelta(days=days_back)).strftime("%d/%m/%Y")

    # Inicializa sessão (cookies) — necessário antes do POST
    session.get(GET_URL.format(codigo), timeout=20)

    body = {
        "dataDe": data_de, "dataAte": data_ate,
        "empresa": f",{codigo_padded}",
        "setorAtividade": "-1", "categoriaEmissor": "-1",
        "situacaoEmissor": "-1", "tipoParticipante": "-1",
        "dataReferencia": "",
        "categoria": "EST_-1,IPE_-1_-1_-1",
        "periodo": "1",
        "horaIni": "", "horaFim": "",
        "palavraChave": "",
        "ultimaDtRef": "false",
        "tipoEmpresa": "0",
        "token": "", "versaoCaptcha": "",
    }
    r = session.post(LISTAR_URL, json=body, headers={
        "Content-Type": "application/json; charset=UTF-8",
        "Referer": GET_URL.format(codigo),
        "X-Requested-With": "XMLHttpRequest",
    }, timeout=30)
    d = r.json().get("d", {})
    if d.get("temErro"):
        raise RuntimeError(f"CVM err: {d.get('msgErro')}")
    return _parse_records(d.get("dados", ""))


def load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return {"last_updated": None, "items": []}


def save_history(history: dict) -> None:
    HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def send_ntfy(title: str, body: str, click_url: str | None = None, tags: list[str] | None = None) -> None:
    payload = {"topic": NTFY_TOPIC, "title": title, "message": body}
    if click_url:
        payload["click"] = click_url
    if tags:
        payload["tags"] = tags
    try:
        requests.post("https://ntfy.sh", json=payload, timeout=10)
    except Exception as e:
        print(f"[ntfy] erro: {e}", file=sys.stderr)


def main():
    history = load_history()
    seen_protocolos = {item["protocolo"] for item in history["items"]}

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    new_items = []
    errors = 0
    for empresa, codigos in COMPANIES_CODIGOS.items():
        for codigo in codigos:
            try:
                items = scrape_codigo(codigo, session)
                for item in items:
                    if item["protocolo"] in seen_protocolos:
                        continue
                    item["empresa_label"] = empresa
                    item["codigo_cvm"] = codigo
                    item["first_seen"] = datetime.now().isoformat()
                    new_items.append(item)
                    seen_protocolos.add(item["protocolo"])
            except Exception as e:
                errors += 1
                print(f"  [{empresa}/{codigo}] erro: {e}", file=sys.stderr)
            time.sleep(0.3)  # polidez com CVM

    print(f"\n[summary] {len(new_items)} novos itens, {errors} erros", file=sys.stderr)

    if not new_items:
        # Atualiza só last_updated pra forçar diff e commit (mostra que checagem rodou)
        history["last_updated"] = datetime.now().isoformat()
        # Prune mesmo assim
        cutoff = (datetime.now() - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
        history["items"] = [i for i in history["items"] if i.get("first_seen", "") >= cutoff]
        save_history(history)
        return

    # Adiciona novos itens
    history["items"].extend(new_items)

    # Ordena por data de entrega desc
    history["items"].sort(key=lambda i: i.get("data_entrega", ""), reverse=True)

    # Prune itens mais antigos que retention
    cutoff = (datetime.now() - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
    history["items"] = [i for i in history["items"] if i.get("first_seen", "") >= cutoff]

    history["last_updated"] = datetime.now().isoformat()
    save_history(history)

    # === Push notifications pra categorias importantes ===
    # Carrega state dos alerts pra não duplicar com a Camada 1 (news)
    alerts_state = {"news": [], "dou": [], "cvm": []}
    if ALERTS_STATE_FILE.exists():
        alerts_state = json.loads(ALERTS_STATE_FILE.read_text(encoding="utf-8"))
    cvm_alerted = set(alerts_state.get("cvm", []))

    pushed = 0
    for item in new_items:
        if item["categoria"] not in ALERT_CATEGORIES:
            continue
        if item["protocolo"] in cvm_alerted:
            continue
        # Monta título
        empresa = item["empresa_label"]
        cat_emoji = "🚨" if item["categoria"] == "Fato Relevante" else "📋"
        title = f"{cat_emoji} {empresa} · {item['categoria']}"
        body = (item["assunto"] or item["tipo"] or "(sem assunto)")[:200]
        send_ntfy(title, body, click_url=item["link_pdf"], tags=["scroll"])
        cvm_alerted.add(item["protocolo"])
        pushed += 1

    if pushed:
        alerts_state["cvm"] = list(cvm_alerted)[-2000:]  # cap
        ALERTS_STATE_FILE.write_text(
            json.dumps(alerts_state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(f"[summary] {pushed} push notifications enviados", file=sys.stderr)


if __name__ == "__main__":
    main()
