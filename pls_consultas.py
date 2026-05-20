"""
Digest diário de PLs (Câmara + Senado) e Consultas/Audiências Públicas da ANEEL.

Filtra por palavras-chave de utilities (energia, saneamento, tarifa, renováveis)
e mantém estado em state/pls_consultas.json pra mostrar só novidades.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (utilities-news/pls-consultas)"}
STATE_PATH = Path(__file__).parent / "state" / "pls_consultas.json"

# Palavras-chave (regex, case-insensitive). Aplicadas sobre ementa/título.
KEYWORDS = re.compile(
    r"\b("
    r"energia\s+elétrica|setor\s+el[ée]trico|aneel|"
    r"saneamento|marco\s+do\s+saneamento|\bana\b|"
    r"tarifa(s|ria|s)?|subs[íi]dio|\bcde\b|encargo\s+setorial|"
    r"renov[áa]vel|gera[çc][ãa]o\s+distribu[íi]da|\bgd\b|hidrog[êe]nio|"
    r"transi[çc][ãa]o\s+energ[ée]tica|e[óo]lica|solar\s+fotovoltaica"
    r")\b",
    re.IGNORECASE,
)

# Temas da Câmara: 54=Energia/Hídricos/Minerais, 48=Meio Ambiente, 41=Cidades.
CAMARA_THEMES = [54, 48, 41]
CAMARA_TIPOS = {"PL", "PLP", "PEC", "PDL", "MPV"}
SENADO_KEYWORDS = [
    "energia elétrica",
    "saneamento básico",
    "tarifa energia",
    "subsídio tarifário",
    "geração distribuída",
    "hidrogênio verde",
    "transição energética",
]

CAMARA_API = "https://dadosabertos.camara.leg.br/api/v2"
SENADO_API = "https://legis.senado.leg.br/dadosabertos"
ANEEL_PACKAGE = "audiencias-e-consultas-publicas"
ANEEL_CKAN = "https://dadosabertos.aneel.gov.br/api/3/action"


def load_state():
    if not STATE_PATH.exists():
        return {"camara": [], "senado": [], "aneel": []}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"camara": [], "senado": [], "aneel": []}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Mantém só os últimos N IDs por fonte pra arquivo não crescer indefinidamente.
    trimmed = {k: list(v)[-2000:] for k, v in state.items()}
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


def collect_senado(state, ano):
    seen_ids = set(state.get("senado", []))
    by_code = {}
    for kw in SENADO_KEYWORDS:
        try:
            r = requests.get(
                f"{SENADO_API}/materia/pesquisa/lista.json",
                params={"palavraChave": kw, "ano": ano},
                headers={**HEADERS, "Accept": "application/json"},
                timeout=30,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"  senado kw={kw!r} falhou: {e}", file=sys.stderr)
            continue
        data = r.json() or {}
        materias = (
            data.get("PesquisaBasicaMateria", {})
            .get("Materias", {})
            .get("Materia", [])
        )
        if isinstance(materias, dict):
            materias = [materias]
        for m in materias:
            codigo = m.get("Codigo")
            sigla = (m.get("Sigla") or "").upper()
            if not codigo or str(codigo) in seen_ids or codigo in by_code:
                continue
            # Mantém só projetos/medidas; descarta REQ, RQS, INC, OFS etc.
            if sigla not in {"PL", "PLP", "PEC", "PDL", "PLS", "PLN", "MPV"}:
                continue
            ementa = (m.get("Ementa") or "").strip()
            # Senado tokeniza palavraChave solta — re-aplica regex pra cortar falsos positivos.
            if not KEYWORDS.search(ementa):
                continue
            by_code[codigo] = {
                "id": str(codigo),
                "label": m.get(
                    "DescricaoIdentificacao",
                    f"{sigla} {m.get('Numero','?')}/{m.get('Ano','?')}",
                ),
                "ementa": ementa,
                "url": f"https://www25.senado.leg.br/web/atividade/materias/-/materia/{codigo}",
            }
    return list(by_code.values())


# ───────────────────────────── ANEEL ─────────────────────────────


def _aneel_resource_id():
    r = requests.get(
        f"{ANEEL_CKAN}/package_show",
        params={"id": ANEEL_PACKAGE},
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    resources = r.json().get("result", {}).get("resources", [])
    # Preferência: JSON nativo > CSV (CKAN datastore funciona pros dois).
    for fmt in ("JSON", "CSV"):
        for res in resources:
            if (res.get("format") or "").upper() == fmt and res.get("datastore_active"):
                return res["id"]
    # Fallback: primeiro recurso com datastore_active.
    for res in resources:
        if res.get("datastore_active"):
            return res["id"]
    raise RuntimeError("Nenhum recurso ANEEL com datastore_active encontrado")


def _aneel_rows(resource_id, limit=1000):
    r = requests.get(
        f"{ANEEL_CKAN}/datastore_search",
        params={"resource_id": resource_id, "limit": limit},
        headers=HEADERS,
        timeout=60,
    )
    r.raise_for_status()
    return r.json().get("result", {}).get("records", [])


def _pick(row, *candidates, default=""):
    """Pega o primeiro campo presente (CKAN ANEEL muda nomes entre versões)."""
    for k in candidates:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return default


def collect_aneel(state):
    try:
        rid = _aneel_resource_id()
        rows = _aneel_rows(rid)
    except (requests.RequestException, RuntimeError, KeyError) as e:
        print(f"  ANEEL CKAN falhou: {e}", file=sys.stderr)
        return []
    seen_ids = set(state.get("aneel", []))
    novos = []
    for row in rows:
        # Identificador estável: número + ano + tipo.
        numero = _pick(row, "NumAudienciaConsultaPublica", "NumeroAudConsPublica", "Numero")
        ano = _pick(row, "AnoAudienciaConsultaPublica", "AnoAudConsPublica", "Ano")
        tipo = _pick(row, "DscModalidadeContribuicao", "TipoAudienciaConsultaPublica", "Tipo", default="CP/AP")
        if not numero or not ano:
            continue
        uid = f"{tipo}-{numero}-{ano}"
        if uid in seen_ids:
            continue
        assunto = _pick(row, "DscObjeto", "DscAssunto", "Assunto", "Objeto")
        dt_ini = _pick(row, "DatAberturaContribuicao", "DataAbertura", "DatInicio")
        dt_fim = _pick(row, "DatEncerramentoContribuicao", "DataEncerramento", "DatFim")
        link = _pick(row, "Link", "Url", "LnkProcesso", default="https://www.gov.br/aneel/pt-br/acesso-a-informacao/participacao-social/consultas-publicas")
        novos.append(
            {
                "id": uid,
                "label": f"{tipo} {numero}/{ano}",
                "assunto": assunto,
                "abertura": dt_ini,
                "encerramento": dt_fim,
                "url": link,
            }
        )
    return novos


# ───────────────────────────── Rendering ─────────────────────────────


def render_text(camara, senado, aneel, date_str):
    out = [f"=== PLs + Consultas Públicas ANEEL — {date_str} ===\n"]
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
    out.append(f"━━ ANEEL — CP/AP ({len(aneel)}) ━━")
    for c in aneel:
        prazo = f"  [aberta: {c['abertura']} • encerra: {c['encerramento']}]" if c.get("encerramento") else ""
        out.append(f"  - {c['label']}: {c['assunto']}{prazo}")
        out.append(f"    {c['url']}")
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


def _section(title, count, items_html):
    inner = items_html or '<p style="color:#888;font-size:13px;margin:8px 0">Nenhuma novidade.</p>'
    return (
        f'<h2 style="margin:32px 0 4px;border-bottom:2px solid #1a73e8;'
        f'padding-bottom:4px;color:#1a73e8;font-size:17px">{title} '
        f'<span style="color:#888;font-weight:normal;font-size:0.85em">({count})</span></h2>'
        f'<ul style="padding-left:18px;line-height:1.4;margin:0">{inner}</ul>'
    )


def render_html(camara, senado, aneel, date_str):
    camara_li = "".join(_item_html(p["label"], p["ementa"], p["url"]) for p in camara)
    senado_li = "".join(_item_html(p["label"], p["ementa"], p["url"]) for p in senado)
    aneel_li = "".join(
        _item_html(
            c["label"],
            c["assunto"],
            c["url"],
            meta=(f"aberta: {c['abertura']} • encerra: {c['encerramento']}" if c.get("encerramento") else ""),
        )
        for c in aneel
    )
    total = len(camara) + len(senado) + len(aneel)
    return f"""<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:680px;margin:auto;color:#222;padding:16px">
