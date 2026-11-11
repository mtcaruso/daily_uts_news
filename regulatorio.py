"""Monitor Regulatório (site/): junta num arquivo só, regulatorio.json, as
consultas abertas e recentes (ANEEL, MME, ARSESP, ARSAE), os processos SEI do
sei_watch.txt e uma linha do tempo do que mudou. Roda no alerts-loop a cada
15min, DEPOIS do alerts.py (lê o alerts_state.json que ele acabou de gravar).
Não notifica nada: os alertas continuam só no alerts.py.

Linha do tempo: cada coisa vira um evento com uma chave (consulta aberta, prazo
alterado, documento novo, andamento fora da rotina, item de pauta, resultado).
Chave já vista não volta (regulatorio_state.json). O evento usa a data da
própria fonte quando ela tem; sem data (documento novo no MME e na ARSAE), vale
o momento em que apareceu. Na 1ª run, o que não tem data só semeia.

Fonte que falha mantém as consultas da leitura anterior e fica marcada no
painel (fontes[nome].ok = false), em vez de sumir com elas.

Uso: python regulatorio.py [--saida arquivo.json]
"""
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import alerts
import sei_monitor

OUT = Path("regulatorio.json")
PROJETOS = Path("regulatorio_projetos.json")  # aba Projetos (histórico completo do SEI)
STATE = Path("regulatorio_state.json")
BRT = timezone(timedelta(hours=-3))
JANELA_ENCERRADAS = 30   # dias em que a consulta encerrada continua no painel
JANELA_EVENTOS = 90      # dias de linha do tempo guardados
JANELA_SEMENTE = 60      # 1ª run: só eventos datados destes últimos dias
MAX_EVENTOS = 600
MAX_VISTOS = 8000

ANEEL_TIPOS = {
    "partic_cp": ("Consulta Pública", "CP", "https://antigo.aneel.gov.br/consultas-publicas"),
    "partic_ts": ("Tomada de Subsídios", "TS", "https://antigo.aneel.gov.br/tomadas-de-subsidios"),
    "partic_ap": ("Audiência Pública", "AP", "https://antigo.aneel.gov.br/audiencias-publicas"),
}
# "obter subsídios e informações adicionais para avaliar…" → "Avaliar…"
_OBTER = re.compile(
    r"^\s*(?:obter|colher)\s+(?:subs[íi]dios|contribui[çc][õo]es)(?:\s+e\s+informa[çc][õo]es(?:\s+adicionais)?)?"
    r"\s*(?:para\s+(?:a\s+|o\s+)?|acerca\s+d[aeo]s?\s+|sobre\s+(?:a\s+|o\s+)?|com\s+vistas\s+[àa]s?\s+|referentes?\s+[àa]s?\s+)?",
    re.IGNORECASE)
_PAUTA_ABERTURA = re.compile(
    r"abertura\s+de\s+(?:consulta\s+p[úu]blica|tomada\s+de\s+subs[íi]dios)", re.IGNORECASE)
_ARSAE_FINAIS = re.compile(r"documentos\s+finais", re.IGNORECASE)


# ---------------- utilidades ----------------
def agora() -> datetime:
    return datetime.now(BRT)


