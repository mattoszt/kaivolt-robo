#!/usr/bin/env python3
"""
ROBÔ DA KAIVOLT
Busca o menor preço (com boa qualidade) de cada produto nas lojas,
gera os links de afiliado, guarda o histórico de preços e publica
o arquivo ofertas.json que o site lê.

Uso:
  python robo.py            -> modo normal (usa as APIs reais)
  python robo.py --demo     -> modo teste, sem APIs (usa os preços de "exemplo")
  python robo.py --teste    -> mostra a resposta bruta das APIs (pra conferir)

As chaves ficam em variáveis de ambiente (NUNCA escreva elas neste arquivo):
  SHOPEE_APP_ID, SHOPEE_SECRET
  ALI_APP_KEY, ALI_SECRET, ALI_TRACKING_ID
  FTP_HOST, FTP_USER, FTP_PASS, FTP_PASTA   (para enviar ao site)
Só usa bibliotecas que já vêm com o Python (não precisa instalar nada).
"""
import hashlib, hmac, json, os, re, sys, time, urllib.parse, urllib.request, ftplib, io
from datetime import datetime, timezone, timedelta

PASTA = os.path.dirname(os.path.abspath(__file__))
ARQ_PRODUTOS = os.path.join(PASTA, "produtos.json")
ARQ_HISTORICO = os.path.join(PASTA, "historico.json")
ARQ_SAIDA = os.path.join(PASTA, "saida", "ofertas.json")

DEMO = "--demo" in sys.argv
TESTE = "--teste" in sys.argv


def log(*a):
    print("[kaivolt]", *a, flush=True)


def ler_json(caminho, padrao):
    try:
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return padrao


def salvar_json(caminho, dados):
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=1)


