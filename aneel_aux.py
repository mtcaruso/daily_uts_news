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

import gemini_util

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

# MME — API REST oficial em consultas-publicas.mme.gov.br
MME_CP_API_URL = (
    "https://consultas-publicas.mme.gov.br/consulta-publica/v1/public/"
    "listagem-sem-filtros?pageNumber=0&pageSize=200&sortBy=id&sortDirection=desc"
)
MME_CP_DETAIL_TPL = "https://consultas-publicas.mme.gov.br/home/consulta/{id}"

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

def _fetch_partic_detail(session, detail_url):
    """Visita página de detalhe da CP/AP/TS no Liferay e extrai
    período de contribuição (start_date, end_date) em formato DD/MM/YYYY.
    Retorna (start, end) ou (None, None) se não encontrar."""
    try:
        r = session.get(detail_url, timeout=30)
        if r.status_code != 200:
            return None, None
    except Exception:
        return None, None
    html_text = r.text
    clean = re.sub(r"<[^>]+>", " ", html_text)
    clean = re.sub(r"\s+", " ", clean).strip()
    # Padrão típico: "Período de contribuição De DD/MM/AAAA a DD/MM/AAAA"
    m = re.search(
        r"Per[íi]odo\s+de\s+contribui[çc][ãa]o\s+De\s+(\d{1,2}/\d{1,2}/\d{4})\s+a\s+(\d{1,2}/\d{1,2}/\d{4})",
        clean, re.IGNORECASE,
    )
    if m:
        def _pad(d):
            parts = d.split("/")
            return f"{parts[0].zfill(2)}/{parts[1].zfill(2)}/{parts[2]}"
        return _pad(m.group(1)), _pad(m.group(2))
    return None, None


def fetch_partic_list():
    """Retorna lista de Consultas/Audiências Públicas + Tomadas de Subsídios
    abertas. Pra cada item, visita a página de detalhe e extrai período exato
    (start + deadline).
    """
    import html as _html_mod
    items = []
    for kind, url, prefix in PARTIC_URLS:
        r = _liferay_get(url)
        if not r:
            continue
        html_text = _html_mod.unescape(r.text)

        # Captura também os links de detalhe (em ordem da tabela)
        m_tab = re.search(r"<table[^>]*>(.*?)</table>", html_text, re.DOTALL)
        if not m_tab:
            continue
        table_html = m_tab.group(1)
        detail_links = re.findall(r'<a[^>]+href="([^"]+ideParticipacaoPublica=\d+[^"]+)"', html_text)
        # Texto limpo pros blocos
        clean = re.sub(r"<[^>]+>", " ", table_html)
        clean = re.sub(r"\s+", " ", clean).strip()
        blocks = re.split(rf"(?={prefix}\s+\d+/\d+\s)", clean)

        # Cria sessão pra reusar p_auth do listing
        import curl_cffi.requests as _cf
        sess = _cf.Session(impersonate="chrome120")
        # Hit listing pra setar cookies
        sess.get(url, timeout=30)
        time.sleep(0.5)

        block_idx = 0
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
                continue
            objeto = re.split(r"\s+ATEN[ÇC][AÃ]O\s*:|\s+Per[íi]odo de Contribui[çc][õo]es\s*:", objeto)[0].strip()
            objeto = objeto[:2000]

            # Pega link de detalhe correspondente (em ordem)
            detail_url = detail_links[block_idx] if block_idx < len(detail_links) else None
            block_idx += 1

            # Visita detalhe pra extrair período exato
            start_date, deadline = None, None
            if detail_url:
                detail_url_clean = _html_mod.unescape(detail_url)
                start_date, deadline = _fetch_partic_detail(sess, detail_url_clean)
                time.sleep(0.8)  # gentle ao Liferay

            # Fallback: regex no texto do bloco se detail falhou
            if not deadline:
                deadline = _extract_partic_deadline(blk)

            items.append({
                "type": f"partic_{kind}",
                "id": f"partic_{kind}_{num.replace('/', '_')}",
                "title": f"{prefix} Pública nº {num}",
                "date": None,
                "link": detail_url_clean if detail_url else url,  # link agora aponta pro detalhe
                "objeto": objeto,
                "start_date": start_date,
                "deadline": deadline,
            })
    return items


