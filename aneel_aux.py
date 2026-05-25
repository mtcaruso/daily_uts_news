"""
ANEEL Auxiliar — coleta movimentos da ANEEL além dos atos oficiais do DOU:

1. Sala de Imprensa (gov.br/aneel/pt-br/assuntos/noticias) — comunicados, anúncios,
   audiências públicas, consultas, leilões, reajustes.

2. Pautas e Atas das Reuniões Públicas da Diretoria (Liferay legacy) — agenda
   do que será votado em cada RD/RPO/RPE/Circuito Deliberativo, normalmente
   publicada 3-7 dias antes da reunião.

Persiste em aneel_aux_history.json, keyed por ID único (slug pra notícia, idNoticia
pra pauta). Resume cada item via Gemini Flash com mesmo padrão markdown **bold**.

Roda no workflow aneel_aux.yml a cada 2-3h.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
import trafilatura

# curl_cffi impersona Chrome real — necessário pra acessar Liferay legacy ANEEL
# que retorna 403 a User-Agents "padrão" de bot
from curl_cffi import requests as cf_requests

HISTORY_FILE = Path("aneel_aux_history.json")
DIAGNOSTIC_FILE = Path("aneel_aux_diagnostic.json")
HISTORY_RETENTION_DAYS = 180  # ANEEL movimentos podem ser referenciados por meses
MAX_SUMMARIZE_PER_RUN = 100

# URLs
NEWS_URL = "https://www.gov.br/aneel/pt-br/assuntos/noticias"
PAUTAS_URL = "https://www2.aneel.gov.br/aplicacoes_liferay/noticias_area/?idAreaNoticia=425"
PAUTA_DETAIL_TPL = "https://www2.aneel.gov.br/aplicacoes_liferay/noticias_area/dsp_detalheNoticia.cfm?idNoticia={id}&idAreaNoticia=425"
# Participação Pública (CP/AP/TS) — antigo.aneel.gov.br Liferay portal
PARTIC_URLS = [
    ("cp", "https://antigo.aneel.gov.br/consultas-publicas", "Consulta"),
    ("ap", "https://antigo.aneel.gov.br/audiencias-publicas", "Audiência"),
    ("ts", "https://antigo.aneel.gov.br/tomadas-de-subsidios", "Tomada"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}

_GEMINI_CLIENT = None


def _gemini_client():
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


# ============== COLETA DE LISTAGENS ==============

def fetch_news_list():
    """Retorna lista de items da Sala de Imprensa gov.br/aneel.
    Cada item: {type, id, title, date, link}."""
    try:
        r = requests.get(NEWS_URL, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return []
    except Exception as e:
        print(f"[news_list] {e}", file=sys.stderr)
        return []

    items = []
    # Padrão Plone: <h2><a href="...noticias/AAAA/slug">TÍTULO</a></h2>
    # Data fica em irmão próximo (geralmente "Publicado em DD/MM/AAAA")
    matches = re.findall(
        r'<h[1-3][^>]*>\s*<a[^>]*href="(https?://www\.gov\.br/aneel/pt-br/assuntos/noticias/(\d{4})/([^"/]+))"[^>]*>([^<]+)</a>',
        r.text,
    )
    seen = set()
    for full_url, year, slug, title in matches:
        if slug in seen:
            continue
        seen.add(slug)
        items.append({
            "type": "noticia",
            "id": f"noticia_{year}_{slug}",
            "title": title.strip(),
            "date": None,  # data exata só após fetch do detalhe
            "year": year,
            "link": full_url,
        })
    return items


def _liferay_get(url, max_attempts=3):
    """GET pra Liferay com múltiplas impersonations + retry.
    Liferay anti-bot é flaky mesmo de IP residencial."""
    impersonations = ["chrome120", "chrome116", "edge99"]
    for attempt in range(max_attempts):
        imp = impersonations[attempt % len(impersonations)]
        try:
            r = cf_requests.get(url, impersonate=imp, timeout=30)
            if r.status_code == 200:
                return r
            if attempt < max_attempts - 1:
                time.sleep(3 + attempt * 2)  # 3s, 5s, 7s entre tentativas
        except Exception as e:
            if attempt < max_attempts - 1:
                time.sleep(3)
            else:
                print(f"[liferay] {e}", file=sys.stderr)
    return None


def fetch_pautas_list():
    """Retorna lista de items das Pautas/Atas via Liferay legacy.

    NOTA: Liferay ANEEL tem blacklist + rate-limit. Pode falhar mesmo de IP
    residencial. Se falhar, retorna [] (sem erro). O script tenta reprocessar
    items pendentes do histórico na mesma run.
    """
    r = _liferay_get(PAUTAS_URL)
    if not r:
        print("[pautas_list] todos os retries falharam (Liferay bloqueando)", file=sys.stderr)
        return []

    items = []
    # Estrutura: <tr><td>DD/MM/YYYY</td><td>...idNoticia=N...título</a></td></tr>
    for tr in re.findall(r"<tr>(.*?)</tr>", r.text, re.DOTALL):
        nid = re.search(r"idNoticia=(\d+)", tr)
        title = re.search(r'titulo_noticias[^>]*>([^<]+)</a>', tr)
        date = re.search(r"(\d{2}/\d{2}/\d{4})", tr)
        if not (nid and title):
            continue
        items.append({
            "type": "pauta_rd",
            "id": f"pauta_{nid.group(1)}",
            "title": title.group(1).strip(),
            "date": date.group(1) if date else None,
            "link": PAUTA_DETAIL_TPL.format(id=nid.group(1)),
        })
    return items


# ============== DETALHES DE CADA ITEM ==============

def fetch_news_detail(url):
    """Pega o corpo do artigo da Sala de Imprensa.
    Retorna (date_str ou None, body_text)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return None, ""
    except Exception:
        return None, ""

    html = r.text
    # Data preferencial: JSON-LD datePublished (sempre acurado, padrão Plone gov.br)
    # Fallback: Publicado: DD/MM/YYYY HHhMM
    date = None
    m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html)
    if m:
        iso = m.group(1)
        d = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso)
        if d:
            date = f"{d.group(3)}/{d.group(2)}/{d.group(1)}"
    if not date:
        m = re.search(r"(\d{2}/\d{2}/\d{4})\s+\d{2}h\d{2}", html)
        if m:
            date = m.group(1)

    body = trafilatura.extract(r.content, favor_recall=True, include_comments=False)
    return date, (body or "")[:8000]


