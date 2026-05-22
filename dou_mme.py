"""
Digest diário do DOU (seção 1) — atos do MME e ANEEL, exceto ANM/ANP/SNGMTM.

Modos:
    python dou_mme.py                    # busca data de hoje, envia por email
    python dou_mme.py --date 19-05-2026  # data específica
    python dou_mme.py --dry-run          # imprime no stdout, não envia email
    python dou_mme.py --backfill 30      # popula dou_history.json com últimos 30 dias
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path

import requests

HISTORY_FILE = Path("dou_history.json")
HISTORY_RETENTION_DAYS = 90
# Cap pra evitar estourar quota free do Gemini (1500 RPD pra gemini-2.5-flash).
# 1500 = 1 run consegue processar quase 1 dia inteiro de quota.
# Cada item leva ~4.2s (rate limit 15 RPM) → 1500 items = ~105 min.
# GHA tem timeout default de 6h, então fica folgado.
MAX_SUMMARIZE_PER_RUN = 1500

BASE_URL = "https://www.in.gov.br/consulta/-/buscar/dou"
HEADERS = {"User-Agent": "Mozilla/5.0 (dou-mme-digest)"}

# Queries usadas para varrer o dia. Como o DOU não tem filtro nativo por órgão
# via URL, a gente puxa vários termos amplos e depois filtra pela hierarchyStr.
QUERIES = ["ANEEL", "Minas e Energia", "energia elétrica", "leilão energia"]

TARGET_PREFIXES = [
    "ministério de minas e energia",
    "ministerio de minas e energia",
]

# Suborgãos do MME a IGNORAR (mineração e petróleo não interessam).
SUBORG_BLOCKLIST = [
    "agência nacional de mineração",
    "agência nacional do petróleo",
    "secretaria nacional de geologia",
]


def fetch_page(date_str, query, page):
    params = {
        "q": query,
        "s": "do1",
        "exactDate": "personalizado",
        "publishFrom": date_str,
        "publishTo": date_str,
        "delta": 20,
        "currentPage": page,
        "newPage": page,
    }
    r = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    m = re.search(
        r'_BuscaDouPortlet_params"[^>]*>\s*(\{.*?\})\s*</script>',
        r.text,
        re.DOTALL,
    )
    if not m:
        return [], 0
    data = json.loads(m.group(1))
    tp_match = re.search(r"totalPages\s*:\s*(\d+)", r.text)
    total_pages = int(tp_match.group(1)) if tp_match else 1
    return data.get("jsonArray", []), total_pages


def fetch_all(date_str, query):
    hits, total_pages = fetch_page(date_str, query, 1)
    out = list(hits)
    for p in range(2, total_pages + 1):
        more, _ = fetch_page(date_str, query, p)
        out.extend(more)
    return out


def sub_org(hit):
    lst = hit.get("hierarchyList") or []
    return lst[1] if len(lst) > 1 else "(sem subórgão)"


def is_mme(hit):
    h = (hit.get("hierarchyStr") or "").lower()
    if not any(h.startswith(p) for p in TARGET_PREFIXES):
        return False
    sub = sub_org(hit).lower()
    return not any(b in sub for b in SUBORG_BLOCKLIST)


def link_for(hit):
    return f"https://www.in.gov.br/web/dou/-/{hit['urlTitle']}"


_PARA_RE = re.compile(
    r'<p class="dou-paragraph"[^>]*>(.*?)</p>', re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
SUMMARY_MAX = 280


def fetch_summary(url):
    """Pega o 2º parágrafo do ato (1º é sempre o preâmbulo) e trunca."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except requests.RequestException:
        return ""
    paras = []
    for m in _PARA_RE.finditer(r.text):
        text = _WS_RE.sub(" ", unescape(_TAG_RE.sub("", m.group(1)))).strip()
        if text:
            paras.append(text)
    if len(paras) < 2:
        return paras[0][:SUMMARY_MAX] if paras else ""
    body = paras[1]
    if len(body) <= SUMMARY_MAX:
        return body
    cut = body.rfind(" ", 0, SUMMARY_MAX)
    return body[: cut if cut > 0 else SUMMARY_MAX].rstrip(",;:.") + "…"


