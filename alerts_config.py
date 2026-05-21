"""
Config dos alertas. Edite as listas abaixo pela interface do GitHub quando quiser
adicionar/remover keywords ou empresas. O próximo run (cron a cada 30min)
usa a nova config automaticamente.

Importante: alterações nas listas NÃO disparam alertas retroativos —
o sistema só notifica itens que aparecem DEPOIS da mudança.
"""

CONFIG = {
    # Topic do ntfy.sh — qualquer um com esse nome consegue ler/enviar.
    # Mantenha em segredo razoável.
    "ntfy_topic": "utl-mtc-621qmvsd",

    # === ALERTAS DE NOTÍCIAS ===
    # Dispara quando o TÍTULO de uma manchete contiver qualquer dessas substrings
    # (case-insensitive). Use termos distintos pra reduzir falso positivo.
    "news_keywords": [
        "LRCAP",
        "leilão A-5",
        "reserva de capacidade",
        "fato relevante",
        "revisão tarifária",
    ],

    # === ALERTAS DE DOU ===
    # Mesmo princípio mas no título/conteúdo de atos do DOU (MME + ANEEL).
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
