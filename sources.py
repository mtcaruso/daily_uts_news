"""Configuração das 8 fontes de notícias e das palavras de busca.

🔴 Duas cópias IDÊNTICAS: utilities-interface/lib/sources.py (site) e
daily_uts_news/sources.py (e-mail, resumos prontos e alertas). Mexeu em uma,
copie para a outra.

Como a busca funciona (testado em 05/11/2026):
- O Google News só lê os primeiros 200 CARACTERES da busca e ignora o resto
  SEM AVISAR. A busca antiga tinha ~250 e "PPP", "transmissão",
  "distribuidora" e "marco do saneamento" nunca eram buscados.
- Cada busca devolve no máximo 100 resultados, e sem filtro de data o teto
  enche de matéria antiga (no Canal Energia, só ~40 dos 100 eram da semana).
Por isso as palavras são repartidas em várias buscas curtas por site, todas
com `when:Nd`, e os resultados são juntados (ver `build_queries`).
- O Google iguala acento (elétrica = eletrica), mas NÃO plural/gênero
  (renovável ≠ renováveis): as variações entram por extenso.
"""
import math

# Palavras da busca, em GRUPOS. Cada grupo vira uma ou mais buscas próprias.
# As amplas lotam o teto de 100 nos sites grandes (Valor, Folha, Estadão, 2 dias);
# separadas delas, as específicas (empresas, temas) resgatam o que ficou de fora.
# Termo com espaço vira frase exata ("leilão de energia").
SEARCH_GROUPS = [
    # Amplas: setor
    ["energia", "elétrica", "elétrico", "saneamento", "Aneel", "leilão",
     "concessão", "PPP", "transmissão", "distribuidora"],
    ["elétricas", "elétricos", "eletricidade", "energética", "energético",
     "distribuidoras", "esgoto", "sanitário", "sanitária", "tratamento de água"],
    ["tarifa", "tarifas", "tarifária", "tarifário", "reajuste", "reajustes",
     "solar", "gerador", "geradora", "geradoras", "postes", "fios"],
    # Geração e rede
    ["hidrelétrica", "hidrelétricas", "termelétrica", "termelétricas",
     "termoelétrica", "eólica", "eólicas", "biomassa", "renovável", "renováveis",
     "hidrogênio"],
    ["transmissora", "transmissoras", "subestação", "concessionária",
     "concessionárias", "rede elétrica", "setor elétrico", "concessão de energia"],
    # Órgãos e associações
    ["ONS", "CCEE", "EPE", "MME", "Abradee", "Abrate", "Abraceel", "Abrace",
     "Abrapch", "Absolar", "Abeeólica"],
    # Empresas (Axia = novo nome da Eletrobras)
    ["Sabesp", "Eletrobras", "Axia", "Equatorial", "Enel", "Cemig", "Copel",
     "CPFL", "Light", "Engie", "EDP", "Neoenergia", "CTEEP", "ISA CTEEP"],
    ["Energisa", "Taesa", "Eneva", "Auren", "Alupar", "Celesc", "Copasa",
     "Sanepar", "Compesa", "Casan", "Iguá", "Aegea", "BRK"],
    # Temas específicos
    ["LRCAP", "BESS", "curtailment", "posteiro", "leilão de armazenamento",
     "leilão de energia", "leilão de transmissão", "leilão de capacidade"],
    ["linhas de transmissão", "linha de transmissão", "marco do saneamento"],
]

# Limite do Google é 200 caracteres; 190 em bytes UTF-8 dá folga para acento.
QUERY_MAX_BYTES = 190