def collect(date_str, with_summary=True):
    seen = {}
    for q in QUERIES:
        for h in fetch_all(date_str, q):
            seen.setdefault(h["urlTitle"], h)
    hits = [h for h in seen.values() if is_mme(h)]
    if with_summary:
        for h in hits:
            h["summary"] = fetch_summary(link_for(h))
    return hits


def group_by_sub(hits):
    by_sub = {}
    for h in hits:
        by_sub.setdefault(sub_org(h), []).append(h)
    return by_sub


# Taxonomia espelhando atosoficiais.com.br/aneel:
# "Atos" = decisões regulatórias (resoluções, despachos, portarias, instruções).
# "Outros Atos" = avisos, editais, extratos de contrato, leilões.
# Ordem importa: padrões mais específicos primeiro pra match correto.
ACT_TYPES_ATOS = [
    ("Resolução Normativa",     ("RESOLUÇÃO NORMATIVA", "RESOLUCAO NORMATIVA")),
    ("Resolução Autorizativa",  ("RESOLUÇÃO AUTORIZATIVA", "RESOLUCAO AUTORIZATIVA")),
    ("Resolução Homologatória", ("RESOLUÇÃO HOMOLOGATÓRIA", "RESOLUCAO HOMOLOGATORIA")),
    ("Resolução Conjunta",      ("RESOLUÇÃO CONJUNTA", "RESOLUCAO CONJUNTA")),
    ("Resolução",               ("RESOLUÇÃO", "RESOLUCAO")),  # fallback genérico
    ("Portaria Conjunta",       ("PORTARIA CONJUNTA",)),
    ("Portaria",                ("PORTARIA",)),
    ("Instrução Administrativa", ("INSTRUÇÃO ADMINISTRATIVA", "INSTRUCAO ADMINISTRATIVA")),
    ("Despacho",                ("DESPACHO",)),
]
ACT_TYPES_OUTROS = [
    ("Aviso de Audiência Pública",   ("AVISO DE AUDIÊNCIA PÚBLICA", "AVISO DE AUDIENCIA PUBLICA")),
    ("Aviso de Consulta Pública",    ("AVISO DE CONSULTA PÚBLICA", "AVISO DE CONSULTA PUBLICA")),
    ("Aviso de Tomada de Subsídios", ("AVISO DE TOMADA DE SUBSÍDIOS", "AVISO DE TOMADA DE SUBSIDIOS")),
    ("Avisos",                       ("AVISO",)),  # catch-all p/ avisos genéricos
    ("Comunicado",                   ("COMUNICADO",)),
    ("Edital",                       ("EDITAL",)),
    ("Leilão",                       ("LEILÃO", "LEILAO")),
    # Extratos: mais específicos primeiro
    ("Extrato de Contrato de Concessão de Distribuição",
     ("EXTRATO DE CONTRATO DE CONCESSÃO DE DISTRIBUIÇÃO", "EXTRATO DE CONTRATO DE CONCESSAO DE DISTRIBUICAO")),
    ("Extrato de Contrato de Concessão de Geração",
     ("EXTRATO DE CONTRATO DE CONCESSÃO DE GERAÇÃO", "EXTRATO DE CONTRATO DE CONCESSAO DE GERACAO")),
    ("Extrato de Contrato de Concessão de Transmissão",
     ("EXTRATO DE CONTRATO DE CONCESSÃO DE TRANSMISSÃO", "EXTRATO DE CONTRATO DE CONCESSAO DE TRANSMISSAO")),
    ("Extrato de Contrato de Concessão de Uso de Bem Público",
     ("EXTRATO DE CONTRATO DE CONCESSÃO DE USO DE BEM PÚBLICO", "EXTRATO DE CONTRATO DE CONCESSAO DE USO DE BEM PUBLICO")),
    ("Extrato de Contrato de Concessão",
     ("EXTRATO DE CONTRATO DE CONCESSÃO", "EXTRATO DE CONTRATO DE CONCESSAO")),
    ("Extrato de Contrato de Metas",      ("EXTRATO DE CONTRATO DE METAS",)),
    ("Extrato de Contrato de Permissão",
     ("EXTRATO DE CONTRATO DE PERMISSÃO", "EXTRATO DE CONTRATO DE PERMISSAO")),
    ("Extrato de Contrato MME",           ("EXTRATO DE CONTRATO MME",)),
    ("Extrato de Contrato",               ("EXTRATO DE CONTRATO",)),
    ("Extrato de Acordo de Cooperação Técnica",
     ("EXTRATO DE ACORDO DE COOPERAÇÃO TÉCNICA", "EXTRATO DE ACORDO DE COOPERACAO TECNICA")),
    ("Extrato de Carta-Contrato",
     ("EXTRATO DE CARTA-CONTRATO", "EXTRATO DE CARTA CONTRATO")),
    ("Extrato de Comodato",               ("EXTRATO DE COMODATO",)),
]
ATOS_ORDER = [t for t, _ in ACT_TYPES_ATOS]
OUTROS_ORDER = [t for t, _ in ACT_TYPES_OUTROS] + ["Outro"]


