KEYWORDS = (
    "(energia OR saneamento OR Aneel OR ONS OR elétrica OR leilão "
    "OR Sabesp OR Eletrobras OR Equatorial OR Enel OR \"setor elétrico\" "
    "OR concessão OR PPP OR transmissão OR distribuidora OR \"marco do saneamento\")"
)


def _q(domain, sector_only=False):
    return f"site:{domain}" if sector_only else f"site:{domain} {KEYWORDS}"


SOURCES = [
    {"name": "Valor",          "query": _q("valor.globo.com")},
    {"name": "Pipeline",       "query": _q("pipelinevalor.globo.com")},
    {"name": "Brazil Journal", "query": _q("braziljournal.com")},
    {"name": "Folha",          "query": _q("folha.uol.com.br")},
    {"name": "Estadão",        "query": _q("estadao.com.br")},
    {"name": "Agência Infra",  "query": _q("agenciainfra.com")},
    {"name": "Canal Energia",  "query": _q("canalenergia.com.br", sector_only=True)},
    {"name": "Megawhat",       "query": _q("megawhat.uol.com.br", sector_only=True)},
]
