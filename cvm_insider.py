"""CVM Insider Trading scraper — agrega Compras/Vendas de Ações por mês × empresa.

Baixa o dataset VLMO oficial (Valores Mobiliários Negociados pelos Administradores)
da CVM em `dados.cvm.gov.br` — 3 anos rolling (ano atual + 2 anteriores) — e gera
resumo mensal de transações dos administradores (Conselho/Diretoria/Controlador/
Fiscal) das empresas que rastreamos.

Filtros aplicados:
  - Tipo_Ativo = "Ações" (ON+PN consolidados; exclui debêntures/units/bônus)
  - Tipo_Movimentacao IN (Compra, Compra à vista, Compra à termo,
                          Venda, Venda à vista, Venda à termo)
  - Dedupe: mantém versão MAX por (CNPJ, Data_Referencia) — companhias podem
    re-submeter o documento, queremos sempre a última versão.

Persiste em cvm_insider_history.json com schema:
  {
    "last_updated": "...",
    "years_covered": [2024, 2025, 2026],
    "n_companies": 18,
    "by_company": {
      "Eletrobras": {
        "2024-01": {
          "buy_brl": 0, "sell_brl": 0, "net_brl": 0,
          "buy_qty": 0, "sell_qty": 0,
          "n_trades_buy": 0, "n_trades_sell": 0,
          "by_cargo": {"Conselho de Administração ou Vinculado": {...}, ...}
        },
        ...
      },
      ...
    }
  }

Roda 1x/dia via workflow cvm-insider.yml (CVM atualiza VLMO semanalmente).
"""
import io
import json
import os
import sys
import zipfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

# UTF-8 no stderr pra Windows console
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HISTORY_FILE = Path("cvm_insider_history.json")
DIAGNOSTIC_FILE = Path("cvm_insider_diagnostic.json")

VLMO_URL = (
    "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/VLMO/DADOS/"
    "vlmo_cia_aberta_{year}.zip"
)

# Mesmo mapeamento de cvm_realtime.py — primeira posição é a holding/principal,
# demais são subsidiárias listadas (podem ter VLMO próprio).
COMPANIES_CODIGOS = {
    "Eletrobras":    ["2437", "27472", "3328"],
    "CTEEP / ISA":   ["18376"],
    "Equatorial":    ["20010", "18309", "25577"],
    "Cemig":         ["2453", "20303", "20320"],
    "Copel":         ["14311", "26808", "24740"],
    "Light":         ["19879", "8036"],
    "Engie":         ["17329"],
    "Neoenergia":    ["15539"],
    "Taesa":         ["20257"],
    "Energisa":      ["15253", "14605", "5576"],
    "Auren":         ["26620", "26174", "25640"],
    "Eneva":         ["21237"],
    "Alupar":        ["21490"],
    "CPFL Energia":  ["18660", "3824", "3204"],
    "Sabesp":        ["14443"],
    "Copasa":        ["19445"],
    "Sanepar":       ["18627"],
    "Aegea":         ["23396"],
}

BUY_TYPES = {"Compra", "Compra à vista", "Compra à termo"}
SELL_TYPES = {"Venda", "Venda à vista", "Venda à termo"}
ALLOWED_ATIVO = {"Ações"}  # exclui debêntures, units, bônus, etc

YEARS_ROLLING = 3  # ano atual + 2 anteriores


def fetch_vlmo_year(year: int):
    """Baixa o ZIP do ano e retorna (meta_bytes, con_bytes) dos 2 CSVs internos."""
    url = VLMO_URL.format(year=year)
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        meta_name = next(
            n for n in z.namelist() if "_con_" not in n and n.endswith(".csv")
        )
        con_name = next(n for n in z.namelist() if "_con_" in n)
        with z.open(meta_name) as f:
            meta_bytes = f.read()
        with z.open(con_name) as f:
            con_bytes = f.read()
    return meta_bytes, con_bytes


def df_from_bytes(b: bytes) -> pd.DataFrame:
    return pd.read_csv(io.BytesIO(b), sep=";", encoding="latin-1", dtype=str)


def build_cdcvm_to_cnpj(meta_df: pd.DataFrame) -> dict:
    """Constrói lookup {Codigo_CVM: CNPJ_Companhia} a partir do meta CSV."""
    lookup = {}
    for _, row in meta_df.iterrows():
        cd = (row.get("Codigo_CVM") or "").strip()
        cnpj = (row.get("CNPJ_Companhia") or "").strip()
        if cd and cnpj and cd not in lookup:
            lookup[cd] = cnpj
    return lookup