def classify(hit):
    """Returns (top_bucket, act_type). Default: ('Outros Atos', 'Outro')."""
    title = (hit.get("title") or "").upper()
    for label, prefixes in ACT_TYPES_ATOS:
        if any(title.startswith(p) for p in prefixes):
            return "Atos", label
    for label, prefixes in ACT_TYPES_OUTROS:
        if any(title.startswith(p) for p in prefixes):
            return "Outros Atos", label
    return "Outros Atos", "Outro"


def source_of(hit):
    sub = sub_org(hit)
    if "Agência Nacional de Energia Elétrica" in sub:
        return "ANEEL"
    return "MME"


def group_by_taxonomy(hits):
    """{source: {bucket: {type: [hits]}}} preservando ordem das listas-mestre."""
    tree = {}
    for h in hits:
        src = source_of(h)
        bucket, atype = classify(h)
        tree.setdefault(src, {}).setdefault(bucket, {}).setdefault(atype, []).append(h)
    return tree


def _ordered_types(bucket):
    return ATOS_ORDER if bucket == "Atos" else OUTROS_ORDER


def _iter_sources(tree):
    # ANEEL primeiro, depois MME, depois qualquer outro em ordem alfabética.
    priority = {"ANEEL": 0, "MME": 1}
    return sorted(tree.keys(), key=lambda s: (priority.get(s, 9), s))


def render_text(tree, date_str):
    total = sum(
        len(hits)
        for buckets in tree.values()
        for types in buckets.values()
        for hits in types.values()
    )
    out = [
        f"=== DOU {date_str} — Seção 1 — MME + ANEEL ===",
        f"Total: {total} atos\n",
    ]
    for src in _iter_sources(tree):
        buckets = tree[src]
        for bucket in ("Atos", "Outros Atos"):
            if bucket not in buckets:
                continue
            types = buckets[bucket]
            bucket_count = sum(len(v) for v in types.values())
            out.append(f"━━ {src} — {bucket}  ({bucket_count}) ━━")
            for tlabel in _ordered_types(bucket):
                if tlabel not in types:
                    continue
                items = types[tlabel]
                out.append(f"  ## {tlabel}  ({len(items)})")
                for h in items:
                    tail = h["hierarchyList"][2:] if len(h["hierarchyList"]) > 2 else []
                    out.append(f"    - {h['title']}")
                    if tail:
                        out.append(f"      [{' / '.join(tail)}]")
                    if h.get("summary"):
                        out.append(f"      {h['summary']}")
                    out.append(f"      {link_for(h)}")
                out.append("")
    return "\n".join(out)


def _render_item_html(h):
    tail = h["hierarchyList"][2:] if len(h["hierarchyList"]) > 2 else []
    tail_html = (
        f'<div style="color:#888;font-size:12px;margin:2px 0 0">'
        f'{" / ".join(tail)}</div>'
        if tail
        else ""
    )
    summary_html = (
        f'<div style="color:#444;font-size:13px;margin:4px 0 0;'
        f'line-height:1.45">{h["summary"]}</div>'
        if h.get("summary")
        else ""
    )
    return (
        f'<li style="margin-bottom:14px">'
        f'<a href="{link_for(h)}" '
        f'style="color:#1a73e8;text-decoration:none">{h["title"]}</a>'
        f"{tail_html}{summary_html}</li>"
    )


