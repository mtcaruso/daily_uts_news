"""
Digest diário de PLs (Câmara + Senado) e da ANEEL: propostas de abertura de CP/TS
na pauta da Diretoria (prévia) + consultas abertas com o prazo de contribuição.

Filtra por palavras-chave de utilities (energia, saneamento, tarifa, renováveis)
e mantém estado em state/pls_consultas.json pra mostrar só novidades (a lista de
consultas abertas sai inteira todo dia).

Modos:
    python pls_consultas.py                # entrega novidades por email
    python pls_consultas.py --dry-run      # imprime no stdout
    python pls_consultas.py --bootstrap    # marca tudo como visto, sem email
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (utilities-news/pls-consultas)"}
STATE_PATH = Path(__file__).parent / "state" / "pls_consultas.json"

# Palavras-chave (regex, case-insensitive). Aplicadas sobre ementa/título.
KEYWORDS = re.compile(
    r"\b("
    r"energia\s+elétrica|setor\s+el[ée]trico|aneel|"
    # ANA só em maiúsculas: sem isso, "Lei Ana Clara" entrava como saneamento.
    r"saneamento|marco\s+do\s+saneamento|(?-i:ANA)|"
    r"tarifa(s|ria|s)?|subs[íi]dio|\bcde\b|encargo\s+setorial|"
    r"renov[áa]vel|gera[çc][ãa]o\s+distribu[íi]da|\bgd\b|hidrog[êe]nio|"
    r"transi[çc][ãa]o\s+energ[ée]tica|e[óo]lica|solar\s+fotovoltaica"
    r")\b",
    re.IGNORECASE,
)

# Temas da Câmara: 54=Energia/Hídricos/Minerais, 48=Meio Ambiente, 41=Cidades.
CAMARA_THEMES = [54, 48, 41]
CAMARA_TIPOS = {"PL", "PLP", "PEC", "PDL", "MPV"}
SENADO_SIGLAS = ("PL", "PLP", "PEC", "PDL", "PLN", "MPV")
# 1ª run depois do conserto de 21/10/2026: recupera o período em que a busca do
# Senado esteve fora (o state parou em 26/05/2026).
SENADO_INICIO_PADRAO = "2026-05-26"

CAMARA_API = "https://dadosabertos.camara.leg.br/api/v2"
SENADO_API = "https://legis.senado.leg.br/dadosabertos"
ANEEL_CKAN = "https://dadosabertos.aneel.gov.br/api/3/action"
# Dataset "Pautas e Atas das Reuniões Públicas da Diretoria" (semanal; a pauta sai
# ~4 dias antes da reunião). O site da ANEEL bloqueia o Actions; os dados abertos, não.
ANEEL_PAUTA_RESOURCE = "43386a8b-4781-44ec-a082-fa7fdfe33186"
ANEEL_PAUTA_URL = "https://www2.aneel.gov.br/aplicacoes_liferay/noticias_area/?idAreaNoticia=425"
ANEEL_LISTAGEM = {"cp": "https://antigo.aneel.gov.br/consultas-publicas",
                  "ts": "https://antigo.aneel.gov.br/tomadas-de-subsidios"}
# Prazos e objetos que os alertas já coletam (commitados no repo pelo Actions e pelo PC).
ALERTS_STATE = Path(__file__).parent / "alerts_state.json"
ANEEL_HISTORY = Path(__file__).parent / "aneel_aux_history.json"
FECHA_EM_DIAS = 7  # "fecham em até 7 dias" no topo da lista de consultas abertas


def load_state():
    if not STATE_PATH.exists():
        return {"camara": [], "senado": [], "aneel_pauta": []}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"camara": [], "senado": [], "aneel_pauta": []}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Mantém só os últimos N IDs por fonte pra arquivo não crescer indefinidamente.
    # (Só listas: senado_ok_ate é uma data.)
    trimmed = {k: (list(v)[-2000:] if isinstance(v, list) else v) for k, v in state.items()}
    STATE_PATH.write_text(
        json.dumps(trimmed, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


# ───────────────────────────── Câmara ─────────────────────────────


def fetch_camara_since(date_from):
    """Proposições apresentadas em ou após date_from, nos temas relevantes."""
    seen = {}
    for tema in CAMARA_THEMES:
        page = 1
        while True:
            params = {
                "codTema": tema,
                "dataApresentacaoInicio": date_from,
                "itens": 100,
                "pagina": page,
                "ordem": "DESC",
                "ordenarPor": "id",
            }
            r = requests.get(
                f"{CAMARA_API}/proposicoes", params=params, headers=HEADERS, timeout=30
            )
            r.raise_for_status()
            data = r.json().get("dados", [])
            if not data:
                break
            for p in data:
                seen.setdefault(p["id"], p)
            if len(data) < 100:
                break
            page += 1
    return list(seen.values())


def _camara_ementa(prop_id):
    try:
        r = requests.get(
            f"{CAMARA_API}/proposicoes/{prop_id}", headers=HEADERS, timeout=20
        )
        r.raise_for_status()
        return r.json().get("dados", {}).get("ementa", "")
    except requests.RequestException:
        return ""


def collect_camara(state, days_back):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime(
        "%Y-%m-%d"
    )
    candidates = fetch_camara_since(cutoff)
    seen_ids = set(state.get("camara", []))
    novos = []
    for p in candidates:
        if p["id"] in seen_ids:
            continue
        if p.get("siglaTipo") not in CAMARA_TIPOS:
            continue
        # /proposicoes lista não traz ementa — busca individual só pros candidatos.
        ementa = _camara_ementa(p["id"])
        if not KEYWORDS.search(ementa):
            continue
        novos.append(
            {
                "id": p["id"],
                "label": f"{p['siglaTipo']} {p['numero']}/{p['ano']}",
                "ementa": ementa,
                "url": f"https://www.camara.leg.br/propostas-legislativas/{p['id']}",
            }
        )
    return novos


# ───────────────────────────── Senado ─────────────────────────────


def collect_senado(state, hoje):
    """Projetos/medidas apresentados no Senado desde a última leitura OK, com as
    KEYWORDS na ementa (o mesmo filtro da Câmara).

    A busca por palavra-chave do Senado (/materia/pesquisa/lista.json e
    /processo?termo=) responde 503 "Recurso de dados indisponível" depois de ~40s:
    o digest ficou sem nenhum PL do Senado de 26/05 a 21/10/2026, calado. A
    listagem por período de apresentação (/processo) responde em <1s. A janela
    começa em state['senado_ok_ate'] (2 dias de folga), então um período sem
    leitura é recuperado na volta."""
    seen_ids = set(state.get("senado", []))
    desde = (datetime.strptime(state.get("senado_ok_ate") or SENADO_INICIO_PADRAO, "%Y-%m-%d")
             - timedelta(days=2)).strftime("%Y-%m-%d")
    novos = {}
    try:
        for sigla in SENADO_SIGLAS:
            # O /processo também dá 503 de vez em quando (medido 21/10/2026: 1 em
            # ~15, e a 1ª consulta do dia leva ~8s): 3 tentativas antes de desistir.
            for tentativa in range(3):
                r = requests.get(
                    f"{SENADO_API}/processo",
                    params={"sigla": sigla, "dataInicioApresentacao": desde, "dataFimApresentacao": hoje},
                    headers={**HEADERS, "Accept": "application/json"},
                    timeout=60,
                )
                if r.status_code != 503 or tentativa == 2:
                    break
                time.sleep(10)
            r.raise_for_status()
            for m in r.json():
                codigo = str(m.get("codigoMateria") or "")
                ementa = (m.get("ementa") or "").strip()
                if not codigo or codigo in seen_ids or codigo in novos or not KEYWORDS.search(ementa):
                    continue
                novos[codigo] = {
                    "id": codigo,
                    "label": m.get("identificacao") or sigla,
                    "ementa": ementa,
                    "url": f"https://www25.senado.leg.br/web/atividade/materias/-/materia/{codigo}",
                }
    except (requests.RequestException, ValueError) as e:
        # Janela não avança: a próxima run tenta o mesmo período de novo.
        print(f"  senado falhou ({desde} a {hoje}): {e}", file=sys.stderr)
        print(f"::warning::Senado: {str(e)[:150]}")
        return list(novos.values())
    state["senado_ok_ate"] = hoje
    return list(novos.values())


# ───────────────────────────── ANEEL ─────────────────────────────
# Até 21/10/2026 esta seção lia o dataset "audiencias-e-consultas-publicas", que
# só tem contagens por ano: nunca mostrou nenhuma CP (0 itens desde 20/05/2026).

_PAUTA_ABERTURA = re.compile(
    r"abertura\s+de\s+(?:consulta\s+p[úu]blica|tomada\s+de\s+subs[íi]dios)", re.IGNORECASE)
_PARTICULAS = {"de", "da", "do", "dos", "das", "e"}


def _nome(s):
    """'GENTIL NOGUEIRA DE SA JUNIOR' → 'Gentil Nogueira de Sa Junior'."""
    return " ".join(w if w in _PARTICULAS else w.capitalize() for w in (s or "").lower().split())


def _json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def collect_aneel_pauta(state, hoje):
    """Propostas de abertura de CP/TS na pauta das reuniões da Diretoria, de hoje
    em diante (a pauta sai ~4 dias antes: a CP 039/2026 estava na de 16/10 para a
    reunião de 20/10). Proposta de reunião que já passou não é mais prévia: a
    abertura em si chega pelo Telegram. Retorna (novos, ids vistos)."""
    r = requests.get(
        f"{ANEEL_CKAN}/datastore_search",
        params={"resource_id": ANEEL_PAUTA_RESOURCE, "limit": 300, "sort": "DatReuniao desc"},
        headers=HEADERS,
        timeout=60,
    )
    r.raise_for_status()
    seen = set(state.get("aneel_pauta", []))
    novos, vistos = {}, []
    for rec in r.json()["result"]["records"]:
        assunto = " ".join((rec.get("TxtAssunto") or "").split())
        if not _PAUTA_ABERTURA.search(assunto):
            continue
        dia = (rec.get("DatReuniao") or "")[:10]
        uid = f"{rec.get('IdeReuniao')}|{rec.get('NumProcesso')}|{assunto[:60]}"
        vistos.append(uid)
        if uid in seen or uid in novos or dia < hoje:
            continue
        procs = [f"{n[:5]}.{n[5:11]}/{n[11:15]}-{n[15:]}" for n in re.findall(r"\d{17}", rec.get("NumProcesso") or "")]
        novos[uid] = {
            "id": uid,
            "label": f"{rec.get('IdeReuniao')} · reunião de {dia[8:10]}/{dia[5:7]}",
            "assunto": assunto,
            "meta": " · ".join(filter(None, [f"relator(a): {_nome(rec.get('NomDiretorRelator'))}" if rec.get("NomDiretorRelator") else "",
                                              f"processo {', '.join(procs)}" if procs else ""])),
            "url": ANEEL_PAUTA_URL,
        }
    return sorted(novos.values(), key=lambda c: c["label"]), vistos


def collect_aneel_prazos(hoje):
    """CP/TS da ANEEL ainda abertas, com o prazo de contribuição, do que os alertas
    já coletam: prazo do aviso no DOU e da listagem (alerts_state.json, gravado pelo
    Actions) e da listagem do PC (aneel_aux_history.json); objeto da listagem ou do
    aviso no DOU (alerts_state['aneel_objetos']). Prazo divergente = o maior
    (prorrogação só aumenta)."""
    alerts = _json(ALERTS_STATE)
    partic = alerts.get("aneel_partic") or {}
    hist = _json(ANEEL_HISTORY).get("items") or {}
    objetos = alerts.get("aneel_objetos") or {}
    lidos = {k: e.get("deadline") for k, e in hist.items()
             if k.startswith(("partic_cp_", "partic_ts_")) and e.get("deadline_source") == "detalhe"}
    prazos = {}
    for fonte in (partic.get("prazos_listagem") or {}, partic.get("prazos") or {}, lidos):
        for k, v in fonte.items():
            try:
                d = datetime.strptime(v or "", "%d/%m/%Y")
            except ValueError:
                continue
            if k not in prazos or d > prazos[k]:
                prazos[k] = d
    hoje_d = datetime.strptime(hoje, "%Y-%m-%d")
    out = []
    for k, fim in prazos.items():
        partes = k.split("_")  # partic_cp_036_2026
        if fim < hoje_d or len(partes) != 4 or partes[1] not in ANEEL_LISTAGEM:
            continue
        e = hist.get(k) or {}
        objeto = e.get("objeto") or objetos.get(k) or (e.get("summary") or "").replace("**", "")
        objeto = " ".join(objeto.split())
        out.append({
            "id": k,
            "label": f"{'CP' if partes[1] == 'cp' else 'TS'} {partes[2]}/{partes[3]}",
            "fim": fim,
            "dias": (fim - hoje_d).days,
            "objeto": objeto if len(objeto) <= 220 else objeto[:219].rstrip() + "…",
            "url": ANEEL_LISTAGEM[partes[1]],
        })
    return sorted(out, key=lambda c: (c["fim"], c["label"]))


# ───────────────────────────── Rendering ─────────────────────────────


def _prazo_txt(c):
    quando = "hoje" if c["dias"] == 0 else ("amanhã" if c["dias"] == 1 else f"em {c['dias']} dias")
    return f"até {c['fim']:%d/%m} ({quando})"


def render_text(camara, senado, pauta, abertas, date_str):
    out = [f"=== PLs + ANEEL — {date_str} ===\n"]
    out.append(f"━━ Câmara dos Deputados ({len(camara)}) ━━")
    for p in camara:
        out.append(f"  - {p['label']}: {p['ementa']}")
        out.append(f"    {p['url']}")
    out.append("")
    out.append(f"━━ Senado Federal ({len(senado)}) ━━")
    for p in senado:
        out.append(f"  - {p['label']}: {p['ementa']}")
        out.append(f"    {p['url']}")
    out.append("")
    out.append(f"━━ ANEEL — Na pauta da Diretoria: propostas de CP/TS ({len(pauta)}) ━━")
    for c in pauta:
        out.append(f"  - {c['label']}: {c['assunto']}")
        out.append(f"    {c['meta']}")
    out.append("")
    out.append(f"━━ ANEEL — Consultas abertas ({len(abertas)}) ━━")
    for c in abertas:
        alerta = "  ⚠" if c["dias"] <= FECHA_EM_DIAS else ""
        out.append(f"  - {c['label']} {_prazo_txt(c)}{alerta}: {c['objeto']}")
    return "\n".join(out)


def _item_html(label, body, url, meta=""):
    meta_html = (
        f'<div style="color:#888;font-size:12px;margin:2px 0 0">{meta}</div>'
        if meta
        else ""
    )
    body_html = (
        f'<div style="color:#444;font-size:13px;margin:4px 0 0;line-height:1.45">{body}</div>'
        if body
        else ""
    )
    return (
        f'<li style="margin-bottom:14px">'
        f'<a href="{url}" style="color:#1a73e8;text-decoration:none;font-weight:600">{label}</a>'
        f"{meta_html}{body_html}</li>"
    )


def _section(title, count, items_html, vazio="Nenhuma novidade."):
    inner = items_html or f'<p style="color:#888;font-size:13px;margin:8px 0">{vazio}</p>'
    return (
        f'<h2 style="margin:32px 0 4px;border-bottom:2px solid #1a73e8;'
        f'padding-bottom:4px;color:#1a73e8;font-size:17px">{title} '
        f'<span style="color:#888;font-weight:normal;font-size:0.85em">({count})</span></h2>'
        f'<ul style="padding-left:18px;line-height:1.4;margin:0">{inner}</ul>'
    )


def render_html(camara, senado, pauta, abertas, date_str):
    camara_li = "".join(_item_html(p["label"], p["ementa"], p["url"]) for p in camara)
    senado_li = "".join(_item_html(p["label"], p["ementa"], p["url"]) for p in senado)
    pauta_li = "".join(_item_html(escape(c["label"]), escape(c["assunto"]), c["url"], meta=escape(c["meta"])) for c in pauta)
    perto = [c for c in abertas if c["dias"] <= FECHA_EM_DIAS]
    resto = [c for c in abertas if c["dias"] > FECHA_EM_DIAS]

    def _abertas_li(lista):
        return "".join(_item_html(escape(c["label"]), escape(c["objeto"]), c["url"], meta=_prazo_txt(c)) for c in lista)

    abertas_li = ""
    if perto:
        abertas_li += (f'<p style="color:#c5221f;font-size:13px;font-weight:600;margin:10px 0 6px">'
                       f'Fecham em até {FECHA_EM_DIAS} dias</p>' + _abertas_li(perto))
    if resto:
        abertas_li += ('<p style="color:#666;font-size:13px;font-weight:600;margin:14px 0 6px">Demais</p>'
                       + _abertas_li(resto))
    novidades = len(camara) + len(senado) + len(pauta)
    return f"""<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:680px;margin:auto;color:#222;padding:16px">
