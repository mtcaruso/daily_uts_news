"""Ranking heurístico de notícias — score composto com breakdown explicável.

Signals (peso entre parênteses):
  - cross_source_count   (30) — número de fontes que cobriram a mesma matéria
  - watchlist_hits       (25) — número de termos da watchlist que batem
  - regulatory_overlap   (20) — empresas citadas que também aparecem em DOU/Pauta/CP
  - financial_signal     (15) — menciona R$ em milhões/bilhões/trilhões
  - high_kw_in_title     (10) — palavras-chave de alto sinal no título (leilão, aquisição, etc)
  - recency_decay        (10) — fator de decaimento por hora (decay exponencial 24h)

Cada notícia recebe um score 0-110+ aproximadamente. Breakdown explica o porquê.
"""
import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Iterable, List

# Pesos dos signals (somam ~110 no caso máximo típico)
WEIGHTS = {
    "cross_source": 30,
    "watchlist": 25,
    "regulatory": 20,
    "financial": 15,
    "high_kw": 10,
    "recency": 10,
}

# Keywords de alto sinal no TÍTULO (ordem ~não importa)
HIGH_SIGNAL_KEYWORDS = [
    "leilão", "leilao",
    "aquisição", "aquisicao", "comprar", "adquire",
    "fusão", "fusao", "incorporação", "incorporacao",
    "fato relevante", "comunicado ao mercado",
    "ipo", "oferta pública", "oferta publica", "follow-on",
    "debênture", "debenture",
    "tarifa", "reajuste", "revisão tarifária", "revisao tarifaria",
    "decreto", "portaria", "resolução normativa", "resolucao normativa",
    "homologa", "aprova", "rejeita", "suspende",
    "leilão de transmissão", "leilao de transmissao",
    "leilão de capacidade", "leilao de capacidade", "lrcap",
    "privatização", "privatizacao",
    "concessão", "concessao",
    "lrcap",
    "fundo", "investimento",
]