def fetch_pauta_detail(url):
    """Pega o corpo da página da pauta. Usa retry com múltiplas impersonations."""
    r = _liferay_get(url)
    if not r:
        return ""
    body = trafilatura.extract(r.content, favor_recall=True, include_comments=False)
    return (body or "")[:8000]


# ============== PARTICIPAÇÃO PÚBLICA (CP/AP/TS) ==============

def fetch_partic_list():
    """Retorna lista de Consultas/Audiências Públicas + Tomadas de Subsídios
    abertas, parsing da tabela do portlet Liferay antigo.aneel.gov.br.

    Estrutura de cada bloco:
      'Consulta NNN/AAAA Objeto - texto descritivo...'
      'Audiência NNN/AAAA Objeto - texto...'
      'Tomada NNN/AAAA Objeto - texto...'
    """
    import html as _html_mod
    items = []
    for kind, url, prefix in PARTIC_URLS:
        r = _liferay_get(url)
        if not r:
            continue
        html_text = _html_mod.unescape(r.text)
        # Extrai conteúdo dentro da única <table>
        tables = re.findall(r"<table[^>]*>(.*?)</table>", html_text, re.DOTALL)
        if not tables:
            continue
        # Limpa tags
        clean = re.sub(r"<[^>]+>", " ", tables[0])
        clean = re.sub(r"\s+", " ", clean).strip()
        # Divide por "Consulta NNN/AAAA", "Audiência NNN/AAAA", "Tomada NNN/AAAA"
        blocks = re.split(rf"(?={prefix}\s+\d+/\d+\s)", clean)
        for blk in blocks:
            blk = blk.strip()
            if not blk or not blk.startswith(prefix):
                continue
            m = re.match(rf"{prefix}\s+(\d+/\d+)\s+(?:Objeto\s*-\s*)?(.*)", blk, re.IGNORECASE | re.DOTALL)
            if not m:
                continue
            num = m.group(1)
            objeto = re.sub(r"\s+", " ", m.group(2)).strip()
            if len(objeto) < 20:
                continue  # provavelmente lixo
            # Corta no próximo separador típico ("ATENÇÃO:" ou "Período de Contribuições:")
            objeto = re.split(r"\s+ATEN[ÇC][AÃ]O\s*:|\s+Per[íi]odo de Contribui[çc][õo]es\s*:", objeto)[0].strip()
            objeto = objeto[:2000]
            items.append({
                "type": f"partic_{kind}",
                "id": f"partic_{kind}_{num.replace('/', '_')}",
                "title": f"{prefix} Pública nº {num}",
                "date": None,
                "link": url,  # link da listagem (detalhe é Liferay portlet, difícil de linkar direto)
                "objeto": objeto,
            })
    return items