<h1 style="font-size:20px;margin:0 0 4px">PLs + ANEEL &mdash; {date_str}</h1>
<p style="color:#666;font-size:13px;margin:0">{novidades} novidade{"s" if novidades != 1 else ""} desde o último e-mail &middot; {len(abertas)} consulta{"s" if len(abertas) != 1 else ""} da ANEEL aberta{"s" if len(abertas) != 1 else ""}.</p>
{_section("Câmara dos Deputados", len(camara), camara_li)}
{_section("Senado Federal", len(senado), senado_li)}
{_section("ANEEL &mdash; Na pauta da Diretoria: propostas de CP/TS", len(pauta), pauta_li, vazio="Nenhuma proposta nova na pauta.")}
{_section("ANEEL &mdash; Consultas abertas", len(abertas), abertas_li, vazio="Nenhuma consulta aberta.")}
<hr style="margin-top:32px;border:none;border-top:1px solid #eee">
<p style="color:#aaa;font-size:11px">Fontes: dadosabertos.camara.leg.br &middot; legis.senado.leg.br &middot; dadosabertos.aneel.gov.br (pauta) &middot; DOU e listagem da ANEEL (prazos)</p>
</body></html>"""


def send_email(subject, html):
    api_key = os.environ["RESEND_API_KEY"]
    to_addr = os.environ["DIGEST_TO"]
    from_addr = os.environ.get("DIGEST_FROM", "PLs Digest <onboarding@resend.dev>")
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"from": from_addr, "to": [to_addr], "subject": subject, "html": html},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Imprime no stdout em vez de enviar email")
    parser.add_argument("--bootstrap", action="store_true", help="Marca tudo como visto, sem enviar email")
    parser.add_argument("--days-back", type=int, default=3, help="Janela de busca em dias (Câmara)")
    args = parser.parse_args()

    state = load_state()
    today = datetime.now(timezone.utc).astimezone()
    hoje_brt = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d")

    print("Buscando Câmara…", file=sys.stderr)
    camara = collect_camara(state, args.days_back)
    print(f"  {len(camara)} novos", file=sys.stderr)

    print("Buscando Senado…", file=sys.stderr)
    senado = collect_senado(state, hoje_brt)
    print(f"  {len(senado)} novos", file=sys.stderr)

    print("Buscando ANEEL (pauta da Diretoria)…", file=sys.stderr)
    try:
        pauta, pauta_vistos = collect_aneel_pauta(state, hoje_brt)
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"  ANEEL pauta falhou: {e}", file=sys.stderr)
        print(f"::warning::ANEEL pauta: {str(e)[:150]}")
        pauta, pauta_vistos = [], []
    print(f"  {len(pauta)} propostas novas na pauta", file=sys.stderr)
    abertas = collect_aneel_prazos(hoje_brt)
    print(f"  {len(abertas)} consultas abertas", file=sys.stderr)

    # Atualiza state com IDs vistos agora (inclusive os que serão enviados).
    state["camara"] = sorted(set(state.get("camara", [])) | {p["id"] for p in camara})
    state["senado"] = sorted(set(state.get("senado", [])) | {p["id"] for p in senado})
    state["aneel_pauta"] = sorted(set(state.get("aneel_pauta", [])) | set(pauta_vistos))
    state.pop("aneel", None)  # ids do dataset antigo (sempre vazio)

    pretty_date = today.strftime("%d/%m/%Y")

    if args.bootstrap:
        save_state(state)
        print(f"bootstrap: estado salvo em {STATE_PATH}", file=sys.stderr)
        return

    if args.dry_run:
        sys.stdout.reconfigure(encoding="utf-8")
        print(render_text(camara, senado, pauta, abertas, pretty_date))
        return

    # Com a lista de consultas abertas (a pedido, 21/10/2026), o e-mail sai todo
    # dia útil em que houver novidade OU consulta aberta.
    novidades = len(camara) + len(senado) + len(pauta)
    if novidades == 0 and not abertas:
        print("nenhuma novidade nem consulta aberta — pulando envio", file=sys.stderr)
        save_state(state)
        return

    html = render_html(camara, senado, pauta, abertas, pretty_date)
    perto = sum(1 for c in abertas if c["dias"] <= FECHA_EM_DIAS)
    subject = (f"PLs + ANEEL — {pretty_date} ({novidades} novidade{'s' if novidades != 1 else ''}"
               + (f" · {perto} CP/TS fechando" if perto else "") + ")")
    result = send_email(subject, html)
    print(f"sent: id={result.get('id', '?')}", file=sys.stderr)
    save_state(state)


if __name__ == "__main__":
    main()
