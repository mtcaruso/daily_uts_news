KEYWORDS = (
    "(energia OR saneamento OR Aneel OR ONS OR elétrica OR leilão "
    "OR Sabesp OR Eletrobras OR Equatorial OR Enel OR \"setor elétrico\" "
    "OR concessão OR PPP OR transmissão OR distribuidora OR \"marco do saneamento\")"
)

# Termos exigidos no TÍTULO quando `strict_title=True` na fonte.
# Use lowercase; o match é case-insensitive e por substring (basta uma ocorrência).
# Edite essa lista pra ajustar precisão das fontes generalistas (Folha, Estadão).
TITLE_REQUIRED_TERMS = [
    # Reguladores e órgãos
    "aneel", "ons", "ccee", "epe", "mme",
    # Termos diretos do setor
    "energia", "energétic", "elétric", "saneamento", "sanitár",
    "esgoto", "tratamento de água",
    # Geração
    "hidrelétric", "termoelétric", "eólic", "solar", "biomassa",
    "renováv", "hidrogênio",
    # Variações sem acento (caso o título use ASCII)
    "eletric", "eletricidade", "hidreletric", "termoeletric", "eolic",
    "renovav", "energetic",
    # Conceitos
    "subestação", "linhas de transmissão", "leilão de energia",
    "leilão de transmissão", "leilão de capacidade", "marco do saneamento",
    # Empresas — elétricas
    "sabesp", "eletrobras", "equatorial", "enel", "cemig", "copel",
    "engie", "edp", "neoenergia", "cteep", "energisa",
    # Empresas — saneamento
    "copasa", "sanepar", "compesa", "casan", "iguá", "aegea", "brk",
]


def _q(domain, sector_only=False):
    return f"site:{domain}" if sector_only else f"site:{domain} {KEYWORDS}"


SOURCES = [
    {"name": "Valor",          "query": _q("valor.globo.com")},
    {"name": "Pipeline",       "query": _q("pipelinevalor.globo.com")},
    {"name": "Brazil Journal", "query": _q("braziljournal.com")},
    {"name": "Folha",          "query": _q("folha.uol.com.br"),   "strict_title": True},
    {"name": "Estadão",        "query": _q("estadao.com.br"),     "strict_title": True},
    {"name": "Agência Infra",  "query": _q("agenciainfra.com")},
    {"name": "Canal Energia",  "query": _q("canalenergia.com.br", sector_only=True)},
    {"name": "Megawhat",       "query": _q("megawhat.uol.com.br", sector_only=True)},
]