def render_html(tree, date_str):
    total = sum(
        len(hits)
        for buckets in tree.values()
        for types in buckets.values()
        for hits in types.values()
    )
    sections = []
    for src in _iter_sources(tree):
        buckets = tree[src]
        for bucket in ("Atos", "Outros Atos"):
            if bucket not in buckets:
                continue
            types = buckets[bucket]
            bucket_count = sum(len(v) for v in types.values())
            type_blocks = []
            for tlabel in _ordered_types(bucket):
                if tlabel not in types:
                    continue
                items = types[tlabel]
                rows = "".join(_render_item_html(h) for h in items)
                type_blocks.append(
                    f'<h4 style="margin:18px 0 6px;color:#333;'
                    f'font-size:14px;font-weight:600">{tlabel} '
                    f'<span style="color:#888;font-weight:normal;font-size:0.9em">'
                    f"({len(items)})</span></h4>"
                    f'<ul style="padding-left:18px;line-height:1.4;margin:0">{rows}</ul>'
                )
            sections.append(
                f'<h2 style="margin:32px 0 4px;border-bottom:2px solid #1a73e8;'
                f'padding-bottom:4px;color:#1a73e8;font-size:17px">'
                f"{src} &mdash; {bucket} "
                f'<span style="color:#888;font-weight:normal;font-size:0.85em">'
                f"({bucket_count})</span></h2>"
                f"{''.join(type_blocks)}"
            )
    body = (
        "\n".join(sections)
        if sections
        else '<p style="color:#888">Nenhum ato do MME/ANEEL publicado hoje.</p>'
    )
    return f"""<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:680px;margin:auto;color:#222;padding:16px">
<h1 style="font-size:20px;margin:0 0 4px">DOU MME/ANEEL &mdash; {date_str}</h1>
<p style="color:#666;font-size:13px;margin:0">{total} atos publicados hoje na Se&ccedil;&atilde;o 1.</p>
{body}
<hr style="margin-top:32px;border:none;border-top:1px solid #eee">
<p style="color:#aaa;font-size:11px">Fonte: Imprensa Nacional &mdash; in.gov.br</p>
</body></html>"""


