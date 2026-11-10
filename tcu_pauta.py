"""Pauta de julgamento do TCU para os processos do projetos_watch.txt.

O TCU publica nos dados abertos (webservice "Pautas das Sessões",
dados-abertos.apps.tcu.gov.br/api/pautassessao) TODAS as sessões de TODOS os
processos num arquivo só: dezenas de MB, sem filtro, com limite de acessos
(HTTP 429). Por isso este script roda à parte (workflow tcu-pauta.yml, poucas
vezes por dia), guarda só os processos acompanhados em tcu_pauta.json e o
regulatorio.py lê esse arquivo.

A consulta processual do TCU (peças, andamentos) bloqueia robôs (firewall de
aplicação): fica de fora DE PROPÓSITO. Para isso existe o Push do TCU (e-mail).

Uso: python tcu_pauta.py   (sem processo TCU no projetos_watch.txt, não baixa nada)
"""
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API = "https://dados-abertos.apps.tcu.gov.br/api/pautassessao"
WATCH = Path("projetos_watch.txt")
SAIDA = Path("tcu_pauta.json")
BRT = timezone(timedelta(hours=-3))


def processos_acompanhados() -> list:
    """TCs do projetos_watch.txt, no formato do TCU (000.000/0000-0)."""
    try:
        linhas = WATCH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for linha in linhas:
        partes = [x.strip() for x in linha.split("|")]
        if len(partes) >= 2 and partes[0].upper() == "TCU" and not linha.lstrip().startswith("#"):
            d = re.sub(r"\D", "", partes[1])
            if len(d) == 11:
                out.append(f"{d[:3]}.{d[3:6]}/{d[6:10]}-{d[10]}")
    return out


def baixar(tentativas: int = 3) -> list:
    erro = None
    for i in range(tentativas):
        try:
            r = requests.get(API, timeout=(30, 600), headers={"Accept-Encoding": "gzip"})
            if r.status_code == 429:
                raise RuntimeError("HTTP 429 (limite de acessos do TCU)")
            r.raise_for_status()
            return r.json()
        except Exception as e:  # conexão cortada, 429, JSON truncado
            erro = e
            print(f"[tcu_pauta] tentativa {i + 1}: {e}", file=sys.stderr)
            time.sleep(90 * (i + 1))
    raise RuntimeError(f"pauta do TCU sem leitura: {erro}")


def main():
    tcs = processos_acompanhados()
    if not tcs:
        print("[tcu_pauta] nenhum processo TCU no projetos_watch.txt")
        return
    try:
        dados = baixar()
    except Exception as e:
        # Mantém o arquivo anterior; o site avisa quando a leitura passa de 48h.
        print(f"::warning::{e}")
        return
    iso = lambda s: datetime.strptime(s, "%d/%m/%Y").strftime("%Y-%m-%d")
    alvo = set(tcs)
    por_tc = {tc: [] for tc in tcs}
    data_max = ""
    for x in dados:
        try:
            dia = iso(x.get("dataSessao") or "")
        except ValueError:
            continue
        data_max = max(data_max, dia)
        if x.get("numeroProcesso") in alvo:
            por_tc[x["numeroProcesso"]].append({
                "data": dia, "colegiado": x.get("nomeColegiado"), "relator": (x.get("nomeRelator") or "").title() or None,
                "tipo": (x.get("tipoProcesso") or "").capitalize() or None, "natureza": x.get("naturezaProcesso")})
    for tc in por_tc:
        por_tc[tc].sort(key=lambda s: s["data"], reverse=True)
    saida = {"lido_em": datetime.now(BRT).isoformat(timespec="seconds"), "registros": len(dados),
             "sessao_mais_recente": data_max, "processos": por_tc}
    SAIDA.write_text(json.dumps(saida, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[tcu_pauta] {len(dados)} registros (até {data_max}); "
          + ", ".join(f"{tc}: {len(v)} sessões" for tc, v in por_tc.items()))


if __name__ == "__main__":
    main()