def fetch_mme_partic_list():
    """Coleta CPs do MME via API REST oficial (consultas-publicas.mme.gov.br).

    API retorna JSON estruturado com id, titulo, dtInicio, dtFim, status, etc.
    Filtra só status=ABERTA.
    """
    items = []
    try:
        import curl_cffi.requests as _cf
        headers = {
            "sistema": "CONSULTA-PUBLICA",
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "referer": "https://consultas-publicas.mme.gov.br/home",
        }
        r = _cf.post(MME_CP_API_URL, headers=headers, json={}, impersonate="chrome120", timeout=30)
        if r.status_code != 200:
            print(f"[mme_partic] HTTP {r.status_code}", file=sys.stderr)
            return []
        data = r.json()
    except Exception as e:
        print(f"[mme_partic] {e}", file=sys.stderr)
        return []

    raw_items = data.get("content", [])
    print(f"  [MME CPs] {len(raw_items)} retornadas, filtrando ABERTAs…", file=sys.stderr)

    for it in raw_items:
        if it.get("status") != "ABERTA":
            continue
        if it.get("isDeleted"):
            continue
        cp_id = it.get("id")
        if not cp_id:
            continue
        titulo = (it.get("titulo") or "").strip()
        assunto = (it.get("assuntoResumido") or "").strip()
        # Datas vêm já em DD/MM/YYYY (string)
        dt_inicio = it.get("dtInicio") or it.get("dtPublicadoDou")
        dt_fim = it.get("dtFim")
        sei_ref = it.get("responsavelSei")

        objeto = assunto or titulo
        items.append({
            "type": "partic_mme_cp",
            "id": f"partic_mme_cp_{cp_id}",
            "title": f"Consulta Pública MME nº {cp_id}",
            "date": None,
            "link": MME_CP_DETAIL_TPL.format(id=cp_id),
            "objeto": objeto[:2000],
            "start_date": dt_inicio,
            "deadline": dt_fim,
            "fonte": "MME",
            "sei_processo": sei_ref,
        })
    return items