def send_email(subject, html):
    api_key = os.environ["RESEND_API_KEY"]
    to_addr = os.environ["DIGEST_TO"]
    from_addr = os.environ.get("DIGEST_FROM", "DOU Digest <onboarding@resend.dev>")
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"from": from_addr, "to": [to_addr], "subject": subject, "html": html},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _clean_content(html_content):
    """Strip HTML tags do snippet do DOU."""
    text = re.sub(r"<[^>]+>", "", html_content or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


_GEMINI_CLIENT = None


def _fetch_full_dou(link: str) -> tuple[str, str]:
    """
    Fetcha página DOU em in.gov.br e retorna (texto_completo, objeto).
    'objeto' é o trecho após 'Objeto:' até 'A íntegra' ou fim do parágrafo.
    Retorna ("", "") em caso de erro.
    """
    if not link:
        return "", ""
    try:
        import trafilatura
        r = requests.get(
            link,
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "pt-BR"},
            timeout=15,
        )
        if r.status_code != 200:
            return "", ""
        text = trafilatura.extract(r.content, favor_recall=True, include_comments=False)
        if not text:
            return "", ""
        # Extrai "Objeto: ... (até 'A íntegra' ou \n\n ou fim)"
        m = re.search(
            r"Objeto\s*:\s*(.+?)(?:\s*A\s+íntegra|\n\n|$)",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        objeto = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        return text[:8000], objeto
    except Exception as e:
        print(f"[fetch_full] erro: {e}", file=sys.stderr)
        return "", ""


def _gemini_client():
    """Lazy init do cliente Gemini. Retorna None se chave não estiver setada."""
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is not None:
        return _GEMINI_CLIENT
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai
        _GEMINI_CLIENT = genai.Client(api_key=api_key)
        return _GEMINI_CLIENT
    except Exception as e:
        print(f"[gemini] init falhou: {e}", file=sys.stderr)
        return None


def _summarize_dou_act(title, body, objeto=""):
    """Resume um ato em 2-3 frases usando Gemini. Retorna None em caso de erro."""
    client = _gemini_client()
    if not client:
        return None
    try:
        from google.genai import types
        objeto_hint = (
            f"\nObjeto declarado: {objeto}\n"
            if objeto else ""
        )
        prompt = (
            "Você é um analista do setor brasileiro de utilities (energia elétrica e saneamento) "
            "resumindo atos do DOU pra um colega que precisa decidir rápido se o ato merece atenção.\n\n"
            "REGRAS:\n"
            "- Resuma em 2-3 frases EM PORTUGUÊS BRASILEIRO.\n"
            "- Diga COM CLAREZA o que foi decidido/autorizado/homologado/aplicado/cancelado.\n"
            "- Cite EMPRESAS, USINAS, PROJETOS, VALORES e DATAS específicas (não 'algo').\n"
            "- Use o trecho 'Objeto:' como guia principal se aparecer.\n"
            "- PULE boilerplate jurídico ('no uso de suas atribuições', 'considerando os autos', etc.).\n"
            "- Se houver dados sobre EMPRESAS DO SETOR REGULADO (transmissoras, distribuidoras, "
            "geradoras, saneamento), destaque o nome.\n"
            "- Se for ato administrativo genérico (recurso, exigência, etc.) que NÃO tem impacto "
            "material, diga apenas 'Recurso administrativo de [empresa] sobre [processo]'.\n\n"
            f"Título: {title}\n{objeto_hint}"
            f"\nTexto completo:\n{body}\n\n"
            "Resumo (2-3 frases descritivas):"
        )
        r = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=600,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        return (r.text or "").strip() or None
    except Exception as e:
        print(f"[gemini] erro ao resumir: {e}", file=sys.stderr)
        return None


def _save_history(history):
    """Persiste o histórico no arquivo JSON."""
    history["last_updated"] = datetime.now().isoformat()
    HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def summarize_pending_history(limit=MAX_SUMMARIZE_PER_RUN):
    """
    Adiciona campo 'summary' aos items que ainda não têm. Cap pra controlar quota.
    Salva incremental a cada 25 items pra não perder progresso em caso de crash/timeout.
    Para imediatamente se detectar 5 erros consecutivos (provável quota hit).
    """
    if not HISTORY_FILE.exists():
        return 0
    if not _gemini_client():
        print("[summarize] GEMINI_API_KEY não definido — pulando", file=sys.stderr)
        return 0

    history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    # Items pendentes: sem summary OU sem objeto (objeto é novo, indica que
    # o item foi processado pela versão antiga sem fetch full text → resumo
    # provavelmente raso. Re-processa com o full text).
    pending = [
        i for i in history["items"]
        if not i.get("summary") or not i.get("objeto")
    ]
    if not pending:
        print("[summarize] tudo em dia", file=sys.stderr)
        return 0

    target = pending[:limit]
    print(f"[summarize] {len(target)}/{len(pending)} pendentes nessa rodada", file=sys.stderr)

    done = 0
    consecutive_failures = 0
    SAVE_EVERY = 25
    MAX_CONSECUTIVE_FAILURES = 5  # provavelmente quota hit, para de tentar

    for i, item in enumerate(target, 1):
        # 1. Fetch full text + extract Objeto (sempre faz, não custa quota Gemini)
        full_text, objeto = _fetch_full_dou(item.get("link", ""))
        if objeto:
            item["objeto"] = objeto

        # 2. Summarize usando texto completo (muito melhor que snippet boilerplate)
        body_for_summary = full_text or item.get("content", "")
        summary = _summarize_dou_act(item["title"], body_for_summary, objeto or item.get("objeto", ""))
        if summary:
            item["summary"] = summary
            done += 1
            consecutive_failures = 0
            print(f"  ✓ [{i}/{len(target)}] {summary[:80]}", file=sys.stderr)
        else:
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(
                    f"[summarize] {MAX_CONSECUTIVE_FAILURES} falhas consecutivas — "
                    f"provavelmente quota Gemini esgotada. Parando.",
                    file=sys.stderr,
                )
                break
        # Save incremental
        if i % SAVE_EVERY == 0:
            _save_history(history)
            print(f"[summarize] checkpoint salvo ({done} resumos até agora)", file=sys.stderr)
        # Rate limit Gemini free tier: 15 RPM ≈ 1 req cada 4s.
        time.sleep(4.2)

    # Save final
    _save_history(history)
    print(f"[summarize] {done} resumos gerados nessa rodada", file=sys.stderr)
    return done


def update_history(hits):
    """Mescla hits no dou_history.json, dedupe por urlTitle, prune > N dias."""
    if HISTORY_FILE.exists():
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    else:
        history = {"last_updated": None, "items": []}

    existing_ids = {item["id"] for item in history["items"]}
    added = 0

    for hit in hits:
        id_ = (hit.get("urlTitle") or "").strip()
        if not id_ or id_ in existing_ids:
            continue
        hl = hit.get("hierarchyList", [])
        orgao = hl[1] if len(hl) > 1 else (hl[0] if hl else "")
        history["items"].append({
            "id": id_,
            "title": hit.get("title", ""),
            "pubDate": hit.get("pubDate", ""),
            "displayDate": hit.get("displayDate", ""),
            "artType": hit.get("artType", ""),
            "orgao": orgao,
            "hierarchy": hit.get("hierarchyStr", ""),
            "content": _clean_content(hit.get("content", "")),
            "link": link_for(hit),
        })
        existing_ids.add(id_)
        added += 1

    # Prune (mantém últimos N dias)
    cutoff = (datetime.now() - timedelta(days=HISTORY_RETENTION_DAYS)).strftime("%Y%m%d000000")
    before = len(history["items"])
    history["items"] = [i for i in history["items"] if i.get("displayDate", "0") >= cutoff]
    pruned = before - len(history["items"])

    history["items"].sort(key=lambda i: i.get("displayDate", "0"), reverse=True)
    history["last_updated"] = datetime.now().isoformat()

    HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"[history] +{added} novos, -{pruned} antigos, total {len(history['items'])}",
        file=sys.stderr,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--date",
        default=datetime.now().strftime("%d-%m-%Y"),
        help="Data de publicação no formato dd-mm-aaaa (padrão: hoje)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Imprime no stdout em vez de enviar email",
    )
    parser.add_argument(
        "--backfill",
        type=int,
        default=0,
        help="Popula dou_history.json com últimos N dias (sem enviar email)",
    )
    parser.add_argument(
        "--summarize-only",
        action="store_true",
        help="Apenas resume items pendentes em dou_history.json (sem scrape, sem email)",
    )
    args = parser.parse_args()

    if args.summarize_only:
        summarize_pending_history()
        return

    if args.backfill > 0:
        for i in range(args.backfill):
            date = (datetime.now() - timedelta(days=i)).strftime("%d-%m-%Y")
            try:
                print(f"\n=== Backfill {date} ({i + 1}/{args.backfill}) ===", file=sys.stderr)
                hits = collect(date, with_summary=False)
                update_history(hits)
            except Exception as e:
                print(f"[backfill {date}] ERR: {e}", file=sys.stderr)
        # Após backfill, tenta resumir
        summarize_pending_history()
        return

    print(f"Buscando DOU {args.date} (seção 1)…", file=sys.stderr)
    hits = collect(args.date)
    update_history(hits)
    summarize_pending_history()
    tree = group_by_taxonomy(hits)
    print(f"  {len(hits)} atos do MME/ANEEL", file=sys.stderr)

    pretty_date = args.date.replace("-", "/")
    subject = f"DOU MME/ANEEL — {pretty_date}"

    if args.dry_run:
        sys.stdout.reconfigure(encoding="utf-8")
        print(render_text(tree, pretty_date))
        return

    html = render_html(tree, pretty_date)
    result = send_email(subject, html)
    print(f"sent: id={result.get('id', '?')}", file=sys.stderr)


if __name__ == "__main__":
    main()