# ============== SUMMARIZE ==============

def summarize(title, body, kind):
    """Gemini Flash gera resumo. Retorna None em erro.
    Pra type=partic_cp/ap/ts, o body JÁ é o objeto extraído (texto curto),
    então usa prompt direto."""
    client = _gemini_client()
    if not client:
        return None
    if not body or len(body) < 80:
        return None
    try:
        from google.genai import types
        if kind.startswith("partic_"):
            partic_label = {"partic_cp": "Consulta Pública", "partic_ap": "Audiência Pública", "partic_ts": "Tomada de Subsídios"}.get(kind, "Participação Pública")
            prompt = (
                f"Você é um analista do setor brasileiro de utilities resumindo uma {partic_label} da ANEEL.\n"
                "REGRAS:\n"
                "- 1-2 frases curtas em PORTUGUÊS BRASILEIRO.\n"
                "- Vá direto ao tema: o QUE está sendo consultado/auditado.\n"
                "- Use **bold** em empresas, processos, valores, datas.\n"
                "- NÃO repita 'Consulta Pública nº X' no início — vai direto pro tema.\n\n"
                f"Texto do Objeto: {body}\n\n"
                "Resumo (1-2 frases):"
            )
        elif kind == "pauta_rd":
            prompt = (
                "Você é um analista do setor brasileiro de utilities resumindo a pauta de uma "
                "Reunião Pública da Diretoria da ANEEL.\n\n"
                "REGRAS RÍGIDAS:\n"
                "- NÃO comece com 'Aqui estão os itens mais relevantes' nem similar.\n"
                "- NÃO repita o título da pauta. NÃO repita o número da reunião.\n"
                "- Vá DIRETO para a lista de 3-5 itens mais importantes da agenda.\n"
                "- Use bullets curtos: '- **Tema/empresa**: ação + processo/valor.'\n"
                "- Use **bold** em empresas, valores R$, processos nº NNN, leilões.\n"
                "- PT-BR. Cada bullet com no máximo 1 linha.\n"
                "- Inclua só itens com relevância p/ utilities elétricas (tarifa, leilão, "
                "transmissão, geração, distribuição, concessão, fiscalização, regulação).\n\n"
                f"Título: {title}\n\n"
                f"Pauta:\n{body}\n\n"
                "Bullets (3-5):"
            )
        else:
            prompt = (
                "Você é um analista do setor brasileiro de utilities resumindo um comunicado "
                "oficial da ANEEL. Em 2-3 frases curtas em PORTUGUÊS BRASILEIRO, diga:\n"
                "- O QUE foi anunciado/decidido (fato principal).\n"
                "- QUEM está envolvido (empresas, órgãos, regiões).\n"
                "- VALORES, DATAS, números ou prazos.\n"
                "Use **bold** pra destacar empresas e valores. PULE introduções vagas.\n\n"
                f"Título: {title}\n\n"
                f"Texto:\n{body}\n\n"
                "Resumo:"
            )
        for attempt in range(4):
            try:
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
                err = str(e)
                if "503" in err or "UNAVAILABLE" in err:
                    if attempt < 3:
                        time.sleep(10 * (attempt + 1))
                        continue
                print(f"[gemini] erro: {e}", file=sys.stderr)
                return None
        return None
    except Exception as e:
        print(f"[summarize] erro: {e}", file=sys.stderr)
        return None


# ============== MAIN ==============

