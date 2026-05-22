"""
Sistema de alertas. Roda a cada 30min via GitHub Actions, checa news/DOU/CVM
contra config (alerts_config.py), e envia push notification via ntfy.sh
pra cada NOVO item que matcha.

State commitado em alerts_state.json (próximo run não realerta o mesmo item).
"""
# digest.py lê env vars no top — define dummies antes de importar
import os
os.environ.setdefault("RESEND_API_KEY", "_unused_by_alerts")
os.environ.setdefault("DIGEST_TO", "_unused_by_alerts")

import io
import json
import re
import sys
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

from alerts_config import CONFIG
from digest import fetch_source as fetch_news
from dou_mme import collect as dou_collect
from dou_mme import link_for as dou_link
from sources import SOURCES as NEWS_SOURCES

STATE_FILE = Path("alerts_state.json")
NTFY_BASE = "https://ntfy.sh"

# Quantos IDs por categoria manter em state (evita arquivo crescer infinito)
STATE_CAP = 2000
# Limite de pushes por run (evita inundar o celular se config muda muito)
PUSH_CAP_PER_RUN = 25


# ============== STATE ==============
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"news": [], "dou": [], "cvm": []}


def save_state(state: dict) -> None:
    # Cap cada categoria nos últimos N (mantém os mais recentes)
    for key in state:
        state[key] = state[key][-STATE_CAP:]
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ============== NTFY ==============
def send_ntfy(topic: str, title: str, body: str, click_url: str | None = None,
              tags: list[str] | None = None) -> bool:
    """Usa o publish endpoint em JSON pra suportar UTF-8 (emojis, acentos) no título."""
    payload: dict = {
        "topic": topic,
        "title": title,
        "message": body,
    }
    if click_url:
        payload["click"] = click_url
    if tags:
        payload["tags"] = tags
    try:
        r = requests.post(NTFY_BASE, json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[ntfy] erro: {e}", file=sys.stderr)
        return False


# ============== MATCHING ==============
def text_matches(text: str, keywords: list) -> bool:
    """
    Match se QUALQUER keyword bater. Cada keyword pode ser:
      - str: match por substring (case-insensitive)
      - list[str]: TODOS os substrings devem aparecer (AND lógico)

    Ex.: ["LRCAP", ["fato relevante", "Eletrobras"]] dispara em:
      - qualquer texto contendo "LRCAP"
      - OU texto contendo AMBOS "fato relevante" e "Eletrobras"
    """
    if not text or not keywords:
        return False
    text_lower = text.lower()
    for k in keywords:
        if isinstance(k, str):
            if k.lower() in text_lower:
                return True
        elif isinstance(k, (list, tuple)):
            if all(kw.lower() in text_lower for kw in k):
                return True
    return False


# ============== NEWS ==============
def alert_news(config: dict, state: dict, dry: bool) -> int:
    keywords = config.get("news_keywords", [])
    if not keywords:
        return 0

    new_matches = []
    for src in NEWS_SOURCES:
        try:
            items = fetch_news(src)
        except Exception as e:
            print(f"[news/{src['name']}] erro: {e}", file=sys.stderr)
            continue
        for item in items:
            link = item["link"]
            if link in state["news"]:
                continue
            state["news"].append(link)  # marca como visto (independente de match)
            if text_matches(item["title"], keywords):
                new_matches.append((src["name"], item))

    if not dry:
        for src_name, item in new_matches[:PUSH_CAP_PER_RUN]:
            send_ntfy(
                config["ntfy_topic"],
                f"📰 {src_name}",
                item["title"],
                click_url=item["link"],
                tags=["newspaper"],
            )

    print(f"[news] {len(new_matches)} alertas novos")
    return len(new_matches)


# ============== DOU ==============
def alert_dou(config: dict, state: dict, dry: bool) -> int:
    keywords = config.get("dou_keywords", [])
    if not keywords:
        return 0

    # Checa DOU de hoje. Em fim de semana / feriado pode estar vazio.
    today = datetime.now().strftime("%d-%m-%Y")
    try:
        hits = dou_collect(today, with_summary=False)
    except Exception as e:
        print(f"[dou] erro: {e}", file=sys.stderr)
        return 0

    new_matches = []
    for hit in hits:
        hit_id = hit.get("urlTitle") or hit.get("id") or hit.get("href", "")
        if not hit_id or hit_id in state["dou"]:
            continue
        state["dou"].append(hit_id)
        text = f"{hit.get('title', '')} {hit.get('content', '')}"
        if text_matches(text, keywords):
            new_matches.append(hit)

    if not dry:
        for hit in new_matches[:PUSH_CAP_PER_RUN]:
            send_ntfy(
                config["ntfy_topic"],
                "🏛️ DOU",
                hit.get("title", "(sem título)"),
                click_url=dou_link(hit),
                tags=["classical_building"],
            )

    print(f"[dou] {len(new_matches)} alertas novos")
    return len(new_matches)


# ============== CVM ==============
_CVM_ZIP_URL = (
    "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/"
    "ipe_cia_aberta_{year}.zip"
)

# Espelho da config em utilities-interface/lib/cvm.py — mantenha sincronizado.
_CVM_COMPANIES = [
    {"label": "Eletrobras",   "patterns": ["axia energia", "eletrobras"]},
    {"label": "CTEEP / ISA",  "patterns": ["isa energia brasil", "cteep", "transmissao paulista"]},
    {"label": "Equatorial",   "patterns": ["equatorial"]},
    {"label": "Cemig",        "patterns": ["cemig", "cia energ minas gerais", "cia energetica de minas gerais"]},
    {"label": "Copel",        "patterns": ["copel", "companhia paranaense de energia", "cia paranaense de energia"]},
    {"label": "Light",        "patterns": ["light s.a", "light s/a", "light servicos"]},
    {"label": "Engie",        "patterns": ["engie brasil"]},
    {"label": "EDP",          "patterns": ["edp - energias", "energias do brasil"]},
    {"label": "Neoenergia",   "patterns": ["neoenergia"]},
    {"label": "Taesa",        "patterns": ["transmissora alianca"]},
    {"label": "Energisa",     "patterns": ["energisa"]},
    {"label": "Auren",        "patterns": ["auren"]},
    {"label": "CPFL Energia", "patterns": ["cpfl"]},
    {"label": "Sabesp",       "patterns": ["saneamento basico estado", "sabesp"]},
    {"label": "Copasa",       "patterns": ["saneamento de minas gerais", "copasa"]},
    {"label": "Sanepar",      "patterns": ["sanepar"]},
]


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))