# Termos exigidos no TÍTULO nas fontes com `strict_title` (as 6 gerais). Sem
# esse filtro, entra tudo que cita a palavra no TEXTO: em 05/11/2026 seriam
# ~700 manchetes em 30h em vez de ~90, quase todas fora do setor. Match pelo
# começo da palavra, sem diferenciar maiúscula. Empresa nova ou que mudou de
# nome (ex.: Eletrobras → Axia) tem que entrar aqui E em SEARCH_GROUPS.
TITLE_REQUIRED_TERMS = [
    "aneel", "ons", "ccee", "epe", "mme",
    "leilão", "concessão", "ppp", "transmissão",
    "energia", "energétic", "elétric", "saneamento", "sanitár",
    "esgoto", "tratamento de água",
    "hidrelétric", "termoelétric", "termelétric", "eólic", "solar", "biomassa",
    "renováv", "hidrogênio",
    "eletric", "eletricidade", "hidreletric", "termoeletric", "eolic",
    "renovav", "energetic",
    "subestação", "linhas de transmissão", "leilão de energia",
    "leilão de transmissão", "leilão de capacidade", "leilão de armazenamento",
    "marco do saneamento", "lrcap", "bess", "curtailment", "posteiro",
    "tarifa", "tarifár", "reajuste",
    "distribuidor", "transmissor", "gerador",
    "concessão de energia", "concessionária", "postes",
    "fios", "rede elétrica", "linha de transmissão",
    "abradee", "abrate", "abraceel", "abrace", "abrapch", "absolar", "abeeolica",
    "sabesp", "eletrobras", "axia", "equatorial", "enel", "cemig", "copel",
    "cpfl", "light", "engie", "edp", "neoenergia", "cteep", "energisa",
    "taesa", "eneva", "isa cteep", "auren", "alupar", "celesc",
    "copasa", "sanepar", "compesa", "casan", "iguá", "aegea", "brk",
]

BLOCKLIST = [
    "agência de comunicação", "patrocinado", "publicidade",
    "press release", "informe publicitário", "branded content",
]


def days_for(lookback_hours: int) -> int:
    """Janela do filtro `when:` do Google, em dias, que cobre `lookback_hours`."""
    return max(1, math.ceil(lookback_hours / 24))


def _term(t: str) -> str:
    return f'"{t}"' if " " in t else t


def build_queries(domain: str, sector_only: bool = False, days: int = 2) -> list[str]:
    """Buscas do Google News para um site, cada uma com até QUERY_MAX_BYTES.

    Site setorial (sector_only) = tudo do site, sem palavras. Os demais: uma
    ou mais buscas por grupo de SEARCH_GROUPS (grupos nunca se misturam, para
    uma palavra ampla não lotar o teto de 100 de uma busca de específicas)."""
    head = f"site:{domain} when:{days}d"
    if sector_only:
        return [head]

    def q(terms):
        return f"{head} ({' OR '.join(terms)})"

    queries = []
    for group in SEARCH_GROUPS:
        cur = []
        for t in map(_term, group):
            if cur and len(q(cur + [t]).encode("utf-8")) > QUERY_MAX_BYTES:
                queries.append(q(cur))
                cur = []
            cur.append(t)
        if cur:
            queries.append(q(cur))
    return queries


def source_queries(source: dict, days: int) -> list[str]:
    """Buscas de uma fonte. Aceita o formato novo (domain) e o antigo (query)."""
    if source.get("domain"):
        return build_queries(source["domain"], source.get("sector_only", False), days)
    return [f"{source['query']} when:{days}d"]


# Gerais: busca pelas palavras + exige termo do setor no título.
# Setoriais (sector_only): tudo do site, sem palavras e sem filtro.
SOURCES = [
    {"name": "Valor",          "domain": "valor.globo.com",         "strict_title": True},
    {"name": "Pipeline",       "domain": "pipelinevalor.globo.com", "strict_title": True},
    {"name": "Brazil Journal", "domain": "braziljournal.com",       "strict_title": True},
    {"name": "Folha",          "domain": "folha.uol.com.br",        "strict_title": True},
    {"name": "Estadão",        "domain": "estadao.com.br",          "strict_title": True},
    {"name": "Agência Infra",  "domain": "agenciainfra.com",        "strict_title": True},
    {"name": "Canal Energia",  "domain": "canalenergia.com.br", "sector_only": True},
    {"name": "Megawhat",       "domain": "megawhat.uol.com.br", "sector_only": True},
]