def http(url, dados=None, headers=None):
    req = urllib.request.Request(url, data=dados, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


REGRAS = {}                 # preenchido no main() com as regras do produtos.json
MOTIVOS = {}                # contagem de reprovados por motivo (relatório no final)


def reprova(motivo):
    MOTIVOS[motivo] = MOTIVOS.get(motivo, 0) + 1
    return False


def palavras_ok(titulo, palavras, excluir=None):
    t = (titulo or "").lower()
    proibidas = list(excluir or []) + list(REGRAS.get("excluir_global", []))
    if any(x.lower() in t for x in proibidas):
        return reprova("palavra proibida no título (réplica, usado, capa etc.)")
    if not all(p.lower() in t for p in (palavras or [])):
        return reprova("título não bate com o produto")
    return True


def tira_suspeitos(cands):
    """Preço bom demais pra ser verdade: bem abaixo da mediana dos concorrentes = golpe/falsificado."""
    fator = float(REGRAS.get("preco_suspeito_fator", 0.45))
    if len(cands) < 4:
        return cands
    precos = sorted(c["por"] for c in cands)
    mediana = precos[len(precos) // 2]
    bons = []
    for c in cands:
        if c["por"] < mediana * fator:
            reprova("preço bom demais pra ser verdade")
            log(f"    suspeito descartado: R$ {c['por']:.2f} (mediana R$ {mediana:.2f}) · {(c.get('titulo') or '')[:50]}")
        else:
            bons.append(c)
    return bons


def faixa_ok(por, cfg):
    if cfg.get("preco_min") and por < float(cfg["preco_min"]):
        return reprova("abaixo do preço mínimo do produto (suspeito)")
    if cfg.get("preco_max") and por > float(cfg["preco_max"]):
        return reprova("acima do preço máximo do produto")
    return True


# ======================================================================
# SHOPEE — Open API de Afiliados (GraphQL)
# ======================================================================
SHOPEE_URL = "https://open-api.affiliate.shopee.com.br/graphql"


def shopee_chamar(query):
    app_id, secret = os.environ.get("SHOPEE_APP_ID"), os.environ.get("SHOPEE_SECRET")
    if not app_id or not secret:
        raise RuntimeError("faltam SHOPEE_APP_ID / SHOPEE_SECRET")
    corpo = json.dumps({"query": query}, separators=(",", ":"))
    ts = str(int(time.time()))
    assinatura = hashlib.sha256((app_id + ts + corpo + secret).encode()).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"SHA256 Credential={app_id}, Timestamp={ts}, Signature={assinatura}",
    }
    resp = http(SHOPEE_URL, corpo.encode(), headers)
    if TESTE:
        log("SHOPEE resposta:", json.dumps(resp, ensure_ascii=False)[:1500])
    if resp.get("errors"):
        raise RuntimeError(f"Shopee: {resp['errors']}")
    return resp["data"]


def shopee_melhor(cfg, regras):
    campos = "itemId shopId productName price priceMin priceMax priceDiscountRate sales ratingStar imageUrl offerLink productLink"
    if cfg.get("itemId"):
        filtro = f'itemId: {int(cfg["itemId"])}' + (f', shopId: {int(cfg["shopId"])}' if cfg.get("shopId") else "")
    else:
        filtro = f'keyword: {json.dumps(cfg["busca"])}, sortType: 2'   # 2 = mais vendidos
    q = f"{{ productOfferV2({filtro}, page: 1, limit: 30) {{ nodes {{ {campos} }} }} }}"
    nodes = shopee_chamar(q)["productOfferV2"]["nodes"] or []

    candidatos = []
    for n in nodes:
        nota = float(n.get("ratingStar") or 0)
        vendas = int(n.get("sales") or 0)
        if nota < regras["nota_minima"]:
            reprova("nota baixa"); continue
        if vendas < regras["vendas_minimas_shopee"]:
            reprova("poucas vendas"); continue
        if not palavras_ok(n.get("productName"), cfg.get("palavras"), cfg.get("excluir")):
            continue
        por = float(n.get("priceMin") or n.get("price") or 0)
        if por <= 0 or not faixa_ok(por, cfg):
            continue
        taxa = float(n.get("priceDiscountRate") or 0)
        de = round(por / (1 - taxa / 100), 2) if 0 < taxa < 95 else por
        link = n.get("offerLink") or shopee_link_curto(n.get("productLink"))
        candidatos.append({"loja": "Shopee", "de": de, "por": por, "link": link, "pid": str(n.get("itemId") or ""),
                           "nota": nota, "vendas": vendas, "titulo": n.get("productName"),
                           "img": n.get("imageUrl")})
    ordem = custo_beneficio(tira_suspeitos(candidatos))
    return ordem[0] if ordem else None


def shopee_link_curto(url):
    if not url:
        return ""
    q = f'mutation {{ generateShortLink(input: {{ originUrl: {json.dumps(url)}, subIds: ["kaivolt"] }}) {{ shortLink }} }}'
    return shopee_chamar(q)["generateShortLink"]["shortLink"]


# ======================================================================
# ALIEXPRESS — Open Platform (Affiliate API)
# ======================================================================
ALI_URL = "https://api-sg.aliexpress.com/sync"


def ali_chamar(metodo, params):
    key, secret = os.environ.get("ALI_APP_KEY"), os.environ.get("ALI_SECRET")
    if not key or not secret:
        raise RuntimeError("faltam ALI_APP_KEY / ALI_SECRET")
    p = {"app_key": key, "method": metodo, "sign_method": "sha256",
         "timestamp": str(int(time.time() * 1000)), "format": "json", "v": "2.0"}
    p.update({k: str(v) for k, v in params.items()})
    base = "".join(k + p[k] for k in sorted(p))
    p["sign"] = hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest().upper()
    resp = http(ALI_URL + "?" + urllib.parse.urlencode(p))
    if TESTE:
        log("ALIEXPRESS resposta:", json.dumps(resp, ensure_ascii=False)[:1500])
    if "error_response" in resp:
        raise RuntimeError(f"AliExpress: {resp['error_response']}")
    return resp


def ali_produtos(resp):
    """Acha a lista de produtos dentro da resposta, seja qual for o método."""
    for v in resp.values():
        try:
            r = v["resp_result"]["result"]
            return r["products"]["product"]
        except (KeyError, TypeError):
            continue
    return []


def ali_num(v):
    try:
        return float(str(v).replace("%", "").replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def ali_link_produto(c, tracking):
    """Gera o link de afiliado que abre direto no produto escolhido (None se não der)."""
    pid = c.get("pid")
    urls = [u for u in [c.get("url_prod"),
                        f"https://www.aliexpress.com/item/{pid}.html" if pid else None,
                        f"https://pt.aliexpress.com/item/{pid}.html" if pid else None] if u]
    for url in dict.fromkeys(urls):
        try:
            resp = ali_chamar("aliexpress.affiliate.link.generate",
                              {"promotion_link_type": 0, "source_values": url, "tracking_id": tracking})
            for v in resp.values():
                try:
                    links = v["resp_result"]["result"]["promotion_links"]["promotion_link"]
                    if links and links[0].get("promotion_link"):
                        return links[0]["promotion_link"]
                except (KeyError, TypeError):
                    continue
        except Exception as e:
            log("    aviso: link.generate falhou:", e)
    return None


def custo_beneficio(cands):
    """Entre os que custam até 15% a mais que o mais barato, fica o de melhor nota, mais vendas e maior desconto."""
    import math
    if not cands:
        return []
    menor = min(c["por"] for c in cands)
    def score(c):
        # o "preço de antes" das lojas não entra: é fácil de inflar
        return (c.get("nota") or 0) * 2 + math.log10(max(c.get("vendas") or 1, 1)) - (c["por"] / menor - 1) * 4
    faixa = [c for c in cands if c["por"] <= menor * 1.15]
    resto = [c for c in cands if c not in faixa]
    return sorted(faixa, key=score, reverse=True) + sorted(resto, key=lambda c: c["por"])


def ali_melhor(cfg, regras):
    tracking = os.environ.get("ALI_TRACKING_ID", "kaivolt")
    comum = {"target_currency": "BRL", "target_language": "PT", "ship_to_country": "BR",
             "tracking_id": tracking}
    if cfg.get("productId"):
        resp = ali_chamar("aliexpress.affiliate.productdetail.get",
                          dict(comum, product_ids=cfg["productId"]))
    else:
        resp = ali_chamar("aliexpress.affiliate.product.query",
                          dict(comum, keywords=cfg["busca"], sort="LAST_VOLUME_DESC",
                               page_size=40, page_no=1))
    candidatos = []
    vmin = regras.get("vendas_minimas_aliexpress", 100)
    itens = ali_produtos(resp)
    log(f"    AliExpress trouxe {len(itens)} resultados")
    for it in itens:
        aval = ali_num(it.get("evaluate_rate"))
        vendas = int(ali_num(it.get("lastest_volume")))
        if not aval or aval < regras["avaliacao_minima_aliexpress"]:   # sem avaliação = descarta
            reprova("avaliação baixa ou sem avaliação"); continue
        if vendas < vmin:                                               # poucas vendas = descarta
            reprova("poucas vendas"); continue
        if not palavras_ok(it.get("product_title"), cfg.get("palavras"), cfg.get("excluir")):
            continue
        por = ali_num(it.get("target_sale_price") or it.get("sale_price"))
        de = ali_num(it.get("target_original_price") or it.get("original_price")) or por
        link = it.get("promotion_link") or ""
        if por <= 0 or not link or not faixa_ok(por, cfg):
            continue
        pid = str(it.get("product_id") or "")
        url_prod = it.get("product_detail_url") or (f"https://pt.aliexpress.com/item/{pid}.html" if pid else "")
        candidatos.append({"loja": "AliExpress", "de": de, "por": por, "link": link, "url_prod": url_prod, "pid": pid,
                           "nota": round(aval / 20, 1), "vendas": vendas,
                           "titulo": it.get("product_title"),
                           "img": it.get("product_main_image_url")})
    ordem = custo_beneficio(tira_suspeitos(candidatos))
    for c in ordem[:3]:
        log(f"    opção R$ {c['por']:.2f} · {c['nota']}★ · {c['vendas']} vendas · {(c['titulo'] or '')[:58]}")
    for c in ordem[:5]:
        link = ali_link_produto(c, tracking)
        if link:
            c["link"] = link
            return c
    if ordem:
        log("    AliExpress: não consegui link direto pro produto; ficou de fora hoje")
    return None


# ======================================================================
# MERCADO LIVRE / AMAZON — links e preços colocados à mão no produtos.json
# ======================================================================
def manual_melhor(loja, cfg):
    """Mercado Livre/Amazon: o produto é escolhido por você; o robô confere se continua valendo."""
    if not cfg.get("link"):
        return None
    o = None
    if loja == "Mercado Livre":
        o = ml_melhor(cfg) if (cfg.get("url") or cfg.get("item")) else None
        if o is False:                       # anúncio acabou/pausado: fora do site
            reprova("anúncio do Mercado Livre pausado/finalizado")
            return None
        if not o and cfg.get("link_meli"):
            o = ml_preco_pagina(cfg)
    if o:
        if not faixa_ok(o["por"], cfg):
            log(f"  {loja}: preço R$ {o['por']:.2f} fora da faixa do produto, ficou de fora")
            return None
        return o
    # sem preço ao vivo: só usa o preço digitado se ele for recente
    if not cfg.get("por"):
        return None
    dias = int(REGRAS.get("dias_validade_manual", 7))
    try:
        idade = (datetime.now(timezone.utc).date() - datetime.strptime(cfg.get("verificado_em", "2000-01-01"), "%Y-%m-%d").date()).days
    except ValueError:
        idade = 999
    if idade > dias:
        reprova("preço manual velho (não deu pra conferir)")
        log(f"  {loja}: preço não conferido há {idade} dias, ficou de fora pra não mostrar preço errado")
        return None
    por = float(cfg["por"])
    return {"loja": loja, "de": float(cfg.get("de") or por), "por": por, "link": cfg["link"],
            "pid": str(cfg.get("item") or ""), "titulo": cfg.get("titulo_ml") or cfg.get("titulo")}


# ======================================================================
# MERCADO LIVRE — API oficial (só PREÇO; o link de afiliado é o que você gerou)
# ======================================================================
ML_API = "https://api.mercadolibre.com"
_ML_TOKEN = {}


def ml_token():
    cid, sec = os.environ.get("ML_CLIENT_ID"), os.environ.get("ML_CLIENT_SECRET")
    if not cid or not sec:
        return None
    if "t" in _ML_TOKEN:
        return _ML_TOKEN["t"]
    try:
        dados = urllib.parse.urlencode({"grant_type": "client_credentials",
                                        "client_id": cid, "client_secret": sec}).encode()
        r = http(ML_API + "/oauth/token", dados,
                 {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
        _ML_TOKEN["t"] = r.get("access_token")
    except Exception as e:
        log("  Mercado Livre: não consegui token da API:", e)
        _ML_TOKEN["t"] = None
    return _ML_TOKEN["t"]


def ml_get(caminho):
    tok = ml_token()
    h = {"Accept": "application/json", "User-Agent": "robo-kaivolt"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    return http(ML_API + caminho, None, h)


def ml_melhor(cfg):
    """Atualiza o preço do anúncio escolhido. O link continua sendo o SEU link de afiliado."""
    alvo = cfg.get("item") or cfg.get("url") or ""
    m_prod = re.search(r"/p/(MLB\d+)", alvo)
    m_item = re.search(r"(MLB)-?(\d{6,})", alvo)
    try:
        if m_prod:
            d = ml_get(f"/products/{m_prod.group(1)}")
            bw = d.get("buy_box_winner") or {}
            por, de = bw.get("price"), bw.get("original_price")
        elif m_item:
            iid = m_item.group(1) + m_item.group(2)
            d = ml_get(f"/items/{iid}")
            if d.get("status") and d["status"] != "active":
                log("  Mercado Livre: anúncio pausado/finalizado, ficou de fora")
                return False
            por, de = d.get("price"), d.get("original_price")
        else:
            return None
        if not por:
            return None
        por = float(por); de = float(de or cfg.get("de") or por)
        log(f"  Mercado Livre: preço atualizado pela API -> R$ {por:.2f}")
        return {"loja": "Mercado Livre", "de": max(de, por), "por": por, "link": cfg["link"],
                "pid": str(cfg.get("item") or ""), "titulo": cfg.get("titulo_ml")}
    except Exception as e:
        log("  Mercado Livre: API não respondeu (", e, "), conferindo pela página")
        return None


def ml_preco_pagina(cfg):
    """Abre o teu link meli.la e lê o preço do produto em destaque (o mesmo que você escolheu)."""
    try:
        req = urllib.request.Request(cfg["link_meli"], headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36",
            "Accept-Language": "pt-BR"})
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read().decode("utf-8", "ignore")
        i = html.find('"polycards":[')
        if i < 0:
            log("  Mercado Livre: página do link não trouxe o produto")
            return None
        card = json.JSONDecoder().raw_decode(html[i + len('"polycards":'):])[0][0]
        iid = card.get("metadata", {}).get("id")
        if cfg.get("item") and iid and iid != cfg["item"]:
            log(f"  Mercado Livre: o link mostra outro anúncio ({iid}), confira o produto")
            return None
        comp = {x.get("type"): x for x in card.get("components", [])}
        preco = comp.get("price", {}).get("price", {})
        por = (preco.get("current_price") or {}).get("value")
        if not por:
            return None
        log(f"  Mercado Livre: preço conferido na página -> R$ {float(por):.2f}")
        fotos = (card.get("pictures") or {}).get("pictures") or []
        img = f"https://http2.mlstatic.com/D_NQ_NP_{fotos[0]['id']}-O.jpg" if fotos and fotos[0].get("id") else ""
        return {"loja": "Mercado Livre", "de": float(por), "por": float(por), "link": cfg["link"],
                "pid": str(cfg.get("item") or ""), "titulo": cfg.get("titulo_ml"), "img": img}
    except Exception as e:
        log("  Mercado Livre: não consegui abrir a página do link (", e, ")")
        return None


# ======================================================================
# MONTAGEM
# ======================================================================
def buscar_ofertas(prod, regras):
    ofertas = []
    for loja, cfg in prod.get("lojas", {}).items():
        if loja not in regras["lojas_ativas"]:
            continue
        try:
            if DEMO:
                ex = prod.get("exemplo", {}).get(loja)
                o = {"loja": loja, "de": ex[0], "por": ex[1], "link": "#"} if ex else None
            elif loja == "Shopee":
                o = shopee_melhor(cfg, regras)
            elif loja == "AliExpress":
                o = ali_melhor(cfg, regras)
            else:
                o = manual_melhor(loja, cfg)
            if o:
                ofertas.append(o)
            elif loja in ("Shopee", "AliExpress"):
                log(f"  {loja}: nenhuma oferta passou nos filtros de qualidade")
            else:
                log(f"  {loja}: sem link/preço preenchido no produtos.json (pulando)")
        except Exception as e:           # uma loja com erro não derruba as outras
            log(f"  {loja}: ERRO -> {e}")
    return sorted(ofertas, key=lambda o: o["por"])


def calcular_selo(hist, preco_hoje):
    """'real' = hoje é o menor preço dos últimos 30 dias (com pelo menos 7 dias de histórico)."""
    limite = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    antigos = [v for d, v in hist.items() if d >= limite and d != hoje()]
    if len(antigos) < 7:
        return ""
    return "real" if preco_hoje <= min(antigos) else ""


def desconto_real(hist_anuncio, por):
    """'De' honesto: o maior preço que o PRÓPRIO robô viu nesse mesmo anúncio nos últimos 30 dias.
    O 'preço de antes' que as lojas mostram é ignorado (o AliExpress infla esse número).
    Se o preço não caiu pelo menos 5%, não mostra desconto nenhum."""
    limite = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    vistos = [v for d, v in hist_anuncio.items() if d >= limite]
    maior = max(vistos, default=por)
    return round(maior, 2) if maior >= por * 1.05 else por


def hoje():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def main():
    base = ler_json(ARQ_PRODUTOS, None)
    if not base:
        sys.exit("produtos.json não encontrado")
    regras = base["regras"]
    REGRAS.update(regras)
    historico = ler_json(ARQ_HISTORICO, {})
    saida = []

    for prod in base["produtos"]:
        log("→", prod["n"])
        ofertas = buscar_ofertas(prod, regras)
        if not ofertas:
            log("  (sem ofertas hoje, produto fica fora do site)")
            continue
        menor = ofertas[0]["por"]
        h = historico.setdefault(prod["id"], {})
        h[hoje()] = menor
        for d in sorted(h)[:-90]:        # guarda só 90 dias
            del h[d]
        anuncios = historico.setdefault("_anuncios", {})
        for o in ofertas:
            ha = anuncios.setdefault(f'{prod["id"]}|{o["loja"]}|{o.get("pid") or o["link"][:80]}', {})
            ha[hoje()] = max(ha.get(hoje(), 0), o["por"])      # maior preço visto no dia
            for d in sorted(ha)[:-35]:
                del ha[d]
            o["de"] = desconto_real(ha, o["por"])
        notas = [o["nota"] for o in ofertas if o.get("nota")]
        vendas = [o["vendas"] for o in ofertas if o.get("vendas")]
        img = prod.get("img") or ""
        saida.append({
            "id": prod["id"], "n": prod["n"], "c": prod["c"], "ic": prod.get("ic", ""),
            "img": img, "nota": round(max(notas), 1) if notas else 4.5,
            "vendas": max(vendas) if vendas else 0,
            "selo": calcular_selo(h, menor),
            "ofertas": [{"loja": o["loja"], "de": round(o["de"], 2), "por": round(o["por"], 2),
                         "link": o["link"], "t": (o.get("titulo") or "")[:90],
                         "img": o.get("img") or ""} for o in ofertas],
        })
        log("  menor preço:", menor, "em", ofertas[0]["loja"])

    if not saida:
        log("NENHUM produto encontrado — o site continua com as ofertas anteriores (nada foi enviado).")
        salvar_json(ARQ_HISTORICO, historico)
        sys.exit(1)

    log("RELATÓRIO DE QUALIDADE — reprovados por motivo:")
    for m, q in sorted(MOTIVOS.items(), key=lambda x: -x[1]):
        log(f"   {q:4d} x {m}")

    # trava: se sumiu muita coisa de uma vez, algo deu errado (API fora do ar etc.) -> não mexe no site
    meta = historico.setdefault("_meta", {})
    antes = int(meta.get("ultimo_total") or 0)
    minimo = float(regras.get("minimo_produtos_publicar", 0.6))
    if antes and len(saida) < antes * minimo:
        log(f"TRAVA DE SEGURANÇA: hoje {len(saida)} produtos, antes {antes}. O site continua com as ofertas anteriores.")
        meta["ultimo_total"] = max(len(saida), int(antes * 0.9))   # se continuar assim, libera aos poucos
        salvar_json(ARQ_HISTORICO, historico)
        sys.exit(1)
    meta["ultimo_total"] = len(saida)

    resultado = {"atualizado": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                 "produtos": saida}
    salvar_json(ARQ_SAIDA, resultado)
    log(f"ok: {len(saida)} produtos em {ARQ_SAIDA}")

    zap = None
    try:
        zap = divulgar(saida, historico, regras)
    except Exception as e:                      # divulgação nunca derruba o site
        log("Divulgação: ERRO ->", e)
    salvar_json(ARQ_HISTORICO, historico)

    if not DEMO and os.environ.get("FTP_HOST"):
        enviar_ftp(resultado, {"zap.html": zap} if zap else None)


# ======================================================================
# DIVULGAÇÃO — posta sozinho no canal do Telegram e prepara os posts do WhatsApp
# ======================================================================
TZ_BR = timezone(timedelta(hours=-3))
SITE = "kaivolt.com.br"


def reais(v):
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def vendidos(v):
    if v >= 1000:
        return f"+{int(v / 100) / 10:g} mil vendidos".replace(".", ",")
    return f"+{v} vendidos"


def queda(p):
    o = p["ofertas"][0]
    return round((1 - o["por"] / o["de"]) * 100) if o.get("de") and o["de"] > o["por"] else 0


def montar_post(p, estilo):
    """estilo 'tg' = HTML do Telegram · 'zap' = negrito do WhatsApp (*assim*)."""
    o, q = p["ofertas"][0], queda(p)
    b = (lambda s: f"<b>{s}</b>") if estilo == "tg" else (lambda s: f"*{s}*")
    esc = (lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")) if estilo == "tg" else (lambda s: s)
    L = [b(esc(p["n"])), ""]
    if q:
        L.append(f"🔻 Caiu {q}%: era {reais(o['de'])} nos últimos dias")
    L.append(f"💰 {b(reais(o['por']))} no {o['loja']}")
    if o["loja"] == "AliExpress":
        L.append("Internacional · chega em 1 a 4 semanas")
    if p.get("vendas", 0) >= 10:
        L.append(vendidos(p["vendas"]) + " na loja")
    outras = [f"{x['loja']} {reais(x['por'])}" for x in p["ofertas"][1:3]]
    if outras:
        L.append("Também em: " + " · ".join(outras))
    L.append("")
    if estilo == "tg":
        L.append(f'👉 <a href="{o["link"]}">Ver oferta no {o["loja"]}</a>')
        L.append(f'Compare todas as lojas: <a href="https://{SITE}">{SITE}</a>')
        L.append("")
        L.append("<i>Preço pode mudar a qualquer momento.</i>")
    else:
        L.append(f"👉 {o['link']}")
        L.append(f"Compare todas as lojas: {SITE}")
        L.append("")
        L.append("_Preço pode mudar a qualquer momento._")
    return "\n".join(L)


def tg_api(metodo, dados):
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    corpo = urllib.parse.urlencode(dados).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{metodo}", data=corpo)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode() or "{}")


def tg_postar(p):
    chat = os.environ["TELEGRAM_CHAT_ID"].strip()
    texto, img = montar_post(p, "tg"), p["ofertas"][0].get("img")
    r = {}
    if img:
        r = tg_api("sendPhoto", {"chat_id": chat, "photo": img, "caption": texto, "parse_mode": "HTML"})
    if not r.get("ok"):
        r = tg_api("sendMessage", {"chat_id": chat, "text": texto, "parse_mode": "HTML"})
    if r.get("ok"):
        log("  Telegram: postado ->", p["n"])
        return True
    log("  Telegram: NÃO postou ->", r.get("description") or r)
    return False


def divulgar(saida, historico, regras):
    """Regras do canal:
    - posta só entre 9h e 22h (Brasília) e no máximo 'telegram_max_dia' posts por dia
    - QUEDA REAL: produto que caiu 10% ou mais (pelo histórico do robô) é postado na hora
    - DESTAQUE: às 12h e às 20h posta o produto mais vendido que não aparece há 5 dias
    - não repete o mesmo produto em 3 dias, a não ser que o preço caia mais 5%"""
    st = historico.setdefault("_divulgacao", {"postados": {}, "slots": {}, "fila_zap": []})
    agora = datetime.now(TZ_BR)
    dia = agora.strftime("%Y-%m-%d")
    forcar = "--tg-teste" in sys.argv
    feitos_hoje = sum(1 for v in st["postados"].values() if v["d"].startswith(dia))
    max_dia = int(regras.get("telegram_max_dia", 6))

    def dias_desde(pid):
        v = st["postados"].get(pid)
        if not v:
            return 999
        return (agora.replace(tzinfo=None) - datetime.strptime(v["d"], "%Y-%m-%d %H:%M")).days

    escolhidos = []
    if forcar or (9 <= agora.hour < 22 and feitos_hoje < max_dia):
        # 1) quedas reais de preço
        quedas = sorted([p for p in saida if queda(p) >= int(regras.get("queda_minima_alerta", 10))], key=queda, reverse=True)
        for p in quedas:
            ult = st["postados"].get(p["id"])
            if dias_desde(p["id"]) >= 3 or (ult and p["ofertas"][0]["por"] <= ult["p"] * 0.95):
                escolhidos.append(p)
            if len(escolhidos) >= 2:
                break
        # 2) destaque das 12h e das 20h
        slot = f"{dia}-{agora.hour}"
        if forcar or (agora.hour in (12, 20) and slot not in st["slots"]):
            livres = [p for p in saida if dias_desde(p["id"]) >= 5 and p not in escolhidos] or [p for p in saida if p not in escolhidos]
            livres.sort(key=lambda p: (p.get("vendas", 0), len(p["ofertas"])), reverse=True)
            if livres:
                escolhidos.append(livres[0])
            st["slots"][slot] = 1
        escolhidos = escolhidos[:max(0, max_dia - feitos_hoje)] if not forcar else escolhidos[:1]

    tem_tg = os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")
    for p in escolhidos:
        if tem_tg and not DEMO:
            tg_postar(p)
        st["postados"][p["id"]] = {"d": agora.strftime("%Y-%m-%d %H:%M"), "p": p["ofertas"][0]["por"]}
        st["fila_zap"].insert(0, {"d": agora.strftime("%d/%m %H:%M"), "n": p["n"], "txt": montar_post(p, "zap")})
    if escolhidos and not tem_tg:
        log("  Telegram: faltam TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID nos Secrets (post só foi pra página do WhatsApp)")
    st["fila_zap"] = st["fila_zap"][:15]
    for k in sorted(st["slots"])[:-20]:
        del st["slots"][k]
    return pagina_zap(st["fila_zap"])


def pagina_zap(fila):
    """Página kaivolt.com.br/ofertas/zap.html: os últimos posts prontos pra copiar e colar no canal do WhatsApp."""
    import html as H
    itens = "".join(
        f'<div class="p"><div class="h"><b>{H.escape(x["n"])}</b><span>{x["d"]}</span></div>'
        f'<pre id="t{i}">{H.escape(x["txt"])}</pre>'
        f'<div class="b"><button onclick="cp({i},this)">Copiar</button>'
        f'<a href="https://wa.me/?text={urllib.parse.quote(x["txt"])}">Abrir no WhatsApp</a></div></div>'
        for i, x in enumerate(fila)) or "<p>Nenhum post ainda. O robô posta às 12h, às 20h e quando algum preço cai de verdade.</p>"
    return ("<!doctype html><html lang=pt-BR><head><meta charset=utf-8><meta name=robots content=noindex>"
            "<meta name=viewport content='width=device-width,initial-scale=1'><title>Posts WhatsApp · Kaivolt</title>"
            "<style>body{margin:0;background:#07060b;color:#f4f2ff;font-family:system-ui,sans-serif;padding:16px}"
            "h1{font-size:20px}.p{background:#13101d;border:1px solid #2a2340;border-radius:14px;padding:14px;margin:0 0 14px}"
            ".h{display:flex;justify-content:space-between;gap:10px;margin-bottom:8px}.h span{color:#9d97b3;font-size:13px}"
            "pre{white-space:pre-wrap;font:14px/1.45 system-ui,sans-serif;margin:0 0 12px;color:#d9d4ee}"
            ".b{display:flex;gap:8px}button,.b a{flex:1;text-align:center;padding:12px;border-radius:10px;border:0;font-weight:700;"
            "font-size:15px;text-decoration:none}button{background:#8b5cf6;color:#fff}.b a{background:#1f9d55;color:#fff}</style></head>"
            "<body><h1>Posts prontos pro WhatsApp</h1><p style='color:#9d97b3'>Toque em Copiar e cole no canal. O mais novo fica em cima.</p>"
            + itens +
            "<script>function cp(i,b){navigator.clipboard.writeText(document.getElementById('t'+i).innerText).then(function(){b.textContent='Copiado!';setTimeout(function(){b.textContent='Copiar'},1500)})}</script>"
            "</body></html>")


def limpa_host(h):
    h = (h or "").strip()
    for pre in ("ftps://", "ftp://", "sftp://", "http://", "https://"):
        if h.lower().startswith(pre):
            h = h[len(pre):]
    return h.strip("/").split("/")[0].split(":")[0]


def enviar_ftp(resultado, extras=None):
    host = limpa_host(os.environ["FTP_HOST"])
    user, senha = os.environ["FTP_USER"].strip(), os.environ["FTP_PASS"]
    dominio = os.environ.get("SITE_DOMINIO", "kaivolt.com.br")
    dados = json.dumps(resultado, ensure_ascii=False).encode("utf-8")
    try:
        ftp = ftplib.FTP_TLS(host, timeout=60); ftp.login(user, senha); ftp.prot_p(); modo = "FTPS"
    except (ftplib.error_perm, OSError, EOFError) as e:
        if "Name or service" in str(e):
            raise RuntimeError(f"FTP_HOST '{host}' não existe. Use o IP/host exato da tela Contas FTP da Hostinger.") from e
        log("FTPS indisponível, tentando FTP normal:", e)
        ftp = ftplib.FTP(host, timeout=60); ftp.login(user, senha); modo = "FTP"
    with ftp:
        def lista():
            try:
                return [n.rsplit("/", 1)[-1] for n in ftp.nlst()]
            except ftplib.error_perm:
                return []
        log("FTP conectado em:", ftp.pwd(), "| conteúdo:", ", ".join(lista()[:12]))
        # procura a pasta raiz do site (onde está o WordPress)
        for _ in range(4):
            nomes = lista()
            if "wp-config.php" in nomes or "wp-content" in nomes:
                break
            if "public_html" in nomes:
                ftp.cwd("public_html"); continue
            if "domains" in nomes:
                ftp.cwd("domains"); continue
            if dominio in nomes:
                ftp.cwd(dominio); continue
            break
        raiz = ftp.pwd()
        log("pasta do site encontrada:", raiz)
        try:
            ftp.cwd("ofertas")
        except ftplib.error_perm:
            ftp.mkd("ofertas"); ftp.cwd("ofertas")
        ftp.storbinary("STOR ofertas.json", io.BytesIO(dados))
        log(f"enviado via {modo} para:", ftp.pwd() + "/ofertas.json")
        for nome, conteudo in (extras or {}).items():
            ftp.storbinary(f"STOR {nome}", io.BytesIO(conteudo.encode("utf-8") if isinstance(conteudo, str) else conteudo))
            log("enviado também:", ftp.pwd() + "/" + nome)
    # confere se o site já está servindo o arquivo
    try:
        url = f"https://{dominio}/ofertas/ofertas.json?v={int(time.time())}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 robo-kaivolt"})
        with urllib.request.urlopen(req, timeout=30) as r:
            log("site OK:", url.split("?")[0], "->", r.status)
    except Exception as e:
        log("ATENÇÃO: o site ainda não mostra o arquivo:", e)


if __name__ == "__main__":
    main()