def _fetch_cvm_year(year: int) -> pd.DataFrame:
    url = _CVM_ZIP_URL.format(year=year)
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    csv_name = next(n for n in z.namelist() if n.endswith(".csv"))
    with z.open(csv_name) as f:
        df = pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)
    return df


def _label_cvm(df: pd.DataFrame) -> pd.DataFrame:
    nome_norm = df["Nome_Companhia"].fillna("").map(_strip_accents).str.lower()
    df = df.copy()
    df["Empresa"] = None
    for c in _CVM_COMPANIES:
        for p in c["patterns"]:
            mask = nome_norm.str.contains(re.escape(p), na=False, regex=True)
            df.loc[mask & df["Empresa"].isna(), "Empresa"] = c["label"]
    return df[df["Empresa"].notna()].copy()


def alert_cvm(config: dict, state: dict, dry: bool) -> int:
    cfg = config.get("cvm", {})
    target_empresas = set(cfg.get("empresas", []))
    target_categorias = set(cfg.get("categorias", []))
    if not target_empresas or not target_categorias:
        return 0

    year = datetime.now().year
    try:
        df = _fetch_cvm_year(year)
    except Exception as e:
        print(f"[cvm] erro: {e}", file=sys.stderr)
        return 0

    df = _label_cvm(df)
    df = df[df["Empresa"].isin(target_empresas)]
    df = df[df["Categoria"].isin(target_categorias)]

    new_matches = []
    for _, row in df.iterrows():
        protocol = str(row.get("Protocolo_Entrega", "")).strip()
        if not protocol or protocol in state["cvm"]:
            continue
        state["cvm"].append(protocol)
        new_matches.append(row)

    if not dry:
        # Ordena por data crescente: notificação mais recente é a última (no topo da lista)
        new_matches.sort(key=lambda r: r.get("Data_Entrega", ""))
        for row in new_matches[:PUSH_CAP_PER_RUN]:
            empresa = row["Empresa"]
            categoria = row.get("Categoria", "?")
            tipo = row.get("Tipo") or ""
            assunto = row.get("Assunto") or tipo or "(sem assunto)"
            link = row.get("Link_Download", "")
            send_ntfy(
                config["ntfy_topic"],
                f"📋 {empresa} · {categoria}",
                assunto[:200],
                click_url=link,
                tags=["scroll"],
            )

    print(f"[cvm] {len(new_matches)} alertas novos")
    return len(new_matches)


# ============== MAIN ==============
def main():
    state = load_state()
    first_run = all(len(state[k]) == 0 for k in ("news", "dou", "cvm"))

    if first_run:
        print("=== Primeiro run: populando state SEM enviar alertas ===")
        print("(senão você receberia spam de todo histórico que matcha)")
        dry = True
    else:
        dry = False

    total = 0
    total += alert_news(CONFIG, state, dry=dry)
    total += alert_dou(CONFIG, state, dry=dry)
    total += alert_cvm(CONFIG, state, dry=dry)

    save_state(state)

    if first_run:
        print(f"State populado com {sum(len(state[k]) for k in state)} IDs. Alertas começam a valer no próximo run.")
        # Notifica que o sistema está ativo
        send_ntfy(
            CONFIG["ntfy_topic"],
            "✅ Alertas Utilities ativos",
            f"Sistema configurado. Você vai receber push a cada match novo (check a cada 30min).",
            tags=["white_check_mark"],
        )
    else:
        print(f"Total: {total} push notifications enviados")


if __name__ == "__main__":
    main()
