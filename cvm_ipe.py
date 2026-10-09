"""Exporta o IPE anual da CVM (dados.cvm.gov.br) filtrado pras empresas de
utilities -> cvm_ipe.json, lido pela interface (aba CVM -> Documentos).

Antes a interface baixava os 2 ZIPs (~4 MB) e lia o CSV de TODAS as
companhias abertas a cada 6h (~2-3 s no primeiro acesso da aba). Aqui roda
1x/dia no workflow do CVM Insider; a interface só lê o JSON pronto (~0,3 MB
com gzip).

A lista de empresas (padrões de nome) veio de utilities-interface/lib/cvm.py
e agora mora SÓ aqui. É por nome, e não pelos códigos do COMPANIES_CODIGOS do
tempo real, de propósito: o nome pega as subsidiárias de cada grupo (as
distribuidoras da Energisa/Equatorial, as empresas CPFL...) — por código o
histórico perdia ~475 documentos.

    python cvm_ipe.py
"""
import io
import json
import re
import sys
import unicodedata
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

BRT = timezone(timedelta(hours=-3))
OUT_FILE = Path("cvm_ipe.json")
ZIP_URL = (
    "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS/"
    "ipe_cia_aberta_{year}.zip"
)
KEEP_DAYS = 400  # a interface filtra no máximo 365 dias
COLUMNS = [
    "Empresa", "Categoria", "Tipo", "Especie", "Assunto", "Data_Entrega",
    "Protocolo_Entrega", "Link_Download", "Nome_Companhia", "Codigo_CVM",
]

# Empresas alvo + padrões de match (substring case+accent-insensitive em Nome_Companhia).
# Patterns são tentados em ordem; primeiro match ganha — coloque o mais específico antes.
# Notas:
#   - Eletrobras foi renomeada pra AXIA ENERGIA após privatização.
#   - CTEEP virou ISA ENERGIA BRASIL após aquisição pela ISA.
COMPANIES = [
    {"label": "Eletrobras",   "patterns": ["axia energia", "eletrobras"]},
    {"label": "CTEEP / ISA",  "patterns": ["isa energia brasil", "cteep", "transmissao paulista"]},
    {"label": "Equatorial",   "patterns": ["equatorial"]},
    {"label": "Cemig",        "patterns": ["cemig", "cia energ minas gerais", "cia energetica de minas gerais"]},
    {"label": "Copel",        "patterns": ["copel", "companhia paranaense de energia", "cia paranaense de energia"]},
    {"label": "Light",        "patterns": ["light s.a", "light s/a", "light servicos"]},
    {"label": "Engie",        "patterns": ["engie brasil"]},
    {"label": "Neoenergia",   "patterns": ["neoenergia"]},
    {"label": "Taesa",        "patterns": ["transmissora alianca"]},
    {"label": "Energisa",     "patterns": ["energisa"]},
    {"label": "Auren",        "patterns": ["auren"]},  # ex-AES Brasil (adquirida pela Auren)
    {"label": "Eneva",        "patterns": ["eneva"]},
    {"label": "Alupar",       "patterns": ["alupar"]},
    {"label": "CPFL Energia", "patterns": ["cpfl"]},
    {"label": "Sabesp",       "patterns": ["saneamento basico estado", "sabesp"]},
    {"label": "Copasa",       "patterns": ["saneamento de minas gerais", "copasa"]},
    {"label": "Sanepar",      "patterns": ["sanepar"]},
    {"label": "Aegea",        "patterns": ["aegea"]},
]


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c)
    )


def label_companies(df: pd.DataFrame) -> pd.DataFrame:
    """Coluna Empresa pelo nome da companhia; descarta quem não é alvo."""
    nome_norm = df["Nome_Companhia"].fillna("").map(_strip_accents).str.lower()
    df = df.copy()
    df["Empresa"] = None
    for company in COMPANIES:
        for pattern in company["patterns"]:
            mask = nome_norm.str.contains(re.escape(pattern), na=False, regex=True)
            df.loc[mask & df["Empresa"].isna(), "Empresa"] = company["label"]
    return df[df["Empresa"].notna()].copy()


def fetch_year(year: int) -> pd.DataFrame:
    r = requests.get(ZIP_URL.format(year=year), timeout=90)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        csv_name = next(n for n in z.namelist() if n.endswith(".csv"))
        with z.open(csv_name) as f:
            return pd.read_csv(f, sep=";", encoding="latin-1", dtype=str)


def main() -> int:
    year = datetime.now(BRT).year
    frames, years_ok = [], []
    for y in (year - 1, year):
        try:
            frames.append(fetch_year(y))
            years_ok.append(y)
        except Exception as e:  # ano corrente pode ainda não existir em janeiro
            print(f"[cvm_ipe] aviso: IPE {y} indisponível: {e}", file=sys.stderr)
    if not frames:
        print("[cvm_ipe] nenhum ano baixado — mantendo o JSON anterior", file=sys.stderr)
        return 1

    df = label_companies(pd.concat(frames, ignore_index=True))
    df["Data_Entrega"] = pd.to_datetime(df["Data_Entrega"], errors="coerce")
    cutoff = pd.Timestamp(datetime.now(BRT).replace(tzinfo=None)) - pd.Timedelta(days=KEEP_DAYS)
    df = df[df["Data_Entrega"] >= cutoff]
    df = df.sort_values("Data_Entrega", ascending=False)
    df["Data_Entrega"] = df["Data_Entrega"].dt.strftime("%Y-%m-%d")
    df = df[[c for c in COLUMNS if c in df.columns]].fillna("")

    out = {
        "last_updated": datetime.now(BRT).isoformat(),
        "source": "dados.cvm.gov.br IPE (anual)",
        "years": years_ok,
        "keep_days": KEEP_DAYS,
        "items": df.to_dict("records"),
    }
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    by_company = df["Empresa"].value_counts().to_dict()
    missing = sorted({c["label"] for c in COMPANIES} - set(by_company))
    print(
        f"[cvm_ipe] {len(df)} documentos de {len(by_company)} empresas "
        f"(últimos {KEEP_DAYS}d) -> {OUT_FILE}"
        + (f" | sem documentos: {', '.join(missing)}" if missing else ""),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