def _save(history):
    history["last_updated"] = datetime.now().isoformat()
    HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main():
    if not _gemini_client():
        print("[aneel_aux] GEMINI_API_KEY não setada — vou coletar mas não resumir", file=sys.stderr)

    # Carrega histórico
    if HISTORY_FILE.exists():
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    else:
        history = {"last_updated": None, "items": {}}
    # Skipa items que já têm summary (no_article/summarize_failed continuam reprocessáveis)
    seen_ids = {k for k, v in history["items"].items() if v.get("summary")}

    # Coleta listings
    print("[aneel_aux] Fetching news + pautas + participação pública…", file=sys.stderr)
    news = fetch_news_list()
    pautas = fetch_pautas_list()
    partic = fetch_partic_list()
    print(f"  News: {len(news)} items", file=sys.stderr)
    print(f"  Pautas: {len(pautas)} items", file=sys.stderr)
    print(f"  Participação Pública (CP/AP/TS): {len(partic)} items", file=sys.stderr)
    all_items = news + pautas + partic

    # Retry: items do histórico SEM summary que sumiram da listagem atual
    # (ex: Liferay deu 403 transitório → as pautas saíram da lista mas estão no JSON)
    current_ids = {it["id"] for it in all_items}
    retry_count = 0
    for k, v in history.get("items", {}).items():
        if k in current_ids or v.get("summary"):
            continue
        item_redo = {
            "type": v.get("type"),
            "id": k,
            "title": v.get("title"),
            "date": v.get("date"),
            "link": v.get("link"),
        }
        if v.get("objeto"):
            item_redo["objeto"] = v["objeto"]
        all_items.append(item_redo)
        retry_count += 1
    if retry_count:
        print(f"  Retry de histórico: +{retry_count} items sem summary", file=sys.stderr)

    # Filtra pra processar só novos
    pending = [it for it in all_items if it["id"] not in seen_ids]
    pending = pending[:MAX_SUMMARIZE_PER_RUN]
    print(f"[aneel_aux] {len(pending)} pendentes (cap {MAX_SUMMARIZE_PER_RUN})", file=sys.stderr)

    done = 0
    failed = 0
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 15
    SAVE_EVERY = 20

    for i, item in enumerate(pending, 1):
        try:
            if item["type"] == "pauta_rd":
                body = fetch_pauta_detail(item["link"])
                date = item.get("date")
            elif item["type"].startswith("partic_"):
                # Pra CP/AP/TS, o "body" é o objeto já extraído do listing
                body = item.get("objeto", "")
                date = item.get("date")
            else:
                date, body = fetch_news_detail(item["link"])

            summary = summarize(item["title"], body, item["type"]) if body else None

            entry = {
                "type": item["type"],
                "title": item["title"],
                "date": date or item.get("date"),
                "link": item["link"],
                "summary": summary,
                "added_at": datetime.now().isoformat(),
            }
            # Preserva objeto bruto pra items partic (caso reprocesso futuro)
            if item.get("objeto"):
                entry["objeto"] = item["objeto"]
            if not body:
                entry["error"] = "no_body"
            elif not summary:
                entry["error"] = "summarize_failed"

            history["items"][item["id"]] = entry

            if summary:
                done += 1
                consecutive_failures = 0
                print(f"  ✓ [{i}/{len(pending)}] ({item['type']}) {summary[:80]}", file=sys.stderr)
            else:
                failed += 1
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(f"[aneel_aux] {MAX_CONSECUTIVE_FAILURES} falhas consecutivas — parando", file=sys.stderr)
                    break
        except Exception as e:
            print(f"  ✗ [{i}/{len(pending)}] EXC: {e}", file=sys.stderr)
            failed += 1
            consecutive_failures += 1

        if i % SAVE_EVERY == 0:
            _save(history)
            print(f"[aneel_aux] checkpoint salvo ({done} ok)", file=sys.stderr)
        time.sleep(0.5)

    # Prune retention
    cutoff = (datetime.now() - timedelta(days=HISTORY_RETENTION_DAYS)).isoformat()
    before = len(history["items"])
    history["items"] = {
        k: v for k, v in history["items"].items()
        if v.get("added_at", "") >= cutoff
    }
    pruned = before - len(history["items"])

    _save(history)
    print(f"[aneel_aux] DONE: {done} novos summaries, {failed} fails, {pruned} podados", file=sys.stderr)

    # Diagnóstico
    DIAGNOSTIC_FILE.write_text(json.dumps({
        "last_run": datetime.now().isoformat(),
        "news_count": len(news),
        "pautas_count": len(pautas),
        "pending": len(pending),
        "done": done,
        "failed": failed,
        "pruned": pruned,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