def _extract_partic_deadline(text: str) -> str:
    """Procura deadline (data final pra envio) no texto do objeto.
    Padrões aceitos:
      - 'até as 23h59 do dia DD/M/YYYY'
      - 'até DD/M/YYYY'
      - 'prazo ... DD/M/YYYY'
      - 'encerra ... DD/M/YYYY'
    Retorna formato DD/MM/YYYY (zero-padded) ou None.
    """
    if not text:
        return None
    patterns = [
        r"at[ée]\s+(?:as\s+)?\d{1,2}h\d{2}\s+do\s+dia\s+(\d{1,2}/\d{1,2}/\d{4})",
        r"at[ée]\s+o\s+dia\s+(\d{1,2}/\d{1,2}/\d{4})",
        r"prazo\s+(?:final|m[áa]ximo|de\s+envio)?[^.]*?(\d{1,2}/\d{1,2}/\d{4})",
        r"(?:final|t[ée]rmino|encerra(?:mento)?)[^.]*?(\d{1,2}/\d{1,2}/\d{4})",
        r"contribui[çc][õo]es[^.]*?(\d{1,2}/\d{1,2}/\d{4})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            d = m.group(1)
            # Normaliza pra DD/MM/YYYY zero-padded
            parts = d.split("/")
            if len(parts) == 3:
                return f"{parts[0].zfill(2)}/{parts[1].zfill(2)}/{parts[2]}"
    return None


# ============== SUMMARIZE ==============

def summarize(title, body, kind):
    """Gemini Flash gera resumo. Retorna None em erro.
    Pra type=partic_cp/ap/ts, o body JÁ é o objeto extraído (texto curto),
    então usa prompt direto. Levanta GeminiCircuitOpen se créditos/quota
    esgotarem (caller aplica fallback)."""
    if not body or len(body) < 80:
        return None
    try:
        if kind.startswith("partic_"):
            prompt = (
                "Você é um analista do setor brasileiro de utilities resumindo o tema "
                "de uma Consulta/Audiência Pública ou Tomada de Subsídios da ANEEL.\n\n"
                "REGRAS RÍGIDAS:\n"
                "- 1-2 frases curtas em PT-BR.\n"
                "- COMECE com o TEMA/AÇÃO direto. NUNCA comece com:\n"
                "  - 'A ANEEL busca/pretende/visa...'\n"
                "  - 'A consulta visa/busca...'\n"
                "  - 'Obter subsídios para...'\n"
                "- Comece com substantivo do tema OU verbo direto. Exemplos VÁLIDOS:\n"
                "  - 'Regulamenta cadastro de representantes... no Submódulo 1.4.'\n"
                "  - 'Edital do Leilão nº 4/2026 — ajustes em prazos e habilitação técnica.'\n"
                "  - 'Revisão tarifária da **Energisa Sul-Sudeste (ESS)** — proposta para vigor em 12/07/2026.'\n"
                "  - 'Tratamento regulatório de créditos **MMGD** conforme Lei 14.300/2022.'\n"
                "- Use **bold** em empresas, valores, processos nº, leis, datas.\n\n"
                f"Texto do Objeto: {body}\n\n"
                "Resumo (1-2 frases, sem filler inicial):"
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
        # gemini_util cuida de throttle/backoff/circuit-breaker (free tier).
        # Propaga GeminiCircuitOpen pro caller aplicar fallback extrativo.
        return gemini_util.generate(prompt, max_output_tokens=600)
    except gemini_util.GeminiCircuitOpen:
        raise
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
    # "Seen" = tem summary LLM. Extrativos (fallback de Gemini indisponível) são
    # re-resumidos com LLM quando créditos/quota voltam.
    seen_ids = {
        k for k, v in history["items"].items()
        if v.get("summary") and v.get("summary_source") != "extractive"
    }

    # Coleta listings — em PARALELO: são 4 hosts independentes (gov.br/aneel,
    # www2.aneel Liferay, antigo.aneel Liferay, API MME). Cada fetch tem seus
    # próprios retries/sleeps internos (que continuam valendo POR host, então
    # não aumenta pressão em nenhum servidor). Sequencial somava ~10-20s/run.
    print("[aneel_aux] Fetching news + pautas + participação pública (ANEEL + MME)…", file=sys.stderr)
    from concurrent.futures import ThreadPoolExecutor

    def _safe(fn):
        try:
            return fn()
        except Exception as e:
            print(f"  [{fn.__name__}] EXC: {e}", file=sys.stderr)
            return []

    with ThreadPoolExecutor(max_workers=4) as _ex:
        _f_news = _ex.submit(_safe, fetch_news_list)
        _f_pautas = _ex.submit(_safe, fetch_pautas_list)
        _f_partic = _ex.submit(_safe, fetch_partic_list)
        _f_mme = _ex.submit(_safe, fetch_mme_partic_list)
        news = _f_news.result()
        pautas = _f_pautas.result()
        partic = _f_partic.result()
        partic_mme = _f_mme.result()
    print(f"  News: {len(news)} items", file=sys.stderr)
    print(f"  Pautas: {len(pautas)} items", file=sys.stderr)
    print(f"  Participação Pública ANEEL (CP/AP/TS): {len(partic)} items", file=sys.stderr)
    print(f"  Participação Pública MME (CPs ABERTAs): {len(partic_mme)} items", file=sys.stderr)
    # Marca fonte ANEEL nos items que não vieram do MME (pra compat com items antigos)
    for it in partic:
        it.setdefault("fonte", "ANEEL")
    all_items = news + pautas + partic + partic_mme

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

            summary_source = "llm"
            try:
                summary = summarize(item["title"], body, item["type"]) if body else None
            except gemini_util.GeminiCircuitOpen:
                # Gemini indisponível (créditos/quota) — degrada pra extrativo
                summary = gemini_util.extractive_summary(body) if body else None
                summary_source = "extractive" if summary else "llm"

            # Fallback pra partic_* (CP/AP/TS): se o Gemini não resumiu (objeto
            # curto demais, < 80 chars) mas o objeto já é uma descrição completa,
            # usa o próprio objeto como resumo. Evita falso "summarize_failed"
            # em CPs com objeto enxuto (ex: "minuta do Plano Nacional de
            # Transição Energética - PLANTE").
            if not summary and item["type"].startswith("partic_") and body:
                objeto_clean = " ".join(body.split()).strip()
                if 15 <= len(objeto_clean) <= 500:
                    summary = objeto_clean

            entry = {
                "type": item["type"],
                "title": item["title"],
                "date": date or item.get("date"),
                "link": item["link"],
                "summary": summary,
                "added_at": datetime.now().isoformat(),
            }
            if summary_source == "extractive":
                entry["summary_source"] = "extractive"  # re-resume com LLM depois
            # Preserva objeto bruto pra items partic (caso reprocesso futuro)
            if item.get("objeto"):
                entry["objeto"] = item["objeto"]
            if item.get("deadline"):
                entry["deadline"] = item["deadline"]
            if item.get("start_date"):
                entry["start_date"] = item["start_date"]
            if item.get("fonte"):
                entry["fonte"] = item["fonte"]
            if item.get("sei_processo"):
                entry["sei_processo"] = item["sei_processo"]
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
