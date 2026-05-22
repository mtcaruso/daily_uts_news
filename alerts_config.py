"""
Config dos alertas. Edite as listas abaixo pela interface do GitHub quando quiser
adicionar/remover keywords ou empresas. O próximo run (cron a cada 30min)
usa a nova config automaticamente.

Importante: alterações nas listas NÃO disparam alertas retroativos —
o sistema só notifica itens que aparecem DEPOIS da mudança.

Formato dos keywords:
  - str: substring match case-insensitive
  - list[str]: AND lógico (TODOS os termos devem aparecer)
"""

CONFIG = {
    # Topic do ntfy.sh — qualquer um com esse nome consegue ler/enviar.
    # Mantenha em segredo razoável.
    "ntfy_topic": "utl-mtc-621qmvsd",

    # === ALERTAS DE NOTÍCIAS ===
    # Dispara quando o TÍTULO de uma manchete contiver:
    # - QUALQUER string desta lista, OU
    # - TODOS os elementos de uma sublista (AND)
    "news_keywords": [
        # Termos genéricos do setor
        "LRCAP",
        "leilão A-5",
        "leilão A-4",
        "leilão A-3",
        "leilão de transmissão",
        "leilão de capacidade",
        "reserva de capacidade",
        "revisão tarifária",
        "reajuste tarifário",

        # === FRs por empresa (AND: "fato relevante" + nome) ===
        ["fato relevante", "eletrobras"],
        ["fato relevante", "axia"],
        ["fato relevante", "equatorial"],
        ["fato relevante", "cemig"],
        ["fato relevante", "copel"],
        ["fato relevante", "light"],
        ["fato relevante", "engie"],
        ["fato relevante", "edp"],
        ["fato relevante", "neoenergia"],
        ["fato relevante", "cteep"],
        ["fato relevante", "isa energia"],
        ["fato relevante", "taesa"],
        ["fato relevante", "energisa"],
        ["fato relevante", "auren"],
        ["fato relevante", "cpfl"],
        ["fato relevante", "sabesp"],
        ["fato relevante", "copasa"],
        ["fato relevante", "sanepar"],

        # === Tickers da B3 (qualquer menção dispara) ===
        # Elétrica
        "ELET3", "ELET6",       # Eletrobras
        "EQTL3",                # Equatorial
        "CMIG3", "CMIG4",       # Cemig
        "CPLE3", "CPLE6", "CPLE11",  # Copel
        "LIGT3",                # Light
        "EGIE3",                # Engie
        "ENBR3",                # EDP
        "NEOE3",                # Neoenergia
        "TRPL3", "TRPL4",       # CTEEP / ISA
        "TAEE11", "TAEE3", "TAEE4",  # Taesa
        "ENGI3", "ENGI4", "ENGI11",  # Energisa
        "AURE3",                # Auren
        "CPFE3",                # CPFL Energia
        # Saneamento
        "SBSP3",                # Sabesp
        "CSMG3",                # Copasa
        "SAPR3", "SAPR4", "SAPR11",  # Sanepar
    ],

    # === ALERTAS DE DOU ===
    "dou_keywords": [
        "revisão tarifária",
        "reajuste tarifário",
        "leilão",
    ],

    # === ALERTAS DE CVM ===
    # Dispara em qualquer NOVO documento IPE das empresas + categorias listadas.
    # Lembrando: dados da CVM têm delay de ~6 dias (alertas chegam atrasados).
    "cvm": {
        "empresas": [
            "Eletrobras", "Equatorial", "Cemig", "Copel", "Light",
            "Engie", "EDP", "Neoenergia", "CTEEP / ISA", "Taesa",
            "Energisa", "Auren", "CPFL Energia",
            "Sabesp", "Copasa", "Sanepar",
        ],
        "categorias": [
            "Fato Relevante",
            "Comunicado ao Mercado",
        ],
    },
}