<h1 style="font-size:20px;margin:0 0 4px">PLs + Consultas Públicas ANEEL &mdash; {date_str}</h1>
<p style="color:#666;font-size:13px;margin:0">{total} novidades nas últimas 24h.</p>
{_section("Câmara dos Deputados", len(camara), camara_li)}
{_section("Senado Federal", len(senado), senado_li)}
{_section("ANEEL &mdash; Consultas e Audiências Públicas", len(aneel), aneel_li)}
<hr style="margin-top:32px;border:none;border-top:1px solid #eee">
<p style="color:#aaa;font-size:11px">Fontes: dadosabertos.camara.leg.br &middot; legis.senado.leg.br &middot; dadosabertos.aneel.gov.br</p>
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
    ano = today.year

    print("Buscando Câmara…", file=sys.stderr)
    camara = collect_camara(state, args.days_back)
    print(f"  {len(camara)} novos", file=sys.stderr)

    print("Buscando Senado…", file=sys.stderr)
    senado = collect_senado(state, ano)
    print(f"  {len(senado)} novos", file=sys.stderr)

    print("Buscando ANEEL CP/AP…", file=sys.stderr)
    aneel = collect_aneel(state)
    print(f"  {len(aneel)} novos", file=sys.stderr)

    # Atualiza state com IDs vistos agora (inclusive os que serão enviados).
    state["camara"] = sorted(set(state.get("camara", [])) | {p["id"] for p in camara})
    state["senado"] = sorted(set(state.get("senado", [])) | {p["id"] for p in senado})
    state["aneel"] = sorted(set(state.get("aneel", [])) | {c["id"] for c in aneel})

    pretty_date = today.strftime("%d/%m/%Y")

    if args.bootstrap:
        save_state(state)
        print(f"bootstrap: estado salvo em {STATE_PATH}", file=sys.stderr)
        return

    if args.dry_run:
        sys.stdout.reconfigure(encoding="utf-8")
        print(render_text(camara, senado, aneel, pretty_date))
        return

    total = len(camara) + len(senado) + len(aneel)
    if total == 0:
        print("nenhuma novidade — pulando envio", file=sys.stderr)
        save_state(state)
        return

    html = render_html(camara, senado, aneel, pretty_date)
    subject = f"PLs + ANEEL CP — {pretty_date} ({total})"
    result = send_email(subject, html)
    print(f"sent: id={result.get('id', '?')}", file=sys.stderr)
    save_state(state)


if __name__ == "__main__":
    main()