def aggregate_year(con_df: pd.DataFrame, cnpj_to_label: dict) -> dict:
    """Filtra + agrega o CSV de detalhes para o esquema do JSON.

    Retorna dict: {label: {YYYY-MM: bucket_dict}}.
    """
    df = con_df[con_df["CNPJ_Companhia"].isin(cnpj_to_label.keys())].copy()
    if df.empty:
        return {}

    # Dedupe por (CNPJ, Data_Referencia) → mantém só linhas com max(Versao)
    df["Versao_int"] = pd.to_numeric(df["Versao"], errors="coerce").fillna(0).astype(int)
    max_v = df.groupby(["CNPJ_Companhia", "Data_Referencia"])["Versao_int"].transform("max")
    df = df[df["Versao_int"] == max_v]

    # Filtra Tipo_Ativo permitido
    df = df[df["Tipo_Ativo"].isin(ALLOWED_ATIVO)]
    if df.empty:
        return {}

    # Classify side
    df["side"] = df["Tipo_Movimentacao"].map(
        lambda x: "buy" if x in BUY_TYPES else ("sell" if x in SELL_TYPES else None)
    )
    df = df[df["side"].notna()]
    if df.empty:
        return {}

    # Converte volume e quantidade
    df["Volume_num"] = (
        pd.to_numeric(df["Volume"].astype(str).str.replace(",", "."), errors="coerce")
        .fillna(0.0)
    )
    df["Qtd_num"] = (
        pd.to_numeric(df["Quantidade"].astype(str).str.replace(",", "."), errors="coerce")
        .fillna(0.0)
        .astype(int)
    )

    # year-month a partir de Data_Referencia (formato "YYYY-MM-DD")
    df["YM"] = df["Data_Referencia"].astype(str).str[:7]

    # Mapeia label
    df["label"] = df["CNPJ_Companhia"].map(cnpj_to_label)

    out = defaultdict(dict)
    for (label, ym), group in df.groupby(["label", "YM"]):
        if not isinstance(ym, str) or len(ym) != 7:
            continue
        buys = group[group["side"] == "buy"]
        sells = group[group["side"] == "sell"]
        buy_brl = float(buys["Volume_num"].sum())
        sell_brl = float(sells["Volume_num"].sum())
        bucket = {
            "buy_brl": round(buy_brl, 2),
            "sell_brl": round(sell_brl, 2),
            "net_brl": round(buy_brl - sell_brl, 2),
            "buy_qty": int(buys["Qtd_num"].sum()),
            "sell_qty": int(sells["Qtd_num"].sum()),
            "n_trades_buy": int(len(buys)),
            "n_trades_sell": int(len(sells)),
            "by_cargo": {},
        }
        for cargo, sub in group.groupby("Tipo_Cargo"):
            sub_buy = float(sub[sub["side"] == "buy"]["Volume_num"].sum())
            sub_sell = float(sub[sub["side"] == "sell"]["Volume_num"].sum())
            bucket["by_cargo"][str(cargo)] = {
                "buy_brl": round(sub_buy, 2),
                "sell_brl": round(sub_sell, 2),
                "net_brl": round(sub_buy - sub_sell, 2),
            }
        out[label][ym] = bucket
    return dict(out)


def main():
    current_year = datetime.now().year
    years = list(range(current_year - YEARS_ROLLING + 1, current_year + 1))
    print(f"[cvm_insider] Baixando VLMO pros anos {years}", file=sys.stderr)

    cdcvm_to_cnpj = {}
    year_data = []  # list of (year, con_df)

    for y in years:
        try:
            meta_bytes, con_bytes = fetch_vlmo_year(y)
            meta_df = df_from_bytes(meta_bytes)
            con_df = df_from_bytes(con_bytes)
            cdcvm_to_cnpj.update(build_cdcvm_to_cnpj(meta_df))
            year_data.append((y, con_df))
            print(f"  ano {y}: meta={len(meta_df)} rows, con={len(con_df)} rows", file=sys.stderr)
        except Exception as e:
            print(f"  ano {y}: ERRO {e}", file=sys.stderr)

    if not year_data:
        print("[cvm_insider] Nenhum dado coletado — abortando", file=sys.stderr)
        return

    # Resolve nossas CD_CVM → CNPJ → label
    cnpj_to_label = {}
    missing = []
    for label, codes in COMPANIES_CODIGOS.items():
        found_any = False
        for code in codes:
            cnpj = cdcvm_to_cnpj.get(code)
            if cnpj:
                # Em caso de duplicação CNPJ entre labels, primeira label ganha
                cnpj_to_label.setdefault(cnpj, label)
                found_any = True
        if not found_any:
            missing.append((label, codes))

    print(
        f"[cvm_insider] {len(cnpj_to_label)} CNPJ mapeados pra {len(COMPANIES_CODIGOS)} labels"
        f" ({len(missing)} labels sem nenhum CNPJ)",
        file=sys.stderr,
    )
    for label, codes in missing:
        print(f"  ⚠️ {label}: CD_CVM {codes} sem CNPJ no VLMO", file=sys.stderr)

    # Agrega anos (ano mais recente sobrescreve, em caso de overlap)
    aggregated = defaultdict(dict)
    for y, con_df in year_data:
        year_agg = aggregate_year(con_df, cnpj_to_label)
        for label, months in year_agg.items():
            for ym, data in months.items():
                aggregated[label][ym] = data

    total_buckets = sum(len(m) for m in aggregated.values())
    print(
        f"[cvm_insider] DONE: {len(aggregated)} empresas, {total_buckets} buckets (empresa×mês)",
        file=sys.stderr,
    )

    # Salva
    output = {
        "last_updated": datetime.now().isoformat(),
        "years_covered": years,
        "n_companies": len(aggregated),
        "by_company": dict(aggregated),
    }
    HISTORY_FILE.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    DIAGNOSTIC_FILE.write_text(json.dumps({
        "last_run": datetime.now().isoformat(),
        "years": years,
        "companies_with_data": len(aggregated),
        "total_buckets": total_buckets,
        "missing_codes": [{"label": l, "codes": c} for l, c in missing],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