def _dmy(s):
    """'27/11/2026' (ou '1º/10/2026') → '2026-11-27'."""
    try:
        return datetime.strptime((s or "").strip().replace("º", ""), "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _utc(s):
    """'2026-10-23T13:42:16Z' (SharePoint, UTC) → datetime BRT."""
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(BRT)
    except (TypeError, ValueError):
        return None


def _dmy_hm(s):
    """'09/11/2026 15:57' → ('2026-11-09', '15:57')."""
    try:
        d = datetime.strptime((s or "").strip()[:16], "%d/%m/%Y %H:%M")
        return d.strftime("%Y-%m-%d"), d.strftime("%H:%M")
    except ValueError:
        return _dmy((s or "")[:10]), None


def _limpa(s, n=None) -> str:
    s = " ".join(str(s or "").replace("**", "").split())
    return s if not n or len(s) <= n else s[:n - 1].rstrip() + "…"


def _tema(s, n=400) -> str:
    s = _limpa(s)
    sem = _OBTER.sub("", s, count=1)
    if sem != s and sem:
        s = sem[0].upper() + sem[1:]
    return _limpa(s, n)


def _chave(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]


def _situacao(fim, hoje, sem_prazo="em andamento") -> str:
    if not fim:
        return sem_prazo
    return "aberta" if fim >= hoje else "encerrada"


def _no_painel(c, hoje) -> bool:
    """Aberta, sem prazo conhecido, ou encerrada há até JANELA_ENCERRADAS dias."""
    if not c.get("fim"):
        return True
    limite = (datetime.strptime(hoje, "%Y-%m-%d") - timedelta(days=JANELA_ENCERRADAS)).strftime("%Y-%m-%d")
    return c["fim"] >= limite


# ---------------- fontes de consulta ----------------
def consultas_aneel(st_alertas: dict, hoje: str) -> tuple:
    """Listagem do PC (aneel_aux_history.json: tema, início, prazo, link) +
    o que o Actions já sabe sem o PC (prazo e objeto do aviso no DOU, guardados
    pelo alerts.py). Devolve (consultas, quando a listagem do PC foi lida)."""
    hist = {}
    lido_pc = None
    if alerts.ANEEL_HISTORY.exists():
        raw = json.loads(alerts.ANEEL_HISTORY.read_text(encoding="utf-8"))
        hist, lido_pc = raw.get("items", {}), raw.get("last_updated")
    partic = st_alertas.get("aneel_partic") or {}
    prazos = partic.get("prazos") or {}
    objetos = st_alertas.get("aneel_objetos") or {}

    out = []
    chaves = {k for k, e in hist.items() if e.get("type") in ANEEL_TIPOS} | set(prazos) | set(objetos)
    ultimo = {}  # (tipo, ano) → maior nº: item sem prazo só fica se for dos mais recentes
    for k in chaves:
        m = re.match(r"(partic_(?:cp|ts|ap))_(\d{1,3})_(\d{4})$", k)
        if m:
            ultimo[(m.group(1), m.group(3))] = max(ultimo.get((m.group(1), m.group(3)), 0), int(m.group(2)))
    recente = (datetime.strptime(hoje, "%Y-%m-%d") - timedelta(days=45)).strftime("%Y-%m-%d")
    for k in sorted(chaves):
        m = re.match(r"(partic_(?:cp|ts|ap))_(\d{1,3})_(\d{4})$", k)
        if not m:
            continue
        tipo, sigla, link = ANEEL_TIPOS[m.group(1)]
        e = hist.get(k) or {}
        fim = ((_dmy(e.get("deadline")) if e.get("deadline_source") == "detalhe" else None)
               or _dmy(prazos.get(k)) or _dmy(e.get("deadline")))
        if not fim:
            # Sem prazo = detalhe que a listagem não leu. Toda CP/TS da ANEEL tem
            # prazo: só fica se for nova (início recente ou dos últimos nºs do ano).
            ini = _dmy(e.get("start_date"))
            if not ((ini and ini >= recente) or (not ini and int(m.group(2)) >= ultimo[(m.group(1), m.group(3))] - 3)):
                continue
        c = {
            "id": f"aneel-{sigla.lower()}-{int(m.group(2)):03d}-{m.group(3)}",
            "orgao": "ANEEL", "tipo": tipo, "sigla": sigla,
            "numero": f"{int(m.group(2)):03d}/{m.group(3)}",
            "tema": _tema(e.get("summary") or e.get("objeto") or objetos.get(k) or e.get("title")),
            "inicio": _dmy(e.get("start_date")), "fim": fim,
            "situacao": _situacao(fim, hoje),
            "setor": "energia", "link": link, "docs": [],
            "fonte": "listagem" if e else "DOU",
        }
        if _no_painel(c, hoje):
            out.append(c)
    return out, lido_pc


MME_TODAS_API = ("https://consultas-publicas.mme.gov.br/consulta-publica/v1/public/"
                 "listagem-sem-filtros?pageNumber=0&pageSize=400&sortBy=id&sortDirection=desc")
_MME_TODAS = {}  # id → CP crua (todas, inclusive antigas): base dos projetos MME


def consultas_mme(hoje: str) -> list:
    """API pública do site de consultas do MME. Só energia elétrica (mesma
    denylist de área do alerta: fora petróleo/gás e mineração). Uma chamada traz
    TODAS as CPs (~220, <1s): as fixadas em projetos_watch.txt podem ser antigas."""
    r = requests.post(MME_TODAS_API, json={}, timeout=60)
    r.raise_for_status()
    todas = r.json().get("content") or []
    _MME_TODAS.clear()
    _MME_TODAS.update({cp["id"]: cp for cp in todas if not cp.get("isDeleted")})
    out = []
    for cp in todas:
        if cp.get("isDeleted"):
            continue
        area = (cp.get("area") or "").split("/")[0].strip().upper()
        if area in alerts.MME_CP_EXCLUDED_AREAS:
            continue
        fim, ini = _dmy(cp.get("dtFim")), _dmy(cp.get("dtInicio"))
        situacao = _situacao(fim, hoje, "aberta" if cp.get("status") == "ABERTA" else "encerrada")
        # Título do arquivo às vezes é o nome cru ("Nota_Tecnica___NT_EPE_031_rev0_").
        docs = [{"k": f"mme{a['id']}",
                 "t": _limpa(re.sub(r"\.pdf$", "", re.sub(r"_+", " ", a.get("titulo") or ""), flags=re.IGNORECASE), 160)
                      or "(documento)"}
                for a in cp.get("arquivosConsultasPublicas") or [] if not a.get("deletar")]
        c = {
            "id": f"mme-cp-{cp['id']}", "orgao": "MME", "tipo": "Consulta Pública", "sigla": "CP",
            "numero": str(cp["id"]),
            "tema": _tema(cp.get("titulo") or cp.get("assuntoResumido")),
            "inicio": ini, "fim": fim, "situacao": situacao, "setor": "energia",
            "area": cp.get("area"), "link": alerts.MME_CP_SITE, "docs": docs,
        }
        if _no_painel(c, hoje):
            out.append(c)
    return out


_ARSESP_TODAS = {}  # id → consulta (todas da janela, mesmo encerradas): base dos projetos ARSESP


def consultas_arsesp(hoje: str) -> list:
    """SharePoint da ARSESP (anônimo). Só saneamento e sem outras concessionárias
    (mesmo filtro do alerta). Documentos com data de criação."""
    itens = alerts._arsesp_get(
        "/Lists/ConsultasPublicas",
        "ID,TipoItem,Setor,Numero,DataDeAbertura,DataDeEncerramento,Descricao,Status,Created,"
        "IdConsultasPublicas,IdAudiencia,Assunto/Title,RevisaoTarifaria/Title",
        60, expand="Assunto,RevisaoTarifaria")
    docs = [(d["ID"], d.get("ConsultasPublicasId"), d.get("Title"), d.get("Created"))
            for d in alerts._arsesp_get("/Lists/ConsultasPublicasDocumentos",
                                        "ID,Title,ConsultasPublicasId,Created", 300)]
    docs += [(d["ID"], d.get("idAudienciasId"), d.get("Titulo"), d.get("Created"))
             for d in alerts._arsesp_get("/Lists/bdAudienciasPublicasDocumentos",
                                         "ID,Titulo,idAudienciasId,Created", 200)]
    por_pai = {}
    for doc_id, pai, titulo, criado in sorted(docs, key=lambda d: d[0]):
        quando = _utc(criado)
        por_pai.setdefault(pai, []).append({
            "k": f"arsesp{doc_id}", "t": _limpa(titulo, 160) or "(documento)",
            "data": quando.strftime("%Y-%m-%d") if quando else None})

    out = []
    for it in itens:
        if not alerts._arsesp_no_escopo(it):
            continue
        audiencia = "Audi" in (it.get("TipoItem") or "")
        abre, fecha = _utc(it.get("DataDeAbertura")), _utc(it.get("DataDeEncerramento"))
        fim = (abre if audiencia else fecha)
        fim = fim.strftime("%Y-%m-%d") if fim else None
        if it.get("IdConsultasPublicas"):
            link = alerts.ARSESP_DETALHE.format(param="idItemC", id=int(it["IdConsultasPublicas"]))
        elif it.get("IdAudiencia"):
            link = alerts.ARSESP_DETALHE.format(param="idItemA", id=int(it["IdAudiencia"]))
        else:
            link = alerts.ARSESP_SITE
        criado = _utc(it.get("Created"))
        numero = str(it.get("Numero") or it["ID"])
        c = {
            "id": f"arsesp-{'ap' if audiencia else 'cp'}-{numero.replace('/', '-')}",
            "orgao": "ARSESP", "tipo": "Audiência Pública" if audiencia else "Consulta Pública",
            "sigla": "AP" if audiencia else "CP", "numero": numero,
            "tema": _tema(it.get("Descricao") or (it.get("Assunto") or {}).get("Title")),
            "inicio": abre.strftime("%Y-%m-%d") if abre and not audiencia else None,
            "fim": fim, "situacao": _situacao(fim, hoje), "setor": "saneamento",
            "audiencia": abre.strftime("%d/%m %Hh%M") if audiencia and abre else None,
            "publicado": criado.strftime("%Y-%m-%d") if criado else None,
            "link": link, "docs": por_pai.get(it["ID"], []),
        }
        _ARSESP_TODAS[c["id"]] = c
        if _no_painel(c, hoje):
            out.append(c)
    return out


def _arsae_blocos(pagina: str) -> dict:
    """Como alerts._arsae_parse, mas separando os documentos finais (depois de
    "Documentos finais"): com eles, a consulta já tem resultado."""
    pagina = pagina.split("<footer", 1)[0]
    starts = [m.start() for m in alerts._ARSAE_MODULO.finditer(pagina)]
    out = {}
    for a, b in zip(starts, starts[1:] + [len(pagina)]):
        bloco = pagina[a:b]
        m = re.search(r'et_pb_tabs_controls.*?<a href="#">(.*?)</a>', bloco, re.S)
        titulo = alerts._txt(m.group(1)) if m else ""
        num = re.search(r"n[º°o]\s*(\d+)", titulo)
        if not num:
            continue
        corte = _ARSAE_FINAIS.search(bloco)
        corte = corte.start() if corte else len(bloco)
        docs, vistos = [], set()
        for mm in re.finditer(r'<a[^>]+href="([^"#][^"]*)"[^>]*>(.*?)</a>', bloco, re.S):
            href = alerts.html.unescape(mm.group(1))
            texto = alerts._txt(mm.group(2))
            if href in vistos:
                if texto:  # mesmo link 2x, um sem texto
                    for d in docs:
                        if d["u"] == href and d["t"] == "(documento)":
                            d["t"] = _limpa(texto, 160)
                continue
            vistos.add(href)
            docs.append({"k": f"arsae{alerts._h(href)}", "t": _limpa(texto, 160) or "(documento)",
                         "u": href, "final": mm.start() > corte})
        out[num.group(1)] = {"titulo": titulo, "docs": docs}
    return out


def consultas_arsae(hoje: str, cache: dict) -> tuple:
    """Páginas do ano atual e do anterior. Só baixa a página (~240KB) quando a
    data de modificação muda; senão reaproveita a leitura anterior (cache no
    regulatorio_state.json). A ARSAE não põe o prazo na página (só no aviso em
    PDF): sem prazo, a situação vem dos documentos finais. Devolve (TODAS as
    consultas das duas páginas, {ano: modified_gmt})."""
    ano = int(hoje[:4])
    mod_prev = cache.get("mod") or {}
    anteriores = cache.get("consultas") or []
    out, mod = [], {}
    for a in (ano, ano - 1):
        try:
            r = alerts._arsae_get(alerts.ARSAE_MODIFIED.format(ano=a), timeout=20)
            lst = r.json() if r.ok else None
        except Exception:
            lst = None
        if lst == []:
            continue  # página do ano ainda não existe
        atual = lst[0].get("modified_gmt") if lst else None
        reuso = [c for c in anteriores if c.get("ano") == a]
        if atual and mod_prev.get(str(a)) == atual and reuso:
            out += reuso
            mod[str(a)] = atual
            continue
        r = alerts._arsae_get(alerts.ARSAE_PAGE.format(ano=a), timeout=30)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        r.encoding = "utf-8"
        blocos = _arsae_blocos(r.text)
        if not blocos and a == ano:
            raise ValueError(f"página de {a} sem nenhuma consulta reconhecida (layout mudou?)")
        pagina = alerts.ARSAE_PAGE.format(ano=a)
        for num, b in blocos.items():
            partes = re.split(r"\s*[–—-]\s+", b["titulo"], maxsplit=1)
            tema = partes[1] if len(partes) > 1 else b["titulo"]
            finais = [d for d in b["docs"] if d["final"]]
            out.append({
                "id": f"arsae-cp-{num}", "orgao": "ARSAE", "tipo": "Consulta e Audiência Pública",
                "sigla": "CP", "numero": f"{num}/{a}", "ano": a, "tema": _tema(tema),
                "inicio": None, "fim": None,
                "situacao": "com resultado" if finais else "em andamento",
                "setor": "gás" if re.search(r"gasmig|g[áa]s\s+canalizado", b["titulo"], re.IGNORECASE) else "saneamento",
                "link": f"{pagina}#CP{num}",
                "docs": [{"k": d["k"], "t": d["t"], "u": d["u"], "final": d["final"]} for d in b["docs"]],
            })
        if atual:
            mod[str(a)] = atual
    return out, mod


# ---------------- processos SEI ----------------
def pauta_aneel() -> list:
    r = requests.get(alerts.ANEEL_PAUTA_API, timeout=60)
    r.raise_for_status()
    return r.json()["result"]["records"]


ANEEL_PAUTA_BUSCA = ("https://dadosabertos.aneel.gov.br/api/3/action/datastore_search"
                     "?resource_id=43386a8b-4781-44ec-a082-fa7fdfe33186&limit=200&sort=DatReuniao%20desc&q=")
_CAMPOS_PAUTA = ("DatReuniao", "IdeReuniao", "TxtAssunto", "NumProcesso", "DscResultadoJulgamento",
                 "NomTipoAtoAdministrativo", "NumAtoAdministrativo", "NomDiretorRelator", "TxtDecisaoJulgamento")


def pauta_por_processo(cache: dict, hoje: str) -> dict:
    """Histórico INTEIRO da pauta de cada processo do sei_watch.txt (a pauta
    recente só cobre ~2 meses: a Enel RJ passou pela Diretoria em 2025). Busca
    pelo nº nos dados abertos da ANEEL, 1x/dia por processo (~4s cada); falha
    mantém a leitura anterior. {chave de 17 dígitos: {"lido": dia, "recs": [...]}}"""
    chaves = set()
    for p in sei_monitor.load_processes():
        num = p.get("processo") or next(iter(alerts._PROC_NUM.findall(p.get("label") or "")), None)
        if num:
            chaves.add(alerts._proc_chave(num))
    novo = {}
    for ch in sorted(chaves):
        c = cache.get(ch)
        if c and c.get("lido") == hoje:
            novo[ch] = c
            continue
        try:
            r = requests.get(ANEEL_PAUTA_BUSCA + ch, timeout=60)
            r.raise_for_status()
            recs = [{k: x.get(k) for k in _CAMPOS_PAUTA} for x in r.json()["result"]["records"]
                    if ch in re.findall(r"\d{17}", x.get("NumProcesso") or "") or ch in (x.get("NumProcesso") or "")]
            novo[ch] = {"lido": hoje, "recs": recs}
        except Exception as e:
            print(f"[regulatorio] pauta do processo {ch}: {e}", file=sys.stderr)
            if c:
                novo[ch] = c
    return novo


def _nulo(s) -> str:
    s = " ".join(str(s or "").split())
    return "" if s in ("None", "null") else s


def processos_sei(recs: list, hoje: str, recs_proc: list = ()) -> tuple:
    """sei_processes.json + sei_historico.json (o PC lê o SEI; o Actions é
    bloqueado) + a pauta da Diretoria nos dados abertos (sem o PC). Devolve
    (processos com o histórico COMPLETO, aberturas de CP/TS que estão na pauta
    de reuniões de hoje em diante). Sem histórico (PC ainda não rodou a versão
    nova), usa os 10 últimos do sei_processes.json."""
    procs = sei_monitor.load_processes()
    historico = sei_monitor.load_historico()
    por_chave = {}
    out = []
    for p in procs:
        h = historico.get(p["id"]) or {}
        # Processo recém-incluído: o número e o tipo vêm do histórico até o PC
        # gravar o sei_processes.json.
        numero = (p.get("processo") or h.get("processo")
                  or next(iter(alerts._PROC_NUM.findall(p.get("label") or "")), None))
        item = {
            "id": p["id"], "apelido": p.get("label") or numero or "processo",
            "numero": numero, "tipo": p.get("tipo") or h.get("tipo"), "link": p.get("url"),
            "lido_em": p.get("last_check_at"),
            "documentos": [], "andamentos": [], "pauta": [],
            "n_documentos": p.get("documento_count"), "n_andamentos": p.get("andamento_count"),
        }
        for x in h.get("documentos") or p.get("last_documentos_top10") or []:
            proto = x.get("protocolo") or ""
            item["documentos"].append({
                "data": _dmy(x.get("data_inclusao") or x.get("data")), "tipo": _limpa(x.get("tipo"), 120),
                "unidade": x.get("unidade"), "unidade_nome": x.get("unidade_nome"),
                "protocolo": proto, "link": x.get("link"), "anexado": "/" in proto})
        for a in h.get("andamentos") or p.get("last_andamentos_top10") or []:
            dia, hora = _dmy_hm(a.get("datahora"))
            desc = _limpa(a.get("descricao"), 300)
            item["andamentos"].append({
                "data": dia, "hora": hora, "unidade": a.get("unidade"), "descricao": desc,
                "hash": a.get("hash"), "rotina": bool(alerts._SEI_ROTINA.match(desc))})
        out.append(item)
        if numero:
            por_chave[alerts._proc_chave(numero)] = item

    aberturas = []
    # recs = pauta recente (400 itens, ~2 meses); recs_proc = histórico inteiro de
    # cada processo acompanhado (pauta_por_processo, 1x/dia). Recente primeiro: o
    # item repetido fica com o resultado mais novo.
    for rec, recente in [(r, True) for r in recs] + [(r, False) for r in recs_proc]:
        dia = (rec.get("DatReuniao") or "")[:10]
        assunto = _limpa(rec.get("TxtAssunto"))
        resultado = _nulo(rec.get("DscResultadoJulgamento"))
        ato = f"{_nulo(rec.get('NomTipoAtoAdministrativo'))} {_nulo(rec.get('NumAtoAdministrativo'))}".strip()
        ato = ato if re.search(r"[A-Za-zÀ-ú]", ato) else ""  # "0" = sem ato
        relator = alerts._particulas(_nulo(rec.get("NomDiretorRelator")))
        base = {"data": dia, "reuniao": _nulo(rec.get("IdeReuniao")), "assunto": _limpa(assunto, 400),
                "resultado": resultado, "ato": ato, "relator": relator,
                "decisao": _limpa(_nulo(rec.get("TxtDecisaoJulgamento")), 400)}
        for d in re.findall(r"\d{17}", rec.get("NumProcesso") or ""):
            p = por_chave.get(alerts._proc_chave(d))
            if p and not any(x["reuniao"] == base["reuniao"] and x["assunto"] == base["assunto"] for x in p["pauta"]):
                p["pauta"].append(dict(base))
        if recente and dia >= hoje and _PAUTA_ABERTURA.search(assunto):
            aberturas.append({**base, "processo": _nulo(rec.get("NumProcesso"))[:40]})
    for p in out:
        p["pauta"] = sorted(p["pauta"], key=lambda x: x["data"], reverse=True)
    aberturas.sort(key=lambda x: x["data"])
    return out, aberturas


# Setor de cada projeto, para o bloco de cima do site (a pedido, 11/11/2026:
# agrupado por tema). Pelo nome/assunto; ARSESP e programas = saneamento. A ordem
# importa: "Fator X" e "WACC" são de distribuição antes de cair em geração.
_GRUPOS = [
    ("Saneamento", re.compile(r"saneamento|sabesp|copasa|sanepar|universaliza|[áa]gua e esgoto", re.IGNORECASE)),
    ("Distribuição", re.compile(r"\benel\b|distribui|perdas|fator x|wacc|remunera[çc][ãa]o do capital|coelce|ampla|tarif", re.IGNORECASE)),
    ("Geração e transmissão", re.compile(r"lrcap|leil[ãa]o|curtail|constrained|armazen|hidrel|gera[çc][ãa]o|transmiss|angra|nuclear|"
                                         r"\bmcp\b|contabiliza|\brap\b|outorga|\bevt\b", re.IGNORECASE)),
]
GRUPOS_ORDEM = ["Distribuição", "Geração e transmissão", "Saneamento", "Outros"]


def grupo_tema(p: dict) -> str:
    if p.get("tipo_projeto") in ("ARSESP", "PROGRAMA"):
        return "Saneamento"
    texto = " ".join(str(p.get(k) or "") for k in ("apelido", "tema", "tipo", "numero"))
    return next((nome for nome, rx in _GRUPOS if rx.search(texto)), "Outros")


def resumo_processo(p: dict) -> dict:
    """Versão curta pro regulatorio.json (a página de consultas e a linha do
    tempo); o histórico inteiro vai no regulatorio_projetos.json."""
    return {**p, "documentos": p["documentos"][:10], "andamentos": p["andamentos"][:10], "pauta": p["pauta"][:10],
            "grupo": grupo_tema(p)}


def projeto(p: dict) -> dict:
    """Histórico completo de um processo, com chaves curtas (o LRCAP sozinho tem
    ~480 documentos e ~600 andamentos): docs {d data, t tipo, u unidade, un nome
    da unidade, p protocolo, l link, x processo anexado}; ands {d, h hora, u, t
    texto, r rotina}."""
    ult = [d["data"] for d in p["documentos"] if d["data"] and not d["anexado"]]
    ult += [a["data"] for a in p["andamentos"] if a["data"] and not a["rotina"]]
    hoje = agora().strftime("%Y-%m-%d")
    ult += [x["data"] for x in p["pauta"] if x.get("data") and x["data"] <= hoje]  # processo só com o nº
    return {
        "id": p["id"], "tipo_projeto": "SEI", "apelido": p["apelido"], "numero": p["numero"], "tipo": p["tipo"],
        "link": p["link"], "lido_em": p["lido_em"],
        # O histórico pode ser mais novo que a contagem do sei_processes.json.
        "n_documentos": max(p["n_documentos"] or 0, len(p["documentos"])),
        "n_andamentos": max(p["n_andamentos"] or 0, len(p["andamentos"])),
        "ultimo_movimento": max(ult) if ult else None,
        "pauta": p["pauta"],
        "docs": [{k: v for k, v in {"d": d["data"], "t": d["tipo"], "u": d["unidade"], "un": d["unidade_nome"],
                                    "p": d["protocolo"], "l": d["link"], "x": 1 if d["anexado"] else None}.items()
                  if v is not None} for d in p["documentos"]],
        "ands": [{k: v for k, v in {"d": a["data"], "h": a["hora"], "u": a["unidade"], "t": a["descricao"],
                                    "r": 1 if a["rotina"] else None}.items() if v is not None}
                 for a in p["andamentos"]],
    }


# ---------------- linha do tempo ----------------
# ---------------- projetos MME e TCU (projetos_watch.txt) ----------------
PROJETOS_WATCH = Path("projetos_watch.txt")
TCU_PAUTA = Path("tcu_pauta.json")  # sessões dos TCs acompanhados (tcu_pauta.py, 1x/dia)
MME_CONTRIB_API = ("https://consultas-publicas.mme.gov.br/consulta-publica/v1/public/"
                   "listagem-contribuicoes-publicas?pageNumber=0&pageSize=1000&sortBy=id&sortDirection=desc")
# Arquivo de documento/contribuição do MME (vem sem nome: baixa como octet-stream).
MME_ARQUIVO = "https://consultas-publicas.mme.gov.br/consulta-publica/v1/public/remote-store/exibir/link/{}"
# Página do processo na Pesquisa Integrada do TCU (abre no navegador; robô é barrado).
TCU_LINK = ("https://pesquisa.apps.tcu.gov.br/documento/processo/*/NUMEROSOMENTENUMEROS%253A{}/"
            "DTAUTUACAOORDENACAO%2520desc%252C%2520NUMEROCOMZEROS%2520desc/0")


def ler_projetos_watch() -> list:
    """[{tipo: MME|TCU, chave, apelido}] do projetos_watch.txt."""
    try:
        linhas = PROJETOS_WATCH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for linha in linhas:
        linha = linha.strip()
        if not linha or linha.startswith("#"):
            continue
        partes = [x.strip() for x in linha.split("|")]
        if len(partes) >= 2 and partes[0].upper() in ("MME", "TCU", "ARSESP", "PROGRAMA") and partes[1]:
            out.append({"tipo": partes[0].upper(), "chave": partes[1], "apelido": partes[2] if len(partes) > 2 else ""})
    return out


def projetos_mme(watch: list, hoje: str) -> list:
    """CPs do MME fixadas: dados da CP (da leitura de todas) + contribuições
    públicas (instituição, data, resumo e arquivo; o nome de quem enviou fica de
    fora). Levanta erro se a API cair (o main mantém a leitura anterior)."""
    out = []
    for w in watch:
        if w["tipo"] != "MME":
            continue
        num = int(re.sub(r"\D", "", w["chave"]) or 0)
        cp = _MME_TODAS.get(num)
        if not cp:
            print(f"::warning::projetos_watch: CP MME {w['chave']} não encontrada na API")
            continue
        r = requests.post(MME_CONTRIB_API, json={"consultaPublica": {"id": num}}, timeout=60)
        r.raise_for_status()
        contrib = []
        for x in r.json().get("content") or []:
            quando = str(x.get("dtEnvioContribuicao") or "")
            contrib.append({
                "id": x["id"], "data": quando[:10] or None, "hora": quando[11:16] or None,
                "instituicao": _limpa(x.get("instituicao") or x.get("descricaoArea"), 140) or "(sem instituição)",
                "titulo": _limpa(x.get("titulo"), 220), "resumo": _limpa(x.get("resumo"), 700),
                "link": MME_ARQUIVO.format(x["arquivo"]) if x.get("arquivo") else None})
        fim, ini = _dmy(cp.get("dtFim")), _dmy(cp.get("dtInicio"))
        docs = [{"k": f"mme{a['id']}",
                 "t": _limpa(re.sub(r"\.pdf$", "", re.sub(r"_+", " ", a.get("titulo") or ""), flags=re.IGNORECASE), 160)
                      or "(documento)",
                 "u": MME_ARQUIVO.format(a["path"]) if a.get("path") else None}
                for a in cp.get("arquivosConsultasPublicas") or [] if not a.get("deletar")]
        out.append({
            "id": f"mme-cp-{num}", "tipo_projeto": "MME", "apelido": w["apelido"] or f"Consulta Pública MME nº {num}",
            "numero": f"CP {num}", "orgao": "MME", "sigla": "CP", "tipo": "Consulta Pública",
            "tema": _tema(cp.get("titulo") or cp.get("assuntoResumido"), 600),
            "inicio": ini, "fim": fim,
            "situacao": _situacao(fim, hoje, "aberta" if cp.get("status") == "ABERTA" else "encerrada"),
            "area": cp.get("area"), "processo_sei": cp.get("responsavelSei"),
            "link": alerts.MME_CP_SITE, "docs": docs, "contribuicoes": contrib,
        })
    return out


def projetos_tcu(watch: list, st_alertas: dict) -> list:
    """Processos do TCU fixados: sessões de julgamento (tcu_pauta.json, dados
    abertos do TCU filtrados pelo tcu_pauta.py) e acórdãos reconhecidos pelo
    alerts.alert_tcu (alerts_state['tcu']['acordaos'])."""
    pauta = _json(TCU_PAUTA, {})
    reconhecidos = ((st_alertas.get("tcu") or {}).get("acordaos") or {})
    out = []
    for w in watch:
        if w["tipo"] != "TCU":
            continue
        dig = re.sub(r"\D", "", w["chave"])
        if len(dig) != 11:
            print(f"::warning::projetos_watch: TC {w['chave']} fora do formato 000.000/0000-0")
            continue
        tc = f"{dig[:3]}.{dig[3:6]}/{dig[6:10]}-{dig[10]}"
        sessoes = sorted(((pauta.get("processos") or {}).get(tc) or []), key=lambda s: s["data"], reverse=True)
        ult = sessoes[0] if sessoes else {}
        out.append({
            "id": f"tcu-{dig}", "tipo_projeto": "TCU", "apelido": w["apelido"] or f"TC {tc}",
            "numero": f"TC {tc}", "link": TCU_LINK.format(int(dig)),
            "relator": ult.get("relator"), "tipo": ult.get("tipo"), "sessoes": sessoes,
            "pauta_lida_em": pauta.get("lido_em"),
            "acordaos": sorted(({"key": k, "numero": a.get("numero"), "ano": a.get("ano"), "colegiado": a.get("colegiado"),
                                 "data": _dmy(a.get("data")), "relator": a.get("relator"), "sumario": a.get("sumario"),
                                 "url": a.get("url")} for k, a in reconhecidos.items() if a.get("tc") == tc),
                               key=lambda a: a["data"] or "", reverse=True),
        })
    return out


def projetos_arsesp(watch: list) -> list:
    """CP/AP da ARSESP fixadas (ex.: 'ARSESP | CP 17/2026; AP 07/2026 | Fator X'):
    as consultas da leitura de hoje (todas da janela, mesmo encerradas), com os
    documentos (deliberação, nota técnica, contribuições…). Vários itens do mesmo
    tema = um projeto só."""
    out = []
    for w in watch:
        if w["tipo"] != "ARSESP":
            continue
        itens = []
        for ref in w["chave"].split(";"):
            m = re.match(r"\s*(CP|AP)\s*(\d+)\s*/\s*(\d{4})", ref, re.IGNORECASE)
            if not m:
                continue
            c = next((c for c in _ARSESP_TODAS.values() if c["sigla"] == m.group(1).upper()
                      and c["numero"].split("/")[0].lstrip("0") == m.group(2).lstrip("0")
                      and c["numero"].endswith(m.group(3))), None)
            if c:
                itens.append(c)
            else:
                print(f"::warning::projetos_watch: ARSESP {ref.strip()} não encontrada na lista da ARSESP")
        if not itens:
            continue
        out.append({
            "id": "arsesp-proj-" + "-".join(c["id"].replace("arsesp-", "") for c in itens),
            "tipo_projeto": "ARSESP", "apelido": w["apelido"] or itens[0]["tema"][:80],
            "numero": " + ".join(f"{c['sigla']} {c['numero']}" for c in itens),
            "tema": itens[0]["tema"], "link": itens[0]["link"], "itens": itens,
        })
    return out


def projetos_programa(st_alertas: dict) -> list:
    """Programas do projetos_watch.txt (UniversalizaSP): notícias oficiais da
    Agência SP e da SEMIL coletadas pelo alerts.alert_programas (alerts_state
    ['programas']) + consultas da ARSESP que citam o programa."""
    out = []
    for pg in alerts.programas_watch():
        st = ((st_alertas.get("programas") or {}).get(pg["slug"]) or {})
        arsesp = [{k: c.get(k) for k in ("id", "sigla", "numero", "tema", "situacao", "fim", "link")}
                  for c in _ARSESP_TODAS.values() if pg["regex"].search(alerts._sem_acento(c.get("tema") or ""))]
        out.append({"id": f"programa-{pg['slug']}", "tipo_projeto": "PROGRAMA", "apelido": pg["apelido"],
                    "numero": "Programa do Governo de SP", "link": pg["link"], "resumo": pg["resumo"],
                    "termos": pg["termos"], "noticias": st.get("itens") or [], "lido_em": st.get("lido_em"),
                    "arsesp": arsesp})
    return out


def resumo_extra(p: dict, hoje: str) -> dict:
    """Cartão do topo para projeto MME/TCU (o completo vai no regulatorio_projetos.json)."""
    base = {**{k: p.get(k) for k in ("id", "tipo_projeto", "apelido", "numero", "link")}, "grupo": grupo_tema(p)}
    if p["tipo_projeto"] == "PROGRAMA":
        n = p["noticias"]
        return {**base, "tema": p["resumo"], "n_noticias": len(n), "n_arsesp": len(p["arsesp"]),
                "ult_noticia": ({"data": n[0]["data"], "titulo": n[0]["titulo"]} if n else None)}
    if p["tipo_projeto"] == "ARSESP":
        docs = [d for c in p["itens"] for d in c.get("docs", [])]
        cp = next((c for c in p["itens"] if c["sigla"] == "CP"), p["itens"][0])
        ap = next((c for c in p["itens"] if c["sigla"] == "AP"), None)
        return {**base, "situacao": cp["situacao"], "fim": cp["fim"], "tema": p["tema"],
                "audiencia": ap["audiencia"] if ap else None, "audiencia_dia": ap["fim"] if ap else None,
                "n_docs": len(docs), "ult_doc": max((d.get("data") or "" for d in docs), default="") or None}
    if p["tipo_projeto"] == "MME":
        c = sorted(p["contribuicoes"], key=lambda x: (x["data"] or "", x["hora"] or ""), reverse=True)
        return {**base, "situacao": p["situacao"], "fim": p["fim"], "n_docs": len(p["docs"]),
                "n_contrib": len(c), "ult_contrib": ({"data": c[0]["data"], "instituicao": c[0]["instituicao"]} if c else None),
                "tema": p["tema"]}
    prox = [s for s in p["sessoes"] if s["data"] >= hoje]
    ac = (p.get("acordaos") or [None])[0]
    return {**base, "relator": p["relator"], "tipo": p["tipo"],
            "ult_acordao": ({k: ac[k] for k in ("numero", "ano", "colegiado", "data")} if ac else None),
            "proxima": prox[-1] if prox else None,
            "ultima": next((s for s in p["sessoes"] if s["data"] < hoje), None),
            "pauta_lida_em": p["pauta_lida_em"]}


def candidatos_extra(extras: list, hoje: str, semente: bool, vistos: set) -> list:
    """Eventos dos projetos MME (contribuição nova) e TCU (entrada em pauta).
    Documentos e prazo da CP fixada vêm do candidatos() comum."""
    evs = []
    for p in extras:
        ref = p["id"]
        if p["tipo_projeto"] == "MME":
            nova = not semente and _chave(f"projeto|{ref}") not in vistos
            evs.append(_ev(f"projeto|{ref}", None, "MME", "aberta", "", ref=ref))  # marca o 1º dia; nunca aparece
            evs[-1]["_silencioso"] = True
            for x in p["contribuicoes"]:
                e = _ev(f"contrib|{ref}|{x['id']}", x["data"], "MME", "contribuicao",
                        f"{p['numero']}: contribuição de {x['instituicao']}",
                        x["titulo"] or x["resumo"], x["link"] or p["link"], ref, hora=x["hora"],
                        grupo=(p["numero"], x["instituicao"], p["link"], "contribuições novas"))
                if nova and not x["data"]:
                    e["_silencioso"] = True
                evs.append(e)
        elif p["tipo_projeto"] == "PROGRAMA":
            curto = p["apelido"].split(":")[0]
            for n in p["noticias"]:
                evs.append(_ev(f"prog|{ref}|{n['chave']}", n["data"], "SP", "noticia", f"{curto}: {n['titulo']}",
                               " e ".join(l["fonte"] for l in n["links"]) + (f" · {n['resumo']}" if n.get("resumo") else ""),
                               n["links"][0]["url"] if n.get("links") else p["link"], ref))
        elif p["tipo_projeto"] == "TCU":
            for a in p.get("acordaos", []):
                evs.append(_ev(f"tcuac|{a['key']}", a["data"], "TCU", "resultado",
                               f"{p['apelido']}: Acórdão {a['numero']}/{a['ano']} ({a['colegiado']})",
                               a.get("sumario") or "", a.get("url") or p["link"], ref))
            for s in p["sessoes"]:
                futura = s["data"] >= hoje
                quando = f"{s['data'][8:10]}/{s['data'][5:7]}"
                # O TCU só publica a sessão no dia em que ela acontece (sem pauta futura).
                evs.append(_ev(f"tcusessao|{ref}|{s['data']}|{s.get('colegiado')}", None if futura else s["data"], "TCU", "pauta",
                               f"{p['apelido']}: sessão de julgamento no {s.get('colegiado') or 'TCU'} em {quando}",
                               " · ".join(filter(None, [p["numero"], f"relator(a): {s['relator']}" if s.get("relator") else "",
                                                        s.get("tipo")])), p["link"], ref))
    return evs


def _ev(uid, dia, orgao, tipo, titulo, detalhe="", link=None, ref=None, hora=None, grupo=None) -> dict:
    """grupo = (rótulo, item, link) dos documentos: vários do mesmo dia viram um evento só."""
    e = {"uid": _chave(uid), "dia": dia, "hora": hora, "orgao": orgao, "tipo": tipo,
         "titulo": titulo, "detalhe": detalhe, "link": link, "ref": ref}
    if grupo:
        e["_grupo"] = grupo
    return e


def _agrupa(evs: list) -> list:
    """Documentos novos do mesmo processo/consulta no mesmo dia → 1 evento (a
    ARSESP publica dezenas de contribuições de uma vez)."""
    out, grupos = [], {}
    for e in evs:
        g = e.pop("_grupo", None)
        if not g:
            out.append(e)
            continue
        k = (e["ref"], e["dia"], e["tipo"])
        if k not in grupos:
            grupos[k] = (e, g, [])
            out.append(e)
        grupos[k][2].append(g[1])
    for e, g, itens in grupos.values():
        rotulo, link = g[0], g[2]
        plural = g[3] if len(g) > 3 else "documentos novos"
        if len(itens) > 1:
            e["titulo"] = f"{rotulo}: {len(itens)} {plural}"
            e["detalhe"] = " · ".join(itens[:4]) + (f" · …e mais {len(itens) - 4}" if len(itens) > 4 else "")
            e["link"] = link
    return out


def _rotulo(c) -> str:
    return f"{c['sigla']} {c['numero']}"


def candidatos(consultas, processos, aberturas, prev, hoje, semente, vistos) -> list:
    """Todos os eventos possíveis desta leitura. Quem já foi visto é filtrado
    depois, pela chave. dia=None = sem data na fonte (vale o momento atual)."""
    evs = []
    prev_c = {c["id"]: c for c in prev.get("consultas", [])}
    for c in consultas:
        rot = _rotulo(c)
        antes = prev_c.get(c["id"])
        nova = not semente and _chave(f"aberta|{c['id']}") not in vistos
        prazo = ""
        if c.get("audiencia"):
            prazo = f" · audiência em {c['audiencia']}"
        elif c.get("fim"):
            prazo = f" · contribuições até {c['fim'][8:10]}/{c['fim'][5:7]}"
        aberta_em = c.get("publicado") or c.get("inicio")
        if aberta_em and aberta_em > hoje:
            aberta_em = None  # audiência futura: o início é a sessão, não a publicação
        evs.append(_ev(f"aberta|{c['id']}", aberta_em, c["orgao"], "aberta",
                       f"{rot} aberta" if c["sigla"] != "AP" else f"{rot} marcada",
                       c["tema"] + prazo, c["link"], c["id"]))
        if antes and antes.get("fim") and c.get("fim") and antes["fim"] != c["fim"]:
            delta = abs((datetime.strptime(c["fim"], "%Y-%m-%d") - datetime.strptime(antes["fim"], "%Y-%m-%d")).days)
            if delta >= 2:  # ANEEL: DOU e listagem às vezes divergem 1 dia (não é prorrogação)
                f0, f1 = antes["fim"], c["fim"]
                evs.append(_ev(f"prazo|{c['id']}|{f1}", None, c["orgao"], "prazo",
                               f"{rot}: prazo {'prorrogado' if f1 > f0 else 'alterado'}",
                               f"{f0[8:10]}/{f0[5:7]} → {f1[8:10]}/{f1[5:7]} · {c['tema']}", c["link"], c["id"]))
        for d in c.get("docs", []):
            dia = d.get("data")
            titulo = f"{rot}: documento novo" + (" (resultado)" if d.get("final") else "")
            e = _ev(f"doc|{c['id']}|{d['k']}", dia, c["orgao"], "documento", titulo,
                    f"{d['t']} · {c['tema']}", d.get("u") or c["link"], c["id"],
                    grupo=(rot, d["t"], c["link"]))
            # Documento da abertura já está no "aberta": só marca como visto
            # (consulta nova nesta leitura, ou 1ª run com documento do mesmo dia).
            if nova or (semente and dia and aberta_em and dia <= aberta_em):
                e["_silencioso"] = True
            evs.append(e)
    for p in processos:
        ref = f"sei:{p['id']}"
        for x in p["documentos"]:
            if "/" in (x.get("protocolo") or ""):
                continue  # processo anexado: ruído
            tipo = x["tipo"] or "documento"
            evs.append(_ev(f"seidoc|{p['id']}|{x['protocolo']}", x["data"], "SEI", "documento",
                           f"{p['apelido']}: {tipo}",
                           " · ".join(filter(None, [x.get("unidade"), p.get("numero")])), x.get("link") or p["link"], ref,
                           grupo=(p["apelido"], f"{tipo} ({x.get('unidade')})" if x.get("unidade") else tipo, p["link"])))
        for a in p["andamentos"]:
            if a["rotina"]:
                continue
            evs.append(_ev(f"seiand|{p['id']}|{a['hash']}", a["data"], "SEI", "andamento",
                           f"{p['apelido']}: andamento", f"{a.get('unidade') or ''}: {a['descricao']}".strip(": "),
                           p["link"], ref, hora=a.get("hora")))
        for x in p["pauta"]:
            uid = f"{x['reuniao']}|{p['id']}|{x['assunto'][:60]}"
            quando = f"{x['reuniao']} de {x['data'][8:10]}/{x['data'][5:7]}"
            if x["resultado"]:
                evs.append(_ev(f"resultado|{uid}", x["data"], "SEI", "resultado",
                               f"{p['apelido']}: {x['resultado']}",
                               " · ".join(filter(None, [quando, x["ato"], x["decisao"] or x["assunto"]])),
                               p["link"], ref))
            elif x["data"] >= hoje:
                evs.append(_ev(f"pauta|{uid}", None, "SEI", "pauta",
                               f"{p['apelido']} na pauta de {x['data'][8:10]}/{x['data'][5:7]}",
                               " · ".join(filter(None, [quando, f"relator(a): {x['relator']}" if x["relator"] else "",
                                                        x["assunto"]])), p["link"], ref))
    for x in aberturas:
        evs.append(_ev(f"pautacp|{x['reuniao']}|{x['assunto'][:80]}", None, "ANEEL", "pauta",
                       f"Na pauta de {x['data'][8:10]}/{x['data'][5:7]}: abertura de consulta",
                       x["assunto"], None, None))
    return evs


def atualiza_eventos(cands, eventos_prev, vistos, hoje, semente, momento) -> tuple:
    corte = (datetime.strptime(hoje, "%Y-%m-%d") - timedelta(days=JANELA_SEMENTE if semente else JANELA_EVENTOS)).strftime("%Y-%m-%d")
    vistos_set = set(vistos)
    novos = []
    for e in cands:
        if e["uid"] in vistos_set:
            continue
        vistos.append(e["uid"])
        vistos_set.add(e["uid"])
        if e.pop("_silencioso", False):
            continue
        if e["dia"] is None:
            if semente:
                continue  # sem data na fonte: a 1ª run só semeia
            e["dia"], e["hora"] = momento.strftime("%Y-%m-%d"), momento.strftime("%H:%M")
            e["detectado"] = True
        if e["dia"] < corte:
            continue
        e["visto_em"] = momento.isoformat(timespec="seconds")
        novos.append(e)
    novos = _agrupa(novos)
    for e in cands:
        e.pop("_grupo", None)
        e.pop("_silencioso", None)
    limite = (datetime.strptime(hoje, "%Y-%m-%d") - timedelta(days=JANELA_EVENTOS)).strftime("%Y-%m-%d")
    todos = [e for e in eventos_prev + novos if e["dia"] >= limite]
    todos.sort(key=lambda e: (e["dia"], e.get("hora") or "", e.get("visto_em") or ""), reverse=True)
    return todos[:MAX_EVENTOS], vistos[-MAX_VISTOS:], len(novos)


# ---------------- main ----------------
def _json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def main():
    saida = Path(sys.argv[sys.argv.index("--saida") + 1]) if "--saida" in sys.argv else OUT
    momento = agora()
    hoje = momento.strftime("%Y-%m-%d")
    prev = _json(saida, {})
    st = _json(STATE, {})
    semente = not st.get("vistos")
    st_alertas = _json(alerts.STATE_FILE, {})
    fontes_prev = prev.get("fontes", {})
    fontes = {}

    def ler(nome, fn, orgao=None):
        try:
            res = fn()
            fontes[nome] = {"ok": True, "lido_em": momento.isoformat(timespec="seconds")}
            return res
        except Exception as e:
            print(f"[regulatorio] {nome}: {e}", file=sys.stderr)
            print(f"::warning::painel regulatório: {nome} sem leitura ({str(e)[:150]})")
            fp = fontes_prev.get(nome) or {}
            fontes[nome] = {"ok": False, "erro": str(e)[:200], "lido_em": fp.get("lido_em"),
                            "desde": fp.get("desde") if not fp.get("ok", True) else momento.isoformat(timespec="seconds")}
            if fp.get("mod"):
                fontes[nome]["mod"] = fp["mod"]
            return None

    consultas = []
    aneel, lido_pc = consultas_aneel(st_alertas, hoje)
    consultas += aneel
    saude = st_alertas.get("saude") or {}
    falhas_aneel = {n: s for n, s in saude.items() if n.startswith("ANEEL") and "pauta" not in n}
    fontes["ANEEL"] = {"ok": not falhas_aneel, "lido_em": momento.isoformat(timespec="seconds"),
                       "listagem_pc": lido_pc}
    if falhas_aneel:
        fontes["ANEEL"]["erro"] = "; ".join(f"{n}: {s.get('erro', '')[:80]}" for n, s in falhas_aneel.items())

    for nome, fn in (("MME", lambda: consultas_mme(hoje)), ("ARSESP", lambda: consultas_arsesp(hoje))):
        res = ler(nome, fn)
        if res is None:
            res = [c for c in prev.get("consultas", []) if c.get("orgao") == nome]
        consultas += res
    # ARSAE: todas as consultas das 2 páginas entram na linha do tempo (documento
    # final novo = resultado); no painel, só as em andamento.
    arsae_cache = st.get("arsae") or {}
    res = ler("ARSAE", lambda: consultas_arsae(hoje, arsae_cache))
    if res is not None:
        arsae_cache = {"mod": res[1], "consultas": res[0]}
    arsae_todas = arsae_cache.get("consultas") or []

    recs = ler("Pauta ANEEL", pauta_aneel)
    if recs is None:
        recs = []
    pauta_proc = pauta_por_processo(st.get("pauta_proc") or {}, hoje)
    processos, aberturas = processos_sei(recs, hoje, [r for c in pauta_proc.values() for r in c["recs"]])
    if not recs:  # pauta fora: mantém a da leitura anterior
        pauta_prev = {p["id"]: p.get("pauta", []) for p in prev.get("processos", [])}
        for p in processos:
            p["pauta"] = pauta_prev.get(p["id"], [])
        aberturas = [a for a in prev.get("pauta_aberturas", []) if a["data"] >= hoje]
    leituras = [p["lido_em"] for p in processos if p.get("lido_em")]
    ultima = max(leituras) if leituras else None
    horas = alerts._horas_desde(ultima) if ultima else float("inf")
    fontes["SEI"] = {"ok": horas <= 24, "lido_em": ultima,
                     **({"erro": "o SEI bloqueia o GitHub; os processos só são lidos com o PC ligado"} if horas > 24 else {})}

    # Projetos MME e TCU (projetos_watch.txt). Fonte fora → leitura anterior.
    watch = ler_projetos_watch()
    proj_prev = _json(saida.with_name(PROJETOS.name), {}).get("projetos", [])
    extras = []
    if any(w["tipo"] == "MME" for w in watch):
        res = ler("Projetos MME", lambda: projetos_mme(watch, hoje)) if (fontes.get("MME") or {}).get("ok") else None
        extras += res if res is not None else [p for p in proj_prev if p.get("tipo_projeto") == "MME"]
    if any(w["tipo"] == "TCU" for w in watch):
        extras += projetos_tcu(watch, st_alertas)
        lida = _json(TCU_PAUTA, {}).get("lido_em")
        fontes["TCU"] = {"ok": bool(lida) and alerts._horas_desde(lida) <= 48, "lido_em": lida,
                         **({} if lida and alerts._horas_desde(lida) <= 48 else
                            {"erro": "pauta do TCU sem leitura recente (dados abertos, 1x/dia)"})}
    if any(w["tipo"] == "PROGRAMA" for w in watch):
        extras += projetos_programa(st_alertas)
    if any(w["tipo"] == "ARSESP" for w in watch):
        extras += (projetos_arsesp(watch) if (fontes.get("ARSESP") or {}).get("ok")
                   else [p for p in proj_prev if p.get("tipo_projeto") == "ARSESP"])
    # A CP fixada também gera os eventos comuns (documento novo, prazo), com o
    # mesmo id da CP do painel: a chave do evento evita repetir.
    mme_fixadas = [{**p, "numero": p["numero"].replace("CP ", ""), "docs": [{**d, "u": None} for d in p["docs"]]}
                   for p in extras if p["tipo_projeto"] == "MME"]

    # Situação recalculada (a consulta da leitura anterior pode ter vencido hoje).
    for c in consultas:
        if c.get("fim"):
            c["situacao"] = _situacao(c["fim"], hoje)
    consultas = [c for c in consultas if _no_painel(c, hoje)]
    consultas.sort(key=lambda c: (c.get("fim") or "9999", c["orgao"], c["numero"]))

    vistos = list(st.get("vistos", []))
    arsesp_fixadas = [c for p in extras if p["tipo_projeto"] == "ARSESP" for c in p["itens"]]
    cands = candidatos(consultas + arsae_todas + mme_fixadas + arsesp_fixadas, processos, aberturas, prev, hoje, semente, set(vistos))
    cands += candidatos_extra(extras, hoje, semente, set(vistos))
    eventos, vistos, n_novos = atualiza_eventos(cands, prev.get("eventos", []), vistos, hoje, semente, momento)
    # Processo que saiu do sei_watch.txt sai também da linha do tempo (a pedido,
    # 11/11/2026: Reajuste RAP 2026). As chaves ficam em "vistos": se o processo
    # voltar, o histórico antigo não vira novidade.
    ids_sei = {f"sei:{p['id']}" for p in processos}
    eventos = [e for e in eventos if not (e.get("ref") or "").startswith("sei:") or e["ref"] in ids_sei]
    consultas += [c for c in arsae_todas if c["situacao"] == "em andamento"]

    # State (vistos + cache da ARSAE) sempre que mudar; o JSON do site, abaixo.
    novo_st = json.dumps({"vistos": vistos, "arsae": arsae_cache, "pauta_proc": pauta_proc}, ensure_ascii=False)
    if novo_st != json.dumps(st, ensure_ascii=False):
        STATE.write_text(novo_st, encoding="utf-8")

    # Aba Projetos: arquivo à parte (~300KB), que o site só baixa quando a aba abre.
    proj_path = saida.with_name(PROJETOS.name)
    proj = json.dumps({"projetos": [projeto(p) for p in processos] + extras}, ensure_ascii=False, separators=(",", ":"))
    try:
        mudou_proj = proj_path.read_text(encoding="utf-8") != proj
    except OSError:
        mudou_proj = True
    if mudou_proj:
        proj_path.write_text(proj, encoding="utf-8")

    dados = {
        "gerado_em": momento.isoformat(timespec="seconds"),
        "fontes": fontes,
        "consultas": consultas,
        "processos": [resumo_processo(p) for p in processos],
        "projetos_extra": [resumo_extra(p, hoje) for p in extras],
        "pauta_aberturas": aberturas,
        "eventos": eventos,
    }
    novo = json.dumps(dados, ensure_ascii=False, indent=1)
    # Sem mudança de conteúdo, não regrava (o commit do loop fica vazio): compara sem os horários.
    def _sem_horario(d):
        d = json.loads(json.dumps(d))
        d.pop("gerado_em", None)
        for f in d.get("fontes", {}).values():
            if f.get("ok"):
                f.pop("lido_em", None)
        return d
    # …mas regrava de hora em hora mesmo assim: o "atualizado às" do site é o gerado_em.
    try:
        idade_min = (momento - datetime.fromisoformat(prev["gerado_em"])).total_seconds() / 60
    except (KeyError, TypeError, ValueError):
        idade_min = float("inf")
    if prev and not semente and idade_min < 55 and _sem_horario(prev) == _sem_horario(dados):
        print(f"[regulatorio] sem mudança ({len(consultas)} consultas, {len(processos)} processos)")
        return
    saida.write_text(novo, encoding="utf-8")
    abertas = sum(1 for c in consultas if c["situacao"] == "aberta")
    print(f"[regulatorio] {len(consultas)} consultas ({abertas} abertas), {len(processos)} processos, "
          f"{len(aberturas)} aberturas na pauta, {n_novos} eventos novos"
          + (" (semente)" if semente else ""))


if __name__ == "__main__":
    main()