# Regex pra captura de valores financeiros
_MONEY_RE = re.compile(
    r"R\$\s*([\d.,]+)\s*(bilh|bi\b|trilh|tri\b|milh)",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    """Normaliza pra match sem acento + lowercase."""
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower()


def normalize_title_for_dedupe(title: str) -> str:
    """Mesma normalização do build_title_index no news_summaries."""
    if not title:
        return ""
    s = title.lower()
    s = re.split(r"\s+[\-|–—]\s+", s)[0]
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def build_cross_source_index(news_items: List[dict]) -> dict:
    """Constrói dict {normalized_title: count} pra calcular cross_source rápido."""
    counts = {}
    for it in news_items:
        norm = normalize_title_for_dedupe(it.get("title") or "")
        if norm:
            counts[norm] = counts.get(norm, 0) + 1
    return counts


# ---- Clustering de near-duplicates (1 card por história no Top do Dia) ----
# Stopwords PT (+ alguns EN) — removidas antes de medir similaridade, pra a
# comparação valer pelas palavras de CONTEÚDO (empresa, ação, valor), não por
# "de/da/o/que" que toda manchete tem.
_PT_STOP = {
    "a", "o", "as", "os", "um", "uma", "uns", "umas", "de", "da", "do", "das",
    "dos", "e", "em", "no", "na", "nos", "nas", "para", "pra", "por", "com",
    "sem", "sob", "sobre", "ao", "aos", "que", "se", "ja", "mais", "menos",
    "ser", "ter", "tem", "vai", "vao", "apos", "ate", "como", "entre", "seu",
    "sua", "seus", "suas", "the", "of", "to", "in", "on", "for", "and", "is",
}


# Palavras que NÃO discriminam uma história (não contam como "entidade"):
# reguladores/órgãos genéricos, lugares, termos de domínio comuns, palavras de
# seção/editoriais e adjetivos de manchete. Sem isso, "energia" (110 títulos!)
# ou um "Novas/Pela" de início de frase colariam histórias distintas.
_GENERIC_ENTITIES = {
    # reguladores / órgãos / mercados genéricos
    "aneel", "ons", "mme", "mpf", "epe", "bndes", "stf", "stj", "tcu", "cvm",
    "tjsp", "alesp", "ana", "cade", "cmse", "anp", "governo", "justica",
    "uniao", "congresso", "camara", "senado", "ministerio", "ministro",
    # lugares
    "brasil", "sao", "paulo", "rio", "janeiro", "minas", "gerais", "ceara",
    "bahia", "pecem", "amazonia", "para", "sul", "norte", "nordeste",
    "sudeste", "centro", "chile", "argentina", "india", "turquia", "eua",
    "venezuela", "irã", "ira", "europa", "china",
    # termos de domínio comuns demais
    "energia", "energetica", "eletrico", "eletrica", "eletricidade", "setor",
    "geracao", "transmissao", "distribuidoras", "distribuidora", "baterias",
    "bateria", "solar", "eolica", "usinas", "usina", "tarifa", "tarifaria",
    "tarifario", "gas", "reservatorios", "regiao", "preco", "precos", "corte",
    "cortes", "leilao", "mercado", "renovaveis", "renovavel", "hidreletrica",
    # seção / editorial / adjetivos de manchete
    "painel", "exclusivo", "agenda", "analise", "coluna", "entrevista", "giro",
    "veja", "saiba", "entenda", "ultimas", "empresas", "politica", "opiniao",
    "balanco", "novas", "novos", "nova", "novo", "primeiro", "primeira", "tag",
}


def _content_tokens(title: str) -> set:
    """Tokens de conteúdo do título (lower, sem pontuação, sem stopword)."""
    norm = normalize_title_for_dedupe(title)
    return {t for t in norm.split() if len(t) > 2 and t not in _PT_STOP}


def _entities(title: str) -> set:
    """Nomes próprios do título (palavras Capitalizadas — empresas, ativos),
    fora stopwords e entidades genéricas demais. São o que discrimina a HISTÓRIA:
    'Engie'+'Jirau' juntam paráfrases; 'CPFL'≠'Cemig' separam empresas.

    CRÍTICO: tira o sufixo da FONTE ("... - Valor Econômico", "- Agência iNFRA")
    antes — senão 'Valor'/'Econômico' viram entidade e TODO artigo da mesma fonte
    se funde (bug do cluster gigante)."""
    head = re.split(r"\s+[\-|–—]\s+", title or "")[0]
    ents = set()
    for w in re.findall(r"[A-ZÀ-Þ][A-Za-zÀ-ÿ0-9]{2,}", head):
        wn = _norm(w)
        if wn and wn not in _PT_STOP and wn not in _GENERIC_ENTITIES:
            ents.add(wn)
    return ents


def _same_story(a_toks: set, b_toks: set, a_ents: set, b_ents: set) -> bool:
    """True se dois títulos cobrem a MESMA história.
    1) Compartilham ≥2 ENTIDADES (mesma empresa+ativo) → mesma história, mesmo
       que as manchetes (de fontes diferentes) usem palavras bem distintas.
    2) Se AMBOS têm entidades mas NÃO compartilham nenhuma → empresas diferentes,
       NÃO junta (evita colar 'reajuste CPFL' com 'reajuste Cemig').
    3) Fallback lexical pra dupes quase idênticos sem entidades claras."""
    shared_ents = a_ents & b_ents
    if len(shared_ents) >= 2:
        return True
    if a_ents and b_ents and not shared_ents:
        return False
    # Fallback lexical ESTRITO — só pra dupes quase idênticos sem entidades
    # claras (ex.: títulos repetidos, relatórios de série). Estrito de propósito:
    # o fallback frouxo colava manchetes genéricas que só dividiam 'energia'.
    if not a_toks or not b_toks:
        return False
    inter = a_toks & b_toks
    if len(inter) < 3:
        return False
    overlap = len(inter) / min(len(a_toks), len(b_toks))
    return overlap >= 0.7


def _cluster_items(items: List[dict]) -> List[List[dict]]:
    """Agrupa items que são a mesma história (greedy, compara com o seed de cada
    cluster pra não derivar). Retorna lista de clusters (cada um lista de items)."""
    clusters = []  # cada: {"toks": set, "ents": set, "items": [...]}
    for it in items:
        title = it.get("title") or ""
        toks = _content_tokens(title)
        ents = _entities(title)
        placed = False
        if toks or ents:
            for c in clusters:
                if _same_story(toks, c["toks"], ents, c["ents"]):
                    c["items"].append(it)
                    placed = True
                    break
        if not placed:
            clusters.append({"toks": toks, "ents": ents, "items": [it]})
    return [c["items"] for c in clusters]


def _high_kw_matches(title: str) -> List[str]:
    """Quais keywords de alto sinal aparecem no título."""
    t_norm = _norm(title)
    hits = []
    for kw in HIGH_SIGNAL_KEYWORDS:
        kw_norm = _norm(kw)
        if kw_norm in t_norm and kw not in hits:
            hits.append(kw)
    return hits


def _financial_signal(text: str) -> tuple:
    """(score, descrição). Score 0-15.
    R$ X bilhões = 15, X milhões >= 100 = 12, milhões < 100 = 8, qualquer R$ = 5
    """
    if not text:
        return 0, ""
    matches = _MONEY_RE.findall(text)
    if not matches:
        return 0, ""
    # Pega o maior valor mencionado
    best_score = 0
    best_label = ""
    for val_str, unit in matches:
        try:
            val = float(val_str.replace(".", "").replace(",", "."))
        except Exception:
            val = 1.0
        unit_norm = unit.lower()
        if "tri" in unit_norm:
            score = 15
            label = f"R$ {val_str} tri"
        elif "bi" in unit_norm:
            score = 15
            label = f"R$ {val_str} bi"
        elif "milh" in unit_norm:
            if val >= 100:
                score = 12
                label = f"R$ {val_str} mi"
            else:
                score = 8
                label = f"R$ {val_str} mi"
        else:
            score = 5
            label = f"R$ {val_str}"
        if score > best_score:
            best_score = score
            best_label = label
    return best_score, best_label


def _recency_factor(published_dt, now=None) -> float:
    """Decay exponencial: 1.0 nas últimas 6h, decay ao longo de 24h.
    Retorna fator 0-1 (depois multiplicado por peso)."""
    if not published_dt:
        return 0.0
    if now is None:
        now = datetime.now(timezone.utc)
    if published_dt.tzinfo is None:
        published_dt = published_dt.replace(tzinfo=timezone.utc)
    hours = (now - published_dt).total_seconds() / 3600
    if hours < 0:
        return 1.0
    if hours <= 6:
        return 1.0
    if hours <= 24:
        return 0.7
    if hours <= 48:
        return 0.4
    if hours <= 168:  # 7 dias
        return 0.2
    return 0.0


def _watchlist_hits(text: str, watch_terms: List[str]) -> List[str]:
    """Quais termos do watchlist aparecem no texto."""
    if not watch_terms or not text:
        return []
    t_norm = _norm(text)
    hits = []
    for term in watch_terms:
        term_norm = _norm(term)
        if term_norm and term_norm in t_norm and term not in hits:
            hits.append(term)
    return hits


def _regulatory_overlap(text: str, regulatory_entities: set) -> List[str]:
    """Quais entidades (empresas extraídas de DOU/Pauta/CP) aparecem no texto.
    regulatory_entities é um set de strings normalizadas."""
    if not regulatory_entities or not text:
        return []
    t_norm = _norm(text)
    hits = []
    for ent in regulatory_entities:
        if ent and ent in t_norm and ent not in hits:
            hits.append(ent)
    return hits[:5]  # cap


def score_news_item(
    item: dict,
    n_sources: int,
    watch_terms: Iterable[str],
    regulatory_entities: set,
    now=None,
) -> dict:
    """Score 0-110+ + breakdown explicativo.

    n_sources = nº de fontes distintas que cobriram a matéria (vem do cluster
    de near-duplicates em rank_news).

    Retorna dict com:
      - total: score numérico
      - breakdown: lista de tuples (label, peso_aplicado, descrição curta)
    """
    title = item.get("title") or ""
    summary = item.get("summary") or ""
    full = f"{title} {summary}"

    # 1. Cross-source (nº de fontes distintas cobrindo a mesma história)
    cross_score = min(n_sources, 5) / 5 * WEIGHTS["cross_source"]  # cap em 5 fontes

    # 2. Watchlist
    wl_hits = _watchlist_hits(full, list(watch_terms))
    wl_score = min(len(wl_hits), 3) / 3 * WEIGHTS["watchlist"]  # cap em 3 hits

    # 3. Regulatory overlap
    reg_hits = _regulatory_overlap(full, regulatory_entities)
    reg_score = min(len(reg_hits), 2) / 2 * WEIGHTS["regulatory"]

    # 4. Financial signal
    fin_score, fin_label = _financial_signal(full)

    # 5. High keyword in title
    kw_hits = _high_kw_matches(title)
    kw_score = min(len(kw_hits), 2) / 2 * WEIGHTS["high_kw"]

    # 6. Recency
    rec_factor = _recency_factor(item.get("published"), now=now)
    rec_score = rec_factor * WEIGHTS["recency"]

    total = cross_score + wl_score + reg_score + fin_score + kw_score + rec_score

    breakdown = []
    if n_sources > 1:
        breakdown.append(("📰", round(cross_score, 1), f"{n_sources} fontes"))
    if wl_hits:
        breakdown.append(("⭐", round(wl_score, 1), "watchlist: " + ", ".join(wl_hits[:3])))
    if reg_hits:
        breakdown.append(("🏛️", round(reg_score, 1), "regulatório: " + ", ".join(reg_hits[:3])))
    if fin_score > 0:
        breakdown.append(("💰", round(fin_score, 1), fin_label))
    if kw_hits:
        breakdown.append(("🔑", round(kw_score, 1), ", ".join(kw_hits[:3])))
    if rec_factor > 0.5:
        breakdown.append(("🕐", round(rec_score, 1), "recente"))

    return {
        "total": round(total, 1),
        "breakdown": breakdown,
    }


def rank_news(
    news_items: List[dict],
    watch_terms: Iterable[str],
    regulatory_entities: Iterable[str] = (),
    top_n: int = 10,
    min_score: float = 0,
    diversify: bool = True,
) -> List[dict]:
    """Recebe lista de items (cada um com title, summary, published, source).
    Retorna top N com score + breakdown anexados.

    diversify=True (padrão): agrupa near-duplicates (2-4 matérias sobre a MESMA
    coisa) e mostra só a MELHOR de cada história — o topo fica com TEMAS
    variados em vez de 4 cópias grudadas. O nº de fontes do cluster ainda
    pontua o cross_source (matéria muito coberta = importante) e vai pro card
    via `_also_sources` (ex.: "também em Valor, Folha").
    """
    if not news_items:
        return []
    reg_set = set(_norm(e) for e in regulatory_entities if e)
    wl = list(watch_terms)
    now = datetime.now(timezone.utc)

    clusters = _cluster_items(news_items) if diversify else [[it] for it in news_items]

    reps = []
    for cluster in clusters:
        # nº de FONTES distintas cobrindo a história (não nº de items). Se os
        # items não trouxerem 'source', usa o tamanho do cluster como proxy.
        srcs = {(it.get("source") or "").strip() for it in cluster}
        srcs.discard("")
        n_sources = max(len(srcs) if srcs else len(cluster), 1)

        # Elege o representante: prefere quem TEM resumo (card útil), depois maior score.
        best_it = best = best_key = None
        for it in cluster:
            s = score_news_item(it, n_sources, wl, reg_set, now=now)
            has_sum = 1 if (it.get("summary") or "").strip() else 0
            key = (has_sum, s["total"])
            if best is None or key > best_key:
                best_it, best, best_key = it, s, key

        rep = dict(best_it)  # shallow copy
        rep["_score"] = best["total"]
        rep["_breakdown"] = best["breakdown"]
        rep["_cluster_size"] = len(cluster)
        rep["_also_sources"] = sorted(srcs)
        reps.append(rep)

    reps = [r for r in reps if r["_score"] >= min_score]
    reps.sort(key=lambda x: x["_score"], reverse=True)
    return reps[:top_n]


def extract_regulatory_entities(aneel_aux: dict, dou_items: list) -> set:
    """Extrai empresas/temas mencionados em DOU + Pautas + CPs pra cross-ref com news.
    Retorna set de strings normalizadas."""
    entities = set()
    # DOU items: pega empresa_extraida + companhia se houver
    if dou_items:
        for it in dou_items:
            emp = it.get("empresa_extraida") or it.get("orgao")
            if emp:
                emp_norm = _norm(str(emp))
                if len(emp_norm) > 3:
                    entities.add(emp_norm)
    # ANEEL aux (pautas + cp): extrai do título/summary
    if aneel_aux:
        for v in aneel_aux.values():
            summary = v.get("summary") or v.get("objeto") or ""
            # Pega nomes propios capitalizados de 3+ chars (empresas)
            for m in re.finditer(r"\b([A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]+)*)\b", v.get("title", "") + " " + summary):
                name = m.group(1).strip()
                if len(name) >= 4 and name not in ("Consulta", "Pública", "Tomada", "Audiência", "Reunião", "Diretoria", "Análise", "Pauta", "Resultado", "Aprovação", "Resolução"):
                    entities.add(_norm(name))
    return entities
