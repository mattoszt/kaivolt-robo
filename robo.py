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
import functools, hashlib, hmac, json, math, os, re, sys, time, unicodedata, urllib.error, urllib.parse, urllib.request, ftplib, io
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
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:           # mostra o motivo que a loja mandou (sem mostrar chaves)
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}")


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

    try:
        achados = achados_site(regras, historico)
    except Exception as e:                      # achados nunca derrubam o site
        log("Achados: ERRO ->", e)
        achados = []
    log(f"Achados do dia no site: {len(achados)}")
    resultado = {"atualizado": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                 "produtos": saida, "achados": achados}
    salvar_json(ARQ_SAIDA, resultado)
    log(f"ok: {len(saida)} produtos em {ARQ_SAIDA}")

    zap = None
    if "--sem-canal" not in sys.argv:           # com o workflow do canal separado, quem posta é ele
        try:
            zap = divulgar(saida, historico, regras)
        except Exception as e:                  # divulgação nunca derruba o site
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


def na_loja(loja):
    return "na Shopee" if loja == "Shopee" else f"no {loja}"


def montar_post(p, estilo):
    """estilo 'tg' = HTML do Telegram · 'zap' = negrito do WhatsApp (*assim*)."""
    o, q = p["ofertas"][0], queda(p)
    b = (lambda s: f"<b>{s}</b>") if estilo == "tg" else (lambda s: f"*{s}*")
    esc = (lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")) if estilo == "tg" else (lambda s: s)
    L = [b(esc(p["n"])), ""]
    if q:
        L.append(f"🔻 Caiu {q}%: era {reais(o['de'])} nos últimos dias")
    L.append(f"💰 {b(reais(o['por']))} {na_loja(o['loja'])}")
    if p.get("oferta_dia"):
        L.append("🏷️ Oferta do dia do Mercado Livre")
    if o["loja"] == "AliExpress":
        L.append("Internacional · chega em 1 a 4 semanas")
    if p.get("vendas", 0) >= 10:
        nota = f"⭐ {p['nota']:.1f}".replace(".", ",") + " · " if p.get("garimpo") and p.get("nota") else ""
        L.append(nota + vendidos(p["vendas"]) + " na loja")
    outras = [f"{x['loja']} {reais(x['por'])}" for x in p["ofertas"][1:3]]
    if outras:
        L.append("Também em: " + " · ".join(outras))
    L.append("")
    if estilo == "tg":
        L.append(f'👉 <a href="{o["link"]}">Ver oferta {na_loja(o["loja"])}</a>')
        L.append(f'Mais ofertas comparadas: <a href="https://{SITE}">{SITE}</a>' if p.get("garimpo") else f'Compare todas as lojas: <a href="https://{SITE}">{SITE}</a>')
        L.append("")
        L.append("<i>Preço pode mudar a qualquer momento.</i>")
    else:
        L.append(f"👉 {o['link']}")
        L.append(f"Mais ofertas comparadas: {SITE}" if p.get("garimpo") else f"Compare todas as lojas: {SITE}")
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


def titulo_curto(t, n=75):
    t = re.sub(r"\s+", " ", (t or "").strip())
    if len(t) <= n:
        return t
    return t[:n].rsplit(" ", 1)[0].rstrip(",.-–/ ") + "..."


def garimpo_ali(regras, st):
    """Garimpo: busca produtos NOVOS no AliExpress (fora do catálogo do site), com filtro de qualidade
    mais rígido, pra ter oferta o dia todo no canal sem repetir."""
    if not (os.environ.get("ALI_APP_KEY") and os.environ.get("ALI_SECRET")):
        return None
    buscas = regras.get("garimpo_buscas") or []
    if not buscas:
        return None
    tracking = os.environ.get("ALI_TRACKING_ID", "kaivolt")
    vmin = int(regras.get("garimpo_vendas_min", 500))
    amin = float(regras.get("garimpo_avaliacao_min", 94))
    pmin, pmax = float(regras.get("garimpo_preco_min", 15)), float(regras.get("garimpo_preco_max", 400))
    proibidas = [x.lower() for x in regras.get("garimpo_excluir", [])]
    ja = st.setdefault("garimpo", {})
    limite = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    for tentativa in range(3):                       # até 3 buscas diferentes por post
        n = st.get("kw", 0)
        kw = buscas[n % len(buscas)]
        pagina = (n // len(buscas)) % 3 + 1          # cada volta completa na lista usa a próxima página
        st["kw"] = n + 1
        log(f"  Garimpo: buscando '{kw}'")
        try:
            resp = ali_chamar("aliexpress.affiliate.product.query", {
                "target_currency": "BRL", "target_language": "PT", "ship_to_country": "BR",
                "tracking_id": tracking, "keywords": kw, "sort": "LAST_VOLUME_DESC", "page_size": 40, "page_no": pagina})
        except Exception as e:
            log("  Garimpo: AliExpress não respondeu:", e)
            return None
        cands = []
        for it in ali_produtos(resp):
            pid = str(it.get("product_id") or "")
            titulo = it.get("product_title") or ""
            aval, vendas = ali_num(it.get("evaluate_rate")), int(ali_num(it.get("lastest_volume")))
            por = ali_num(it.get("target_sale_price") or it.get("sale_price"))
            if not pid or ja.get(pid, "") >= limite:     # já postado nos últimos 30 dias
                continue
            if not aval or aval < amin or vendas < vmin or not (pmin <= por <= pmax):
                continue
            if any(x in titulo.lower() for x in proibidas) or not palavras_ok(titulo, None, None):
                continue
            cands.append({"loja": "AliExpress", "de": por, "por": por, "pid": pid, "titulo": titulo,
                          "nota": round(aval / 20, 1), "vendas": vendas, "img": it.get("product_main_image_url"),
                          "link": it.get("promotion_link") or "",
                          "url_prod": it.get("product_detail_url") or f"https://pt.aliexpress.com/item/{pid}.html"})
        cands = tira_suspeitos(cands)
        cands.sort(key=lambda c: (c["nota"], math.log10(max(c["vendas"], 1))), reverse=True)
        for c in cands[:4]:
            link = ali_link_produto(c, tracking)
            if link:
                c["link"] = link
                ja[c["pid"]] = hoje()
                for k in sorted(ja, key=ja.get)[:-2000]:
                    del ja[k]
                return {"id": "g:" + c["pid"], "n": titulo_curto(c["titulo"]), "garimpo": 1,
                        "nota": c["nota"], "vendas": c["vendas"], "ofertas": [c]}
        log(f"  Garimpo: nada bom o suficiente em '{kw}', tentando outra busca")
    return None


# ======================================================================
# GARIMPO DO MERCADO LIVRE — lê a página pública "Ofertas do dia" (a API de busca do ML dá 403)
# e monta o link de afiliado direto (matt_word/matt_tool), igual aos links do site.
# ======================================================================
ML_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
ML_CATS = {   # id da categoria no Mercado Livre -> (nome, emoji) — usado no site e nos posts
    "": ("Ofertas do dia", "🔥"), "MLB1574": ("Casa", "🏠"), "MLB5726": ("Eletrodomésticos", "🍳"),
    "MLB1246": ("Beleza", "💄"), "MLB1276": ("Esportes", "🏋️"), "MLB263532": ("Ferramentas", "🔧"),
    "MLB1071": ("Pet", "🐾"), "MLB1132": ("Brinquedos", "🧸"), "MLB1384": ("Bebês", "🍼"),
    "MLB5672": ("Automotivo", "🚗"), "MLB1403": ("Mercado", "🛒"), "MLB1430": ("Moda", "👟"),
    "MLB1051": ("Celular", "📱"), "MLB1648": ("Informática", "💻"), "MLB1000": ("Eletrônicos", "🎧"),
    "MLB1144": ("Games", "🎮"), "MLB264586": ("Saúde", "🩺"), "MLB1039": ("Câmeras", "📷"),
    "MLB3937": ("Relógios", "⌚"),
}


def ml_num(inteiro, centavos=None):
    return float(inteiro.replace(".", "")) + (int(centavos) / 100 if centavos else 0)


def ml_cards_ofertas(url):
    """Lê os cards (polycards) de uma página de ofertas do Mercado Livre."""
    import html as H
    req = urllib.request.Request(url, headers={"User-Agent": ML_UA, "Accept-Language": "pt-BR"})
    with urllib.request.urlopen(req, timeout=30) as r:
        if "account-verification" in r.geturl():
            raise RuntimeError("o Mercado Livre pediu verificação (bloqueou o robô nesta rodada)")
        pagina = r.read().decode("utf-8", "ignore")
    cards = []
    for bloco in re.split(r'class="(?:andes-card )?poly-card(?=[ "])', pagina)[1:]:
        m = re.search(r'<a href="([^"]+)"[^>]*class="poly-component__title"[^>]*>(.*?)</a>', bloco, re.S)
        cur = re.search(r'poly-price__current.*?aria-label="([\d.]+) reais(?: com (\d+) centavos?)?"', bloco, re.S)
        if not m or not cur:
            continue
        url_p = H.unescape(m.group(1))
        nota = re.search(r'Classificação ([\d.]+) de 5 estrelas\.(?: Mais de ([\d.,]+)\s*(mil)? produtos? vendidos?)?', bloco)
        vend = 0
        if nota and nota.group(2):
            vend = int(float(nota.group(2).replace(".", "").replace(",", ".")) * (1000 if nota.group(3) else 1))
        foto = re.search(r'class="poly-component__picture"[^>]*?(?:data-src|src)="(https://[^"]+)"', bloco)
        fid = re.search(r'(\d+-ML[A-Z]\d+_\d+)', foto.group(1)) if foto else None
        wid = re.search(r'[#&?]wid=(MLB\d+)', url_p)
        pid = re.search(r'/(?:p|up)/(MLBU?\d+)', url_p) or re.search(r'(MLB-?\d+)', url_p)
        tags = " ".join(re.findall(r'polylabel-fw-semibold">([A-ZÁÉÍÓÚÂÊÔÃÕÇ ]{4,})<', bloco))
        cards.append({
            "url": url_p, "titulo": H.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip(),
            "nota": float(nota.group(1)) if nota else 0.0, "vendas": vend, "por": ml_num(*cur.groups()),
            "img": f"https://http2.mlstatic.com/D_NQ_NP_{fid.group(1)}-O.jpg" if fid else "",
            "item": wid.group(1) if wid else "", "pid": (wid.group(1) if wid else (pid.group(1) if pid else url_p[:80])),
            "oferta_dia": "OFERTA DO DIA" in tags or "RELÂMPAGO" in tags,
        })
    return cards


def ml_link_afiliado(url_p, item, regras):
    """Link direto do produto com o teu código de afiliado (mesmo formato dos links do site)."""
    base = url_p.split("#", 1)[0]
    u = urllib.parse.urlsplit(base)
    q = dict(urllib.parse.parse_qsl(u.query))
    params = {"pdp_filters": f"item_id:{item}"} if item else ({"pdp_filters": q["pdp_filters"]} if q.get("pdp_filters") else {})
    params["matt_word"] = regras.get("ml_matt_word", "ky20260925222946505")
    params["matt_tool"] = regras.get("ml_matt_tool", "97504693")
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(params), ""))


def ml_filtra(cards, regras, ja=None, limite=""):
    """Filtro de qualidade do Mercado Livre (vale pro canal e pro site)."""
    nmin = float(regras.get("ml_garimpo_avaliacao_min", 4.7))
    vmin = int(regras.get("ml_garimpo_vendas_min", 1000))
    pmin, pmax = float(regras.get("ml_garimpo_preco_min", 15)), float(regras.get("ml_garimpo_preco_max", 400))
    proibidas = [x.lower() for x in regras.get("ml_garimpo_excluir", [])]
    bons = []
    for c in cards:
        t = c["titulo"].lower()
        if ja is not None and ja.get("ml:" + c["pid"], "") >= limite:
            continue
        if c["nota"] < nmin or c["vendas"] < vmin or not (pmin <= c["por"] <= pmax):
            continue
        if any(x in t for x in proibidas) or not palavras_ok(c["titulo"], None, None):
            continue
        bons.append(c)
    bons.sort(key=lambda c: (c["oferta_dia"], c["nota"], math.log10(max(c["vendas"], 1))), reverse=True)
    return bons


def ml_url_ofertas(cat, pagina=1):
    q = [f"category={cat}"] if cat else []
    if pagina > 1:
        q.append(f"page={pagina}")
    return "https://www.mercadolivre.com.br/ofertas" + ("?" + "&".join(q) if q else "")


# ======================================================================
# GRUPOS (ABAS) DO SITE — cada aba só mostra o que promete: a palavra do título confirma
# ======================================================================
GRUPOS = [  # (nome na aba, emoji, categorias do Mercado Livre que alimentam a aba, preço máximo no site)
    ("Celular", "📱", ["MLB1051"], 3000),
    ("Tech", "💻", ["MLB1648", "MLB1039"], 2500),
    ("Áudio e TV", "🎧", ["MLB1000"], 3000),
    ("Games", "🎮", ["MLB1144"], 1200),
    ("Moda", "👟", ["MLB1430", "MLB3937"], 600),
    ("Beleza", "💄", ["MLB1246"], 500),
    ("Esportes", "🏋️", ["MLB1276"], 800),
    ("Casa", "🏠", ["MLB1574"], 800),
    ("Eletro", "🍳", ["MLB5726"], 1800),
    ("Ferramentas", "🔧", ["MLB263532"], 800),
    ("Carro", "🚗", ["MLB5672"], 800),
    ("Pet", "🐾", ["MLB1071"], 500),
    ("Brinquedos", "🧸", ["MLB1132"], 500),
    ("Bebês", "🍼", ["MLB1384"], 800),
]
GRUPO_DA_CAT = {c: g[0] for g in GRUPOS for c in g[2]}
GRUPO_IC = {g[0]: g[1] for g in GRUPOS}
GRUPO_TETO = {g[0]: g[3] for g in GRUPOS}
GRUPO_ESTRITO = {"Games", "Áudio e TV", "Celular"}   # categoria do ML sozinha não basta: o título precisa ter a palavra da aba

# Palavras do título que decidem a aba (a primeira regra que bater vence — a ordem importa!).
# Tudo sem acento e em minúsculas; palavra inteira (coração não vira ração) e aceita plural.
GRUPO_PALAVRAS = [
    ("Brinquedos", ["pet eletronico", "bichinho virtual"]),          # "pet" de brinquedo não é produto pra pet
    ("Pet", ["racao", "cachorro", "caes", "gato", "pet", "coleira", "arranhador", "comedouro", "bebedouro", "tosa",
             "peitoral", "focinheira", "petisco", "areia sanitaria", "caminha pet", "antipulgas", "aquario", "pelos de pet"]),
    ("Bebês", ["bebe", "fralda", "mamadeira", "chupeta", "mordedor", "berco", "carrinho de bebe", "cadeirinha", "banheira",
               "baba eletronica", "enxoval", "esterilizador", "andador", "bebe conforto", "baby", "cadeira de carro infantil",
               "cadeira infantil", "assento infantil"]),
    ("Áudio e TV", ["headset", "headphone", "fone", "fones", "earbuds", "earphone", "microfone", "caixa de som",
                    "caixinha de som", "soundbar"]),                  # headset/fone pra console é áudio, não é jogo
    ("Games", ["ps5", "ps4", "ps3", "playstation", "xbox", "nintendo", "switch lite", "dualsense", "joystick", "gamepad",
               "videogame", "video game", "console", "steam deck", "controle sem fio", "controle bluetooth", "controle para ps",
               "controle para xbox", "controle para pc", "controle para celular", "controle gamer", "controle gamesir",
               "controle ps5", "controle ps4", "controle xbox", "midia fisica", "jogo ps5", "jogo ps4", "jogo xbox",
               "jogo para ps", "jogo nintendo", "volante gamer", "retro game", "game stick", "cartao psn"]),
    ("Brinquedos", ["boneca", "brinquedo", "lego", "pokemon", "pelucia", "quebra cabeca", "carrinho de brinquedo", "carrinhos de brinquedo", "pista de carrinhos", "pista de carros",
                    "carro de controle remoto",
                    "carro controle remoto", "helicoptero", "dinossauro", "massinha", "slime", "blocos de montar", "bloco de montar",
                    "hot wheels", "jogo de tabuleiro", "jogo educativo", "jogo de cartas", "baralho", "cama elastica", "nerf",
                    "lancador", "pistola de agua", "fidget", "fantasia", "miniatura", "action figure", "tapete de atividades",
                    "pula pula", "piscina de bolinhas", "patinho", "pop it"]),
    ("Carro", ["automotivo", "automotiva", "veicular", "para carro", "de carro", "do carro", "pneu", "partida bateria",
               "auxiliar de partida", "para brisa", "kit lavagem", "cera automotiva", "multimidia", "som automotivo", "dashcam",
               "dash cam", "camera veicular", "calibrador de pneu", "capa de volante", "tapete automotivo", "porta malas",
               "retrovisor", "capa de banco", "cheirinho automotivo"]),
    ("Ferramentas", ["ferramenta", "furadeira", "parafusadeira", "chave de fenda", "chaves de precisao", "alicate universal",
                     "alicate de corte", "alicate de bico", "trena", "serra", "lixadeira", "esmerilhadeira", "multimetro",
                     "ferro de solda", "soldador", "pistola de cola", "nivel a laser", "maleta de ferramentas",
                     "caixa de ferramentas", "kit de chaves", "martelo", "grampeador", "fita metrica", "broca", "chave de impacto"]),
    ("Eletro", ["air fryer", "airfryer", "fritadeira", "liquidificador", "batedeira", "cafeteira", "sanduicheira", "microondas",
                "micro ondas", "geladeira", "fogao", "cooktop", "lavadora", "maquina de lavar", "aspirador", "robo aspirador", "ventilador",
                "climatizador", "ar condicionado", "ferro de passar", "passadeira", "purificador", "umidificador",
                "processador de alimentos", "multiprocessador", "mixer", "espremedor", "chaleira eletrica", "panela eletrica",
                "grill", "grelha eletrica", "forno eletrico", "torradeira", "balanca de cozinha", "balanca digital",
                "aquecedor", "bebedouro eletrico", "triturador", "moedor de cafe", "escova eletrica de limpeza"]),
    ("Casa", ["cadeira", "guarda roupa", "escrivaninha", "estante"]),   # cadeira gamer/escritório é móvel
    ("Áudio e TV", ["fone", "fones", "headset", "headphone", "earbuds", "earphone", "tws", "caixa de som", "caixinha de som",
                    "soundbar", "smart tv", "tv", "televisao", "televisor", "projetor", "microfone", "home theater",
                    "amplificador", "tv box", "conversor digital", "antena", "som bluetooth", "alto falante", "radio", "walkie talkie", "radinho"]),
    ("Celular", ["celular", "smartphone", "iphone", "galaxy", "redmi", "poco", "moto g", "carregador", "power bank", "powerbank",
                 "cabo usb", "cabo lightning", "cabo tipo c", "cabo type c", "cabo carregador", "pelicula", "capinha",
                 "capa para celular", "suporte celular", "suporte de celular", "suporte para celular", "suporte telefone",
                 "suporte de telefone", "suporte para telefone", "tripe celular", "tripe para celular", "pop socket", "magsafe",
                 "selfie", "carregamento sem fio", "carregador sem fio", "ring light celular", "lente celular", "gimbal"]),
    ("Tech", ["notebook", "laptop", "monitor", "mouse", "teclado", "mousepad", "webcam", "ssd", "hub usb", "hub tipo c",
              "usb c hub", "roteador", "repetidor", "wifi", "impressora", "tablet", "smartwatch", "smart watch", "smartband",
              "relogio inteligente", "pulseira inteligente", "fonte atx", "placa de video", "gabinete", "kindle", "cabo hdmi",
              "adaptador", "camera", "ring light", "tripe", "drone", "cooler", "pc gamer", "computador", "hd externo",
              "suporte notebook", "suporte para notebook", "lampada inteligente", "tomada inteligente", "alexa", "echo dot"]),
    ("Esportes", ["academia", "bicicleta", "bike", "ciclismo", "halter", "yoga", "corrida", "futebol", "patinete", "skate",
                  "musculacao", "elastico de exercicio", "faixa elastica", "faixas de resistencia",
                  "corda de pular", "tapete de yoga", "luva de boxe", "boxe", "caneleira", "joelheira", "cotoveleira",
                  "esteira", "natacao", "mochila de hidratacao", "garrafa esportiva", "squeeze", "pistola de massagem",
                  "massage gun", "legging", "bola de futebol", "bola de basquete", "bola de volei", "kit halteres"]),
    ("Beleza", ["perfume", "maquiagem", "secador", "prancha", "chapinha", "batom", "skincare", "hidratante", "cabelo",
                "barbeador", "depilador", "creme", "shampoo", "condicionador", "protetor solar", "serum", "mascara facial",
                "esmalte", "unha", "aparador", "barba", "trimmer", "pente", "base liquida", "corretivo", "gloss", "rimel",
                "sombra", "pincel", "pele", "facial", "rosto", "booster", "modelador de cachos", "alisador", "escova de cabelo",
                "escova secadora", "massageador facial", "cilios"]),
    ("Moda", ["tenis", "camiseta", "camisa", "cueca", "calcinha", "sutia", "biquini", "sunga", "calca", "bermuda", "short",
              "jaqueta", "casaco", "moletom", "vestido", "saia", "blusa", "regata", "pijama", "mochila", "oculos", "relogio",
              "pulseira", "colar", "brinco", "anel", "alianca", "joia", "bolsa", "bone", "chapeu", "sandalia", "chinelo",
              "sapato", "sapatilha", "bota", "meia", "carteira", "cinto", "lenco", "gravata", "bijuteria", "necessaire"]),
    ("Casa", ["lencol", "travesseiro", "cortina", "organizador", "pote", "marmita", "varal", "luminaria", "lampada", "fita led",
              "luz led", "luz solar", "luzes", "tira de led", "luminaria led", "lampada led", "led strip", "tapete", "toalha", "edredom", "cobertor", "colcha",
              "colchao", "cabide", "vassoura", "rodo", "mop", "decoracao", "espelho", "prateleira", "gaveta", "lixeira",
              "cesto", "panela", "frigideira", "faca", "tabua", "garrafa termica", "copo", "caneca", "xicara", "prato",
              "talher", "escorredor", "utensilio", "cozinha", "papel higienico", "rolos", "sabao", "detergente", "cadeira",
              "mesa", "sofa", "ventosa", "sapateira", "jogo de cama", "jogo de panelas", "jogo de facas", "jogo de toalhas",
              "jogo de lencol", "sensor de movimento", "chuveiro", "ducha", "torneira", "fechadura"]),
]


def _txt(s):
    """minúsculas, sem acento, hífen/barra viram espaço (micro-ondas = microondas)"""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[\s\-_/]+", " ", s)


def _regex_grupo(palavras):
    ps = sorted({_txt(p) for p in palavras}, key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(p) for p in ps) + r")(?:s|es)?(?![a-z0-9])")


GRUPO_REGEX = [(g, _regex_grupo(p)) for g, p in GRUPO_PALAVRAS]

# marcas de roupa, calçado, relógio e cosmético: nas lojas com muita réplica (AliExpress/Shopee) a gente nem mostra
REPLICA_MARCAS = ["nike", "adidas", "puma", "lacoste", "gucci", "louis vuitton", "chanel", "rolex", "oakley", "ray ban", "rayban",
                  "calvin klein", "tommy", "fila", "new balance", "vans", "converse", "jordan", "yeezy", "champion", "supreme",
                  "balenciaga", "prada", "versace", "hugo boss", "armani", "michael kors", "casio", "seiko", "g shock", "gshock",
                  "under armour", "reebok", "asics", "mizuno", "kappa", "lupo", "havaianas", "olympikus", "mormaii", "technos",
                  "cetaphil", "principia", "taiff", "lizze", "apple", "airpods", "samsung", "jbl", "sony", "playstation"]


def tem_marca_replica(titulo):
    t = _txt(titulo)
    return any(re.search(r"(?<![a-z0-9])" + re.escape(_txt(m)) + r"(?![a-z0-9])", t) for m in REPLICA_MARCAS)


def grupo_pelo_titulo(titulo):
    t = _txt(titulo)
    for g, rx in GRUPO_REGEX:
        if rx.search(t):
            return g
    return None


# dica de grupo pela busca (em inglês) antiga do AliExpress — só pra achados velhos que ainda estão guardados
DICA_BUSCA = [
    ("Pet", ["pet", "cat", "dog"]), ("Carro", ["car "]),
    ("Beleza", ["makeup", "hair", "shaver", "nail"]),
    ("Esportes", ["resistance", "jump rope", "bike", "yoga"]),
    ("Moda", ["bottle", "bag", "umbrella", "sunglasses", "wallet", "watch strap"]),
    ("Casa", ["kitchen", "organizer", "chopper", "lint", "fan", "frother", "screwdriver", "drill", "tape measure", "led strip bedroom", "motion sensor"]),
    ("Games", ["gaming", "gamepad", "controller"]),
]


def grupo_do_produto(titulo, cat=None, nome_antigo=None, busca=None, hint=None):
    """Aba do produto, ou None quando ele NÃO deve aparecer (não combina com o que a aba promete).
    - hint: aba da busca que achou o produto (AliExpress/Shopee) — o título tem que concordar
    - cat: categoria do Mercado Livre de onde veio — vale quando o título não decide"""
    g = grupo_pelo_titulo(titulo)
    if hint:
        return hint if g in (None, hint) else None
    if g:
        return g
    if cat in GRUPO_DA_CAT:
        g2 = GRUPO_DA_CAT[cat]
        return None if g2 in GRUPO_ESTRITO else g2
    if busca:
        b = " " + busca.lower() + " "
        for gg, palavras in DICA_BUSCA:
            if any(p in b for p in palavras):
                return gg
        return "Tech"
    if nome_antigo in GRUPO_IC:
        return nome_antigo
    return None


# Buscas por aba (AliExpress em inglês, Shopee em português). O robô gira por aqui: cada rodada pega
# algumas abas e, dentro de cada uma, a próxima busca da lista. Pode editar/aumentar em produtos.json
# (regras -> "grupos_buscas") sem mexer no código.
GRUPOS_BUSCAS = {
    "Celular": {
        "ali": ["ugreen gan charger 65w", "baseus power bank 20000mah", "usb c cable 100w braided", "magsafe wireless charger",
                "phone tripod bluetooth remote", "phone cooling fan", "lightning cable fast charging", "wireless charging stand",
                "phone stand desk adjustable", "baseus magnetic power bank"],
        "shopee": ["carregador turbo 20w", "power bank 20000mah", "cabo usb c 100w", "carregador sem fio magnético",
                   "tripé celular com controle", "suporte celular mesa", "cabo lightning iphone", "carregador tomada 65w",
                   "suporte celular bicicleta", "carregador portátil magsafe"]},
    "Tech": {
        "ali": ["wireless mouse bluetooth", "mechanical keyboard hot swappable", "usb c hub hdmi", "webcam 1080p", "mouse pad large desk",
                "laptop stand aluminum", "smart watch amoled", "smart band fitness", "usb microphone condenser", "laptop cooling pad",
                "wifi 6 router", "bluetooth keyboard tablet"],
        "shopee": ["mouse sem fio", "teclado mecânico", "hub usb c", "webcam full hd", "mousepad grande", "suporte notebook",
                   "smartwatch", "relógio inteligente", "microfone usb", "cooler notebook", "roteador wifi", "teclado bluetooth"]},
    "Áudio e TV": {
        "ali": ["bluetooth earbuds tws", "qcy earbuds", "anc headphones wireless", "bluetooth speaker portable", "soundbar tv",
                "gaming headset", "neckband earphones", "tv box android 4k", "projector 4k portable", "wired earphones with mic",
                "lavalier wireless microphone"],
        "shopee": ["fone bluetooth", "fone tws", "caixa de som bluetooth", "soundbar", "headset gamer", "tv box",
                   "projetor portátil", "microfone lapela", "fone de ouvido com fio", "caixa de som portátil", "suporte tv parede"]},
    "Games": {
        "ali": ["gamepad bluetooth controller", "ps5 controller", "nintendo switch case", "switch joycon grip", "game controller phone",
                "retro game console handheld", "ps5 headset stand", "xbox controller thumb grips", "game stick 4k"],
        "shopee": ["controle ps5", "controle ps4", "controle xbox", "nintendo switch", "jogo ps5", "jogo ps4", "console retro game",
                   "controle gamepad celular", "cabo hdmi ps5", "volante gamer"]},
    "Moda": {
        "ali": ["men quartz watch", "polarized sunglasses", "women handbag", "waterproof backpack", "men casual sneakers",
                "leather wallet men", "baseball cap", "stainless steel bracelet", "oversized t shirt", "hoodie men", "cotton socks",
                "leather belt men"],
        "shopee": ["camiseta masculina algodão", "tênis masculino casual", "relógio masculino", "óculos de sol polarizado",
                   "mochila impermeável", "bolsa feminina", "bermuda masculina", "kit meias", "boné", "carteira masculina couro",
                   "jaqueta corta vento", "moletom canguru", "cinto masculino"]},
    "Beleza": {
        "ali": ["hair clipper trimmer", "hair straightener", "ionic hair dryer", "electric shaver men", "makeup brush set",
                "nail art kit", "facial steamer", "eyelash curler", "hair curler automatic", "beard trimmer", "makeup organizer"],
        "shopee": ["secador de cabelo", "chapinha", "aparador de pelos", "barbeador elétrico", "kit pincéis maquiagem",
                   "organizador maquiagem", "modelador cachos", "escova alisadora", "kit skincare", "massageador facial"]},
    "Esportes": {
        "ali": ["resistance bands set", "jump rope", "yoga mat", "adjustable dumbbell", "bike phone holder", "cycling gloves",
                "sports water bottle", "running armband", "gym gloves", "knee brace", "bicycle light", "massage gun"],
        "shopee": ["elástico exercício kit", "corda de pular", "tapete yoga", "halter ajustável", "luva academia",
                   "garrafa esportiva", "suporte celular bike", "faixa elástica", "pistola de massagem", "caneleira",
                   "bola de futebol", "joelheira"]},
    "Casa": {
        "ali": ["led strip lights", "storage organizer box", "vacuum storage bags", "night light motion sensor", "shower head high pressure",
                "wall shelf adhesive", "cable organizer", "smart led bulb", "door stopper", "drawer organizer", "solar light outdoor"],
        "shopee": ["organizador gaveta", "fita led", "luminária led", "varal de luzes", "jogo de lençol", "kit organizador",
                   "cabides", "lixeira pia", "prateleira adesiva", "tapete antiderrapante", "ducha pressurizada", "cortina blackout"]},
    "Eletro": {
        "ali": ["electric kettle", "portable blender", "milk frother", "electric food chopper", "coffee grinder electric",
                "electric lunch box", "garment steamer", "robot vacuum", "humidifier", "digital kitchen scale", "usb neck fan"],
        "shopee": ["air fryer", "liquidificador portátil", "cafeteira", "sanduicheira", "ventilador", "umidificador",
                   "balança cozinha digital", "chaleira elétrica", "mixer", "processador alimentos", "aspirador portátil", "panela elétrica"]},
    "Ferramentas": {
        "ali": ["cordless electric screwdriver", "tool kit set", "laser measure", "digital multimeter", "soldering iron kit",
                "precision screwdriver set", "glue gun", "cordless drill", "wire stripper"],
        "shopee": ["parafusadeira sem fio", "kit ferramentas", "trena laser", "multímetro digital", "kit chaves de precisão",
                   "furadeira", "nível a laser", "pistola de cola quente", "caixa de ferramentas"]},
    "Carro": {
        "ali": ["car phone holder magnetic", "car vacuum cleaner", "car trunk organizer", "dash cam", "car jump starter",
                "tire inflator portable", "car seat cover", "car led interior light", "tire pressure gauge", "windshield sun shade",
                "obd2 scanner"],
        "shopee": ["aspirador automotivo", "suporte veicular", "calibrador pneu portátil", "câmera veicular", "organizador porta malas",
                   "capa volante", "kit limpeza automotiva", "carregador veicular", "auxiliar partida", "tapete automotivo"]},
    "Pet": {
        "ali": ["dog harness no pull", "cat toys interactive", "pet grooming glove", "automatic pet feeder", "retractable dog leash",
                "cat litter mat", "pet water fountain", "dog chew toys", "pet hair remover", "cat scratching board"],
        "shopee": ["peitoral cachorro", "brinquedo gato", "luva escova pet", "fonte bebedouro gato", "arranhador gato", "comedouro pet",
                   "coleira cachorro", "cama pet", "tapete higiênico", "brinquedo cachorro"]},
    "Brinquedos": {
        "ali": ["building blocks set", "rc car remote control", "toy car track", "kids puzzle", "slime kit", "kids drawing tablet",
                "magnetic tiles", "water gun", "toy dinosaur"],
        "shopee": ["blocos de montar", "carrinho controle remoto", "quebra-cabeça", "massinha de modelar", "pista carrinhos",
                   "boneca", "pelúcia", "jogo de tabuleiro", "pistola de água", "nerf"]},
    "Bebês": {
        "ali": ["baby monitor", "baby bottle", "baby toys", "baby carrier", "stroller organizer", "baby bath", "teething toy"],
        "shopee": ["mamadeira", "babá eletrônica", "mordedor", "banheira bebê", "tapete atividades bebê", "chupeta", "kit higiene bebê",
                   "organizador carrinho bebê"]},
}


def proxima_busca(meta, loja, regras):
    """Rodízio das buscas: devolve (aba, termo, volta). Uma aba por vez e, dentro dela, o próximo termo da lista."""
    buscas = regras.get("grupos_buscas") or GRUPOS_BUSCAS
    nomes = [g[0] for g in GRUPOS if buscas.get(g[0], {}).get(loja)]
    if not nomes:
        return None, None, 0
    n = meta.get(loja + "_n", 0)
    meta[loja + "_n"] = n + 1
    g = nomes[n % len(nomes)]
    lista = buscas[g][loja]
    q = meta.setdefault(loja + "_q", {})
    i = q.get(g, 0)
    q[g] = i + 1
    return g, lista[i % len(lista)], i // len(lista)       # (aba, termo, nº da volta nessa lista)


# ======================================================================
# COMPARADOR — pra cada achado, procura o MESMO produto nas outras lojas
# (só compara quando dá pra ter certeza que é o mesmo: marca/modelo/capacidade batendo)
# ======================================================================
MARCAS = set("""ugreen baseus anker soundcore xiaomi redmi poco samsung motorola apple lenovo thinkplus haylou qcy edifier
tronsmart jbl kz aula redragon attack shark logitech hyperx havit fantech easysmx gamesir 8bitdo multilaser intelbras positivo
inova basike jskj pioneer bosch vonder tramontina electrolux oster arno britania philco mondial wap taiff lizze gama cadence
puma kappa lupo havaianas casio technos mormaii olympikus nike adidas fila oakley amazfit huawei realme honor tp-link mercusys
roku philips lg sony dapon kaidi hoco pmcell geonav i2go elg exbom knup lehmox tomate fontastic newpet relaxmedic nathor
asus acer dell hp epson canon brother karcher dewalt makita worx stanley hikvision sandisk kingston seagate adata lexar
corsair razer steelseries elgin lenoxx tcl hisense semp aoc gigabyte msi nokia infinix tecno nintendo microsoft hasbro
mattel lego estrela xalingo chicco avent tommee lillo kuka nuk mam premier pedigree whiskas furminator kemei wahl babyliss
gaomon vinik tomshoo mijia dreame roborock eufy tuya sonoff shelly yeelight ecovacs""".split())
UNID_SPEC = ("mah", "w", "gb", "tb", "ml", "kg", "l", "lbf", "psi", "hz")
# marcas de roupa, calçado, relógio e cosmético: no AliExpress quase sempre é réplica -> não compara lá
SO_LOJA_OFICIAL = set("""puma kappa lupo havaianas casio technos mormaii olympikus nike adidas fila oakley principia taiff lizze
gama cetaphil apple samsung jbl""".split())
STOP = set("de da do das dos para com e sem a o as os em no na kit un und unid original novo nova".split())


@functools.lru_cache(maxsize=20000)
def _norm(t):
    t = (t or "").lower()
    t = re.sub(r"(?<=\d)[.,](?=\d{3}(?!\d))", "", t)               # 20.000 -> 20000
    t = re.sub(r"(?<=\d)[.,](?=\d)", "p", t)                      # 22.5w -> 22p5w (decimal não vira dois números)
    t = re.sub(r"(\d)\s*(?:litros?|lts?)\b", r"\1l", t)
    t = re.sub(r"(\d)\s+(mah|w|gb|tb|ml|kg|l|lbf|psi|hz)\b", r"\1\2", t)            # 20000 mah -> 20000mah
    t = re.sub(r"(?<=[a-z0-9])-(?=[a-z0-9])", "", t)             # w-218h -> w218h
    return re.findall(r"[a-z0-9À-ú\-]+", t)


def identidade(titulo):
    """Marca, modelo e capacidade do produto. Sem marca nem modelo = não dá pra comparar com segurança."""
    tk = _norm(titulo)
    marca = [w for i, w in enumerate(tk) if w in MARCAS              # "para iPhone"/"compatível Xiaomi" não é a marca
             and not any(x in ("para", "compatível", "compativel", "p", "pra", "compatible") for x in tk[max(0, i - 3):i])]
    spec = [w for w in tk if re.fullmatch(r"\d+(?:p\d+)?(%s)" % "|".join(UNID_SPEC), w)]
    modelo = [w for w in tk if re.search(r"\d", w) and re.search(r"[a-z]", w) and w not in spec
              and not re.fullmatch(r"\d+(mm|cm|m|km|ml|l|kg|g|v|a|h|hz|pol|pcs|x|p|k|ghz|mp|pçs|peças|ch|cores|pares|un)", w) and len(w) >= 2]
    nomes = [w for w in tk if w not in STOP and not re.search(r"\d", w) and w not in MARCAS and len(w) > 2]
    return {"marca": marca[:1], "modelo": modelo[:2], "spec": spec[:4], "nomes": nomes[:2], "nomes3": nomes[:3],
            "qtd": qtd_pecas(titulo), "tokens": set(tk)}


ACESSORIO = set("""capa capinha case pelicula película protetor suporte cabo adaptador refil refis filtro carcaça carcaca bateria
tampa almofadas almofada borracha pecas peças reposição reposicao substituição substituicao fonte pulseira correia
tapete bolsa estojo skin adesivo""".split())


def qtd_pecas(titulo):
    """Quantas peças vêm no anúncio (kit 6, 10 pares, 3 un...). Sem aviso = 1 peça."""
    t = (titulo or "").lower()
    m = re.search(r"\bkit\s*(?:com\s*)?(\d{1,3})\b", t) or re.search(
        r"\b(\d{1,3})\s*(?:pares?|pcs|pç|pçs|peças|pecas|unidades|un|und|rolos|potes|pack)\b", t) or re.search(
        r"\b(?:c|com)/?\s*(\d{1,3})\s*(?:un|und|unidades|pares|peças)", t)
    return int(m.group(1)) if m else 1


def _sing(w):
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def mesmo_produto(ident, titulo):
    """Só vale quando dá pra ter certeza: marca + modelo + capacidade batem E a quantidade (kit) é a mesma."""
    if not (ident["marca"] or ident["modelo"]):
        return False
    tk = set(_norm(titulo))
    if not all(w in tk for w in ident["marca"] + ident["modelo"] + ident["spec"]):
        return False
    esp = "|".join(UNID_SPEC)
    if {w for w in tk if re.fullmatch(r"\d+(?:p\d+)?(%s)" % esp, w)} != set(ident["spec"]):
        return False                                  # capacidade/potência diferente (ex.: 22,5W x 65W) = outro produto
    if ident.get("qtd", 1) != qtd_pecas(titulo):
        return False
    orig = ident.get("tokens", set())
    if (tk & ACESSORIO) - orig:                       # "capa/cabo/suporte pro X" não é o X
        return False
    if re.search(r"\b(?:para|pra|p/|compat[ií]vel(?: com)?)\s+(?:[\w\-]+\s+){0,2}(?:%s)\b" % "|".join(map(re.escape, ident["marca"] or ["~~"])), (titulo or "").lower()) \
            and not re.search(r"\b(?:para|pra|p/|compat[ií]vel)\b", " ".join(orig)):
        return False
    if not ident["modelo"] and not ident["spec"]:        # só a marca não basta: o nome do produto também tem que bater
        sg = {_sing(w) for w in tk}
        if not all(_sing(w) in sg for w in ident.get("nomes3", [])):
            return False
    return True


def _preco_ok(por, ref, regras, loja, titulo):
    if por < ref * float(regras.get("comparar_min_fator", 0.55)):
        reprova("comparador: preço bom demais pra ser verdade (golpe?)")
        log(f"      descartado {loja} R$ {por:.2f} (ref R$ {ref:.2f}) · {titulo[:50]}")
        return False
    return por <= ref * float(regras.get("comparar_max_fator", 1.8))


ML_BLOQUEADO = {"on": False}
TIPO_STOP = set("""original novo nova premium profissional super mega hot sale global model oficial lançamento lancamento promoção
promocao brinde frete grátis gratis pronta entrega qualidade universal portátil portatil kit conjunto jogo par pares unidade""".split())


def tipo_do_produto(titulo):
    """As 3 primeiras palavras que dizem O QUE é o produto (sem marca, sem número): 'lavadora alta pressão'."""
    tk = _norm(titulo)
    return [w for w in dict.fromkeys(tk) if w not in STOP and w not in TIPO_STOP and w not in MARCAS
            and not re.search(r"\d", w) and len(w) > 2][:3]


def _spec_val(tok):
    m = re.fullmatch(r"(\d+(?:p\d+)?)([a-z]+)", tok)
    return (float(m.group(1).replace("p", ".")), m.group(2)) if m else (0.0, "")


def specs_proximas(specs, tk):
    """Capacidade/potência parecida (até 25% de diferença). Se o anúncio não diz, não reprova."""
    for sp in specs:
        val, un = _spec_val(sp)
        outros = [_spec_val(w)[0] for w in tk if _spec_val(w)[1] == un and re.fullmatch(r"\d+(?:p\d+)?[a-z]+", w)]
        if val and outros and not any(abs(o - val) / val <= 0.25 for o in outros):
            return False
    return True


# subtipos que se excluem: headphone não é fone intra-auricular, produto com GPS não é sem GPS, de cachorro não é de gato...
SUBTIPOS = [set(x.split()) for x in (
    "headphone headset overear concha", "tws earbud earbuds intra intraauricular gancho earhook", "gps", "gamer gaming",
    "infantil criança crianças kids bebê", "cachorro cachorros cão cães", "gato gatos", "masculino masculina", "feminino feminina",
    "mecânico mecanico", "pendurado pescoço",
    # plataforma/compatibilidade: controle de PS5 não é controle de Xbox, cabo Lightning não é USB-C
    "ps5 ps4 ps3 playstation dualsense dualshock psvr", "xbox", "switch nintendo joycon", "iphone ios lightning magsafe",
    "android", "macbook", "usbc typec", "microusb", "steam deck")]
# marcas fortes: só vale o MESMO produto (um "parecido" de outra marca confunde o cliente)
SO_IGUAL = set("""sony nintendo microsoft apple samsung lg philips xiaomi motorola dell hp asus acer lenovo canon epson jbl bosch
dewalt makita karcher oster arno electrolux playstation xbox""".split())


def parecido(ident, tipo, titulo, cat):
    """Produto PARECIDO (mesmo tipo, mesma aba, capacidade próxima, mesma quantidade) — não é o mesmo modelo."""
    if len(tipo) < 2:
        return False
    tk = set(_norm(titulo))
    sg = {_sing(w) for w in tk}
    if _sing(tipo[0]) not in sg or sum(1 for w in tipo if _sing(w) in sg) < 2:
        return False                                  # o 1º nome (o que o produto É) e mais um têm que bater
    orig = ident.get("tokens", set())
    if any(bool(g & orig) != bool(g & tk) for g in SUBTIPOS):
        return False
    if ident.get("qtd", 1) != qtd_pecas(titulo) or (tk & ACESSORIO) - ident.get("tokens", set()):
        return False
    if not specs_proximas(ident["spec"], tk):
        return False
    g = grupo_pelo_titulo(titulo)
    return not (cat and g and g != cat)


def buscar_loja(loja, termo, regras):
    """Lê a busca de uma loja e devolve candidatos já no mesmo formato. mk() gera o link de afiliado só do escolhido."""
    out = []
    if loja == "Mercado Livre":
        if ML_BLOQUEADO["on"]:
            return out
        slug = "-".join(re.sub(r"[^a-z0-9]+", " ", _txt(termo)).split())
        if not slug:
            return out
        time.sleep(1.2)
        try:
            cards = ml_cards_ofertas(f"https://lista.mercadolivre.com.br/{slug}")
        except Exception as e:
            if "verificação" in str(e):
                ML_BLOQUEADO["on"] = True               # o ML bloqueou: para de procurar nele nesta rodada
            log("      comparador ML:", str(e)[:120])
            return out
        for c in cards[:40]:
            host = urllib.parse.urlsplit(c["url"]).netloc
            if "mercadolivre.com.br" not in host or host.startswith("click"):
                continue                                 # anúncio patrocinado (link de clique): não dá pra gerar o link certo
            out.append({"por": c["por"], "nota": c["nota"], "vendas": c["vendas"], "titulo": c["titulo"],
                        "mk": (lambda c=c: ml_link_afiliado(c["url"], c["item"], regras))})
    elif loja == "Shopee":
        if not shopee_ok():
            return out
        for n in shopee_busca(termo, limite=30):
            try:
                por = float(n.get("priceMin") or n.get("price") or 0)
                out.append({"por": por, "nota": float(n.get("ratingStar") or 0), "vendas": int(n.get("sales") or 0),
                            "titulo": n.get("productName") or "",
                            "mk": (lambda n=n: n.get("offerLink") or shopee_link_curto(n.get("productLink")))})
            except (TypeError, ValueError):
                continue
    elif loja == "AliExpress":
        if not (os.environ.get("ALI_APP_KEY") and os.environ.get("ALI_SECRET")):
            return out
        tracking = os.environ.get("ALI_TRACKING_ID", "kaivolt")
        resp = ali_chamar("aliexpress.affiliate.product.query", {
            "target_currency": "BRL", "target_language": "PT", "ship_to_country": "BR", "tracking_id": tracking,
            "keywords": termo, "sort": "LAST_VOLUME_DESC", "page_size": 30, "page_no": 1})
        for it in ali_produtos(resp):
            pid = str(it.get("product_id") or "")
            aval = ali_num(it.get("evaluate_rate"))
            cand = {"loja": "AliExpress", "pid": pid,
                    "url_prod": it.get("product_detail_url") or f"https://pt.aliexpress.com/item/{pid}.html"}
            out.append({"por": ali_num(it.get("target_sale_price") or it.get("sale_price")), "nota": round(aval / 20, 1),
                        "vendas": int(ali_num(it.get("lastest_volume"))), "titulo": it.get("product_title") or "",
                        "aval": aval, "mk": (lambda cand=cand: ali_link_produto(cand, tracking))})
    return out


def _qualidade_ok(loja, c, regras, modo):
    """Nota e vendas mínimas (no 'parecido' o filtro é mais rígido, porque não é o mesmo modelo)."""
    rig = modo == "parecido"
    if loja == "AliExpress":
        return (c.get("aval", 0) >= float(regras.get("avaliacao_minima_aliexpress", 94 if rig else 92))
                and c["vendas"] >= int(regras.get("vendas_minimas_aliexpress", 200 if rig else 100)))
    if loja == "Shopee":
        return c["nota"] >= float(regras.get("nota_minima", 4.6 if rig else 4.5)) and c["vendas"] >= (200 if rig else 100)
    return c["nota"] >= float(regras.get("nota_minima", 4.6 if rig else 4.5)) and c["vendas"] >= (100 if rig else 50)


def comparar_loja(loja, v, ident, grupo, regras, so_igual=False):
    """Procura numa loja: 1º o MESMO produto; se não houver, um PARECIDO (mesmo tipo, preço e capacidade próximos)."""
    ref, titulo = v["por"], v.get("t") or v.get("n")
    tipo = tipo_do_produto(titulo)
    fmin, fmax = (0.75, 1.35) if so_igual else (0.6, 1.5)
    tentativas = []
    if ident["marca"] or ident["modelo"]:
        tentativas.append(("igual", " ".join(ident["marca"] + ident["modelo"] + ident["spec"] + ident["nomes"][:1]),
                           lambda t: mesmo_produto(ident, t)))
    if not so_igual and len(tipo) >= 2:
        tentativas.append(("parecido", " ".join(tipo + ident["spec"][:1]), lambda t: parecido(ident, tipo, t, grupo)))
    replica = grupo in ("Moda", "Beleza", "Esportes") and loja != "Mercado Livre"
    for modo, termo, ok in tentativas:
        cands = []
        for c in buscar_loja(loja, termo, regras):
            if c["por"] <= 0 or not ok(c["titulo"]) or not _qualidade_ok(loja, c, regras, modo):
                continue
            if not (ref * fmin <= c["por"] <= ref * fmax) or not palavras_ok(c["titulo"], None, regras.get("garimpo_excluir")):
                continue
            if replica and tem_marca_replica(c["titulo"]):
                continue
            cands.append(c)
        for c in custo_beneficio(cands)[:3]:
            link = c["mk"]()
            if link:
                return {"loja": loja, "por": round(c["por"], 2), "nota": c["nota"], "vendas": c["vendas"],
                        "t": c["titulo"][:90], "link": link, "sim": modo == "parecido"}
    return None


def comparar_achados(regras, guard, agora):
    """Compara alguns achados por rodada (os que ainda não foram comparados, ou comparados há +24h): em cada
    outra loja procura o MESMO produto e, se não houver, um PARECIDO. Assim cada card mostra as 3 lojas."""
    if not regras.get("comparar_ativo", True):
        return
    limite = int(regras.get("comparar_por_rodada", 20))
    validade = float(regras.get("comparar_validade_horas", 24)) * 3600
    fila = [v for v in guard.values()
            if not v.get("cmp_em") or v.get("cmp_v") != CMP_VERSAO
            or (agora - datetime.fromisoformat(v["cmp_em"])).total_seconds() > validade]
    fila.sort(key=lambda v: (not v.get("cmp_em"), v.get("od", 0), v.get("nota", 0)), reverse=True)   # os nunca comparados primeiro
    feitos = 0
    for v in fila:
        if feitos >= limite:
            break
        titulo = v.get("t") or v.get("n")
        ident = identidade(titulo)
        v["cmp_em"], v["cmp_v"] = agora.isoformat(timespec="minutes"), CMP_VERSAO
        v["cmp"] = []
        if not (ident["marca"] or ident["modelo"]) and len(tipo_do_produto(titulo)) < 2:
            continue                                   # nem marca nem tipo claro: não dá pra comparar
        feitos += 1
        grupo = grupo_do_produto(titulo, v.get("cid"), None, v.get("busca"), v.get("g")) or ""
        oficial = bool(set(ident["marca"]) & SO_LOJA_OFICIAL)   # marca de loja oficial: só o MESMO produto
        forte = oficial or bool((set(ident["marca"]) | set(tipo_do_produto(titulo))) & SO_IGUAL) or bool(set(_norm(titulo)) & SO_IGUAL)
        for loja in ("Mercado Livre", "Shopee", "AliExpress"):
            if loja == v["loja"] or (loja == "AliExpress" and (oficial or grupo in ("Moda", "Beleza"))):
                continue                               # réplica é comum nessas marcas/abas no AliExpress
            try:
                o = comparar_loja(loja, v, ident, grupo, regras, so_igual=forte)
                if o:
                    v["cmp"].append(o)
            except Exception as e:
                log(f"    comparador {loja}: erro ->", str(e)[:150])
        if v["cmp"]:
            log(f"  Comparado: {v['n'][:45]} -> " + ", ".join(
                f"{o['loja']} R$ {o['por']:.2f}{' (parecido)' if o.get('sim') else ''}" for o in v["cmp"]))


CMP_VERSAO = 6          # sobe quando a regra do comparador muda: as comparações antigas são refeitas


def ofertas_do_achado(v, guard, regras):
    """Lista final de lojas do achado: a própria + o MESMO produto nas outras lojas + (se não houver igual) um
    PARECIDO ('sim': True). Idênticos vêm primeiro; até 3 lojas."""
    titulo = v.get("t") or v.get("n")
    ofs = [{"loja": v["loja"], "por": v["por"], "nota": v["nota"], "vendas": v["vendas"], "t": v.get("t", "")[:90], "link": v["link"]}]
    ident = identidade(titulo)
    oficial = bool(set(ident["marca"]) & SO_LOJA_OFICIAL)
    forte = oficial or bool(set(_norm(titulo)) & SO_IGUAL)          # marca forte: só o MESMO produto, nunca um parecido
    sem_ali = oficial or v.get("cat") in ("Moda", "Beleza")
    if v.get("cmp_v") == CMP_VERSAO:
        ofs += [o for o in v.get("cmp", []) if o["loja"] != v["loja"] and not (sem_ali and o["loja"] == "AliExpress")
                and not (forte and o.get("sim"))]
    lojas = {o["loja"] for o in ofs}
    # o mesmo produto que a vitrine já tem em outra loja
    if ident["marca"] or ident["modelo"]:
        for w in guard.values():
            if w is v or w["loja"] in lojas or (sem_ali and w["loja"] == "AliExpress"):
                continue
            f1, f2 = (0.75, 1.35) if oficial or v.get("cat") in ("Moda", "Beleza") else (0.6, 1.5)
            if mesmo_produto(ident, w.get("t") or w.get("n")) and v["por"] * f1 <= w["por"] <= v["por"] * f2:
                ofs.append({"loja": w["loja"], "por": w["por"], "nota": w["nota"], "vendas": w["vendas"],
                            "t": w.get("t", "")[:90], "link": w["link"]})
                lojas.add(w["loja"])
    # lojas que ainda faltam: um produto PARECIDO que a vitrine já tem (mesma aba)
    if not forte:
        tipo = tipo_do_produto(titulo)
        for loja in ("Mercado Livre", "Shopee", "AliExpress"):
            if loja in lojas or (loja == "AliExpress" and sem_ali):
                continue
            cands = [w for w in guard.values()
                     if w["loja"] == loja and w.get("cat") == v.get("cat") and v["por"] * 0.6 <= w["por"] <= v["por"] * 1.5
                     and parecido(ident, tipo, w.get("t") or w.get("n"), v.get("cat"))
                     and (w.get("nota") or 0) >= 4.6]
            if cands:
                w = custo_beneficio(cands)[0]
                ofs.append({"loja": loja, "por": w["por"], "nota": w["nota"], "vendas": w["vendas"],
                            "t": w.get("t", "")[:90], "link": w["link"], "sim": True})
    return sorted(ofs, key=lambda o: (bool(o.get("sim")), o["por"]))


def _mantem_comparacao(novo, antes):
    """Quando o mesmo produto aparece de novo, mantém a comparação entre lojas que já foi feita (vale 24h)."""
    for k in ("cmp", "cmp_em", "cmp_v"):
        if k in antes:
            novo[k] = antes[k]


def ml_filtra_site(cards, regras, cat):
    """Filtro de qualidade dos achados do site (Mercado Livre): nota, vendas, palavras proibidas, aba certa
    e preço máximo da aba (celular/TV aceitam produto mais caro; pet/beleza não)."""
    nmin = float(regras.get("achados_nota_min", regras.get("ml_garimpo_avaliacao_min", 4.7)))
    vmin = int(regras.get("achados_ml_vendas_min", 500))
    pmin = float(regras.get("ml_garimpo_preco_min", 15))
    proibidas = [x.lower() for x in regras.get("ml_garimpo_excluir", [])]
    bons = []
    for c in cards:
        t = c["titulo"].lower()
        if c["nota"] < nmin or c["vendas"] < vmin:
            continue
        if any(x in t for x in proibidas) or not palavras_ok(c["titulo"], None, None):
            continue
        g = grupo_do_produto(c["titulo"], cat)
        if not g:
            reprova("fora da aba (não segue o que a aba promete)")
            continue
        if not (pmin <= c["por"] <= GRUPO_TETO.get(g, 600)):
            continue
        c["grupo"] = g
        bons.append(c)
    bons.sort(key=lambda c: (c["oferta_dia"], c["nota"], math.log10(max(c["vendas"], 1))), reverse=True)
    return bons


def achados_site(regras, historico):
    """Seção 'Achados do dia' do site: a cada rodada lê algumas categorias das Ofertas do dia do Mercado Livre
    (páginas 1 e 2, em rodízio) e faz buscas por aba na Shopee e no AliExpress. Cada produto só entra na aba
    que combina com o título; fica guardado por até 'achados_horas'."""
    if not regras.get("achados_ativo", True):
        return []
    cats = regras.get("achados_categorias") or [c for g in GRUPOS for c in g[2]]
    meta = historico.setdefault("_achados_meta", {"n": 0})
    guard = historico.setdefault("_achados", {})
    agora = datetime.now(timezone.utc)
    paginas = max(1, int(regras.get("achados_paginas", 2)))
    for _ in range(int(regras.get("achados_categorias_por_rodada", 5))):
        n = meta["n"]
        cat, pagina = cats[n % len(cats)], (n // len(cats)) % paginas + 1
        meta["n"] = n + 1
        nome = ML_CATS.get(cat, (cat,))[0]
        try:
            cards = ml_cards_ofertas(ml_url_ofertas(cat, pagina))
        except Exception as e:
            log(f"Achados: não consegui ler {nome} (pág. {pagina}) ->", e)
            if "verificação" in str(e):
                break                                   # o ML bloqueou: não insiste nesta rodada
            continue
        bons = ml_filtra_site(cards, regras, cat)[:int(regras.get("achados_por_categoria", 14))]
        log(f"Achados: {nome} (pág. {pagina}) -> {len(cards)} lidas, {len(bons)} aprovadas")
        for c in bons:
            antes = guard.get(c["pid"], {})
            guard[c["pid"]] = {"n": titulo_curto(c["titulo"], 70), "t": c["titulo"][:140],
                               "cid": antes.get("cid") or cat,          # fica com a 1ª categoria em que apareceu
                               "loja": "Mercado Livre", "por": round(c["por"], 2), "nota": c["nota"],
                               "vendas": c["vendas"], "img": c["img"], "od": 1 if c["oferta_dia"] else 0,
                               "link": ml_link_afiliado(c["url"], c["item"], regras),
                               "visto": agora.isoformat(timespec="minutes")}
            _mantem_comparacao(guard[c["pid"]], antes)
    for nome_fn, fn in (("AliExpress", achados_ali), ("Shopee", achados_shopee)):
        try:
            fn(regras, meta, guard, agora)
        except Exception as e:                       # uma loja nunca derruba os achados das outras
            log(f"Achados {nome_fn}: ERRO ->", e)
    horas = float(regras.get("achados_horas", 36))
    for k in [k for k, v in guard.items()
              if (agora - datetime.fromisoformat(v["visto"])).total_seconds() > horas * 3600]:
        del guard[k]
    proib = [x.lower() for x in regras.get("ml_garimpo_excluir", [])]
    for k in [k for k, v in guard.items() if any(x in (v.get("t") or v.get("n") or "").lower() for x in proib)]:
        del guard[k]
    try:
        comparar_achados(regras, guard, agora)
    except Exception as e:                       # comparador nunca derruba os achados
        log("Comparador: ERRO ->", e)
    # aba final de cada produto (título + categoria/busca de origem); o que não combina com nenhuma aba sai
    for k in list(guard):
        v = guard[k]
        g = grupo_do_produto(v.get("t") or v.get("n"), v.get("cid"), None, v.get("busca"), v.get("g"))
        if not g:
            del guard[k]
            continue
        v["cat"], v["ic"] = g, GRUPO_IC[g]
    # o que fica guardado: no máximo 'achados_guarda_por_loja' por aba E por loja (os vistos mais recentemente),
    # pra uma loja com muito produto não empurrar as outras pra fora da aba
    teto = int(regras.get("achados_guarda_por_loja", 10))
    por_grupo = {}
    for k, v in guard.items():
        por_grupo.setdefault((v["cat"], v["loja"]), []).append(k)
    for ks in por_grupo.values():
        for k in sorted(ks, key=lambda k: (guard[k]["visto"], guard[k].get("nota", 0)), reverse=True)[teto:]:
            del guard[k]
    # vitrine: cada aba alterna as lojas (ML, Shopee, Ali, ML...) e as abas se misturam em "Todos"
    por_cat = {g[0]: [] for g in GRUPOS}
    nlojas = {id(v): len(ofertas_do_achado(v, guard, regras)) for v in guard.values()}   # em quantas lojas o MESMO produto foi achado
    for v in sorted(guard.values(), key=lambda v: (nlojas[id(v)], v["od"], v["nota"], math.log10(max(v["vendas"], 1))), reverse=True):
        por_cat.setdefault(v["cat"], []).append(v)
    ordem_lojas = ["Mercado Livre", "Shopee", "AliExpress"]
    por_aba = int(regras.get("achados_por_grupo_site", 12))
    for g, lista in por_cat.items():
        lojas = {}
        for v in lista:
            lojas.setdefault(v["loja"], []).append(v)
        mix = []
        while any(lojas.values()):
            for l in sorted(lojas, key=lambda l: ordem_lojas.index(l) if l in ordem_lojas else 9):
                if lojas[l]:
                    mix.append(lojas[l].pop(0))
        por_cat[g] = mix[:por_aba]
    saida, maximo, vistos, links_vistos = [], int(regras.get("achados_max_site", 200)), set(), set()
    while len(saida) < maximo and any(por_cat.values()):
        for cat in list(por_cat):
            if por_cat[cat] and len(saida) < maximo:
                v = por_cat[cat].pop(0)
                chave = (v["loja"], _txt(v.get("t") or v.get("n"))[:45])
                if chave in vistos:
                    continue                           # o mesmo anúncio apareceu em duas categorias
                vistos.add(chave)
                item = {k: v[k] for k in ("n", "cat", "ic", "loja", "por", "nota", "vendas", "img", "od", "link") if k in v}
                item["ofertas"] = ofertas_do_achado(v, guard, regras)
                links = {o["link"] for o in item["ofertas"] if not o.get("sim")}
                if links & links_vistos:
                    continue                           # esse produto já saiu num card (com todas as lojas dentro)
                links_vistos |= links
                item["por"] = item["ofertas"][0]["por"]          # "a partir de": o menor preço entre as lojas
                saida.append(item)
    resumo = {g: sum(1 for i in saida if i["cat"] == g) for g in por_cat}
    log("Achados por aba: " + " · ".join(f"{g} {q}" for g, q in resumo.items()))
    return saida


def achados_ali(regras, meta, guard, agora):
    """Achados do AliExpress pelo site (API oficial de afiliados): a cada rodada faz algumas buscas por aba
    (lista GRUPOS_BUSCAS), com o mesmo filtro rígido do garimpo do canal."""
    if not regras.get("achados_ali_ativo", True) or not (os.environ.get("ALI_APP_KEY") and os.environ.get("ALI_SECRET")):
        return
    tracking = os.environ.get("ALI_TRACKING_ID", "kaivolt")
    vmin = int(regras.get("garimpo_vendas_min", 500))
    amin = float(regras.get("garimpo_avaliacao_min", 94))
    pmin = float(regras.get("garimpo_preco_min", 15))
    proibidas = [x.lower() for x in regras.get("garimpo_excluir", []) + regras.get("ml_garimpo_excluir", [])]
    for _ in range(int(regras.get("achados_ali_buscas_por_rodada", 4))):
        g, kw, volta = proxima_busca(meta, "ali", regras)
        if not kw:
            return
        pmax = min(float(regras.get("achados_preco_max_externas", 600)), GRUPO_TETO.get(g, 600))
        try:
            resp = ali_chamar("aliexpress.affiliate.product.query", {
                "target_currency": "BRL", "target_language": "PT", "ship_to_country": "BR",
                "tracking_id": tracking, "keywords": kw, "sort": "LAST_VOLUME_DESC", "page_size": 40, "page_no": volta % 2 + 1})
        except Exception as e:
            log(f"Achados AliExpress: '{kw}' ERRO ->", e)
            continue
        cands = []
        for it in ali_produtos(resp):
            pid = str(it.get("product_id") or "")
            titulo = it.get("product_title") or ""
            aval, vendas = ali_num(it.get("evaluate_rate")), int(ali_num(it.get("lastest_volume")))
            por = ali_num(it.get("target_sale_price") or it.get("sale_price"))
            if not pid or not aval or aval < amin or vendas < vmin or not (pmin <= por <= pmax):
                continue
            if any(x in titulo.lower() for x in proibidas) or not palavras_ok(titulo, None, None):
                continue
            if not grupo_do_produto(titulo, hint=g):
                reprova("fora da aba (não segue o que a aba promete)")
                continue
            if g in ("Moda", "Beleza", "Esportes") and tem_marca_replica(titulo):
                reprova("marca com muita réplica (AliExpress/Shopee)")
                continue
            cands.append({"loja": "AliExpress", "por": por, "pid": pid, "titulo": titulo, "nota": round(aval / 20, 1),
                          "vendas": vendas, "img": it.get("product_main_image_url") or "",
                          "url_prod": it.get("product_detail_url") or f"https://pt.aliexpress.com/item/{pid}.html"})
        cands = tira_suspeitos(cands)
        cands.sort(key=lambda c: (c["nota"], math.log10(max(c["vendas"], 1))), reverse=True)
        novos = 0
        for c in cands[:int(regras.get("achados_ali_por_busca", 4))]:
            chave = "ali:" + c["pid"]
            link = (guard.get(chave) or {}).get("link") or ali_link_produto(c, tracking)
            if not link:
                continue
            antes = guard.get(chave) or {}
            guard[chave] = {"n": titulo_curto(c["titulo"], 70), "t": c["titulo"][:140], "g": g,
                            "loja": "AliExpress", "por": round(c["por"], 2), "nota": c["nota"], "vendas": c["vendas"],
                            "img": c["img"], "od": 0, "link": link, "visto": agora.isoformat(timespec="minutes")}
            _mantem_comparacao(guard[chave], antes)
            novos += 1
        log(f"Achados AliExpress: [{g}] '{kw}' -> {len(cands)} aprovados, {novos} no site")


# ======================================================================
# SHOPEE — achados do site e garimpo do canal (busca por aba, API oficial de afiliados)
# ======================================================================
def shopee_ok():
    return bool(os.environ.get("SHOPEE_APP_ID") and os.environ.get("SHOPEE_SECRET"))


def shopee_busca(kw, pagina=1, limite=30):
    campos = "itemId shopId productName price priceMin priceMax priceDiscountRate sales ratingStar imageUrl offerLink productLink"
    q = f"{{ productOfferV2(keyword: {json.dumps(kw)}, sortType: 2, page: {int(pagina)}, limit: {int(limite)}) {{ nodes {{ {campos} }} }} }}"
    return shopee_chamar(q)["productOfferV2"]["nodes"] or []


def shopee_cands(nodes, regras, g, vendas_min=None):
    """Filtro de qualidade da Shopee: nota, vendas, faixa de preço, palavras proibidas e aba certa."""
    nmin = float(regras.get("achados_shopee_nota_min", 4.7))
    vmin = int(vendas_min if vendas_min is not None else regras.get("achados_shopee_vendas_min", 300))
    pmin = float(regras.get("garimpo_preco_min", 15))
    pmax = min(float(regras.get("achados_preco_max_externas", 600)), GRUPO_TETO.get(g, 600))
    proibidas = [x.lower() for x in regras.get("garimpo_excluir", []) + regras.get("ml_garimpo_excluir", [])]
    out = []
    for n in nodes:
        pid, tit = str(n.get("itemId") or ""), n.get("productName") or ""
        try:
            nota, vendas = float(n.get("ratingStar") or 0), int(n.get("sales") or 0)
            por = float(n.get("priceMin") or n.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if not pid:
            continue
        if nota < nmin:
            reprova("nota baixa")
            continue
        if vendas < vmin or not (pmin <= por <= pmax):
            continue
        if any(x in tit.lower() for x in proibidas) or not palavras_ok(tit, None, None):
            continue
        if not grupo_do_produto(tit, hint=g):
            reprova("fora da aba (não segue o que a aba promete)")
            continue
        if g in ("Moda", "Beleza", "Esportes") and tem_marca_replica(tit):
            reprova("marca com muita réplica (AliExpress/Shopee)")
            continue
        out.append({"loja": "Shopee", "por": por, "pid": pid, "titulo": tit, "nota": round(nota, 1), "vendas": vendas,
                    "img": n.get("imageUrl") or "", "link": n.get("offerLink") or "", "url_prod": n.get("productLink") or ""})
    out = tira_suspeitos(out)
    out.sort(key=lambda c: (c["nota"], math.log10(max(c["vendas"], 1))), reverse=True)
    return out


def shopee_link(c):
    """Link de afiliado do produto (o offerLink da busca; se não vier, gera um link curto)."""
    if c.get("link"):
        return c["link"]
    if c.get("url_prod"):
        try:
            return shopee_link_curto(c["url_prod"])
        except Exception as e:
            log("    aviso: link curto da Shopee falhou:", e)
    return ""


def achados_shopee(regras, meta, guard, agora):
    """Achados da Shopee pelo site: a cada rodada faz algumas buscas por aba (em português) e guarda os melhores."""
    if not regras.get("achados_shopee_ativo", True) or not shopee_ok():
        return
    for _ in range(int(regras.get("achados_shopee_buscas_por_rodada", 4))):
        g, kw, volta = proxima_busca(meta, "shopee", regras)
        if not kw:
            return
        try:
            nodes = shopee_busca(kw, pagina=volta % 2 + 1)
        except Exception as e:
            log(f"Achados Shopee: '{kw}' ERRO ->", e)
            continue
        cands = shopee_cands(nodes, regras, g)
        novos = 0
        for c in cands[:int(regras.get("achados_shopee_por_busca", 4))]:
            chave = "shp:" + c["pid"]
            link = (guard.get(chave) or {}).get("link") or shopee_link(c)
            if not link:
                continue
            antes = guard.get(chave) or {}
            guard[chave] = {"n": titulo_curto(c["titulo"], 70), "t": c["titulo"][:140], "g": g,
                            "loja": "Shopee", "por": round(c["por"], 2), "nota": c["nota"], "vendas": c["vendas"],
                            "img": c["img"], "od": 0, "link": link, "visto": agora.isoformat(timespec="minutes")}
            _mantem_comparacao(guard[chave], antes)
            novos += 1
        log(f"Achados Shopee: [{g}] '{kw}' -> {len(nodes)} lidos, {len(cands)} aprovados, {novos} no site")


def garimpo_shopee(regras, st):
    """Garimpo do canal na Shopee: produto novo (nunca repetido em 30 dias), nota alta e muita venda, aba por aba."""
    if not shopee_ok() or not regras.get("garimpo_shopee_ativo", True):
        return None
    ja = st.setdefault("garimpo", {})
    limite = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    for _ in range(3):                               # até 3 buscas diferentes por post
        g, kw, volta = proxima_busca(st, "shopee", regras)
        if not kw:
            return None
        log(f"  Garimpo Shopee: [{g}] buscando '{kw}'")
        try:
            nodes = shopee_busca(kw, pagina=volta % 3 + 1)
        except Exception as e:
            log("  Garimpo Shopee: a Shopee não respondeu:", e)
            return None
        cands = [c for c in shopee_cands(nodes, regras, g, int(regras.get("garimpo_shopee_vendas_min", 1000)))
                 if ja.get("shp:" + c["pid"], "") < limite]
        for c in cands[:4]:
            link = shopee_link(c)
            if not link:
                continue
            ja["shp:" + c["pid"]] = hoje()
            for k in sorted(ja, key=ja.get)[:-3000]:
                del ja[k]
            c["link"], c["de"] = link, c["por"]
            return {"id": "shp:" + c["pid"], "n": titulo_curto(c["titulo"]), "garimpo": 1,
                    "nota": c["nota"], "vendas": c["vendas"], "ofertas": [c]}
        log(f"  Garimpo Shopee: nada bom o suficiente em '{kw}', tentando outra busca")
    return None


def shopee_teste():
    """python robo.py --shopee-teste  -> confere se as chaves da Shopee funcionam (não mostra nenhuma chave)."""
    if not shopee_ok():
        log("SHOPEE: faltam SHOPEE_APP_ID / SHOPEE_SECRET (cadastre em Settings > Secrets > Actions)")
        return 1
    try:
        nodes = shopee_busca("fone bluetooth", limite=10)
    except Exception as e:
        log("SHOPEE: a API respondeu com erro ->", e)
        return 1
    log(f"SHOPEE: API OK — {len(nodes)} produtos na busca 'fone bluetooth'")
    for n in nodes[:5]:
        log(f"   · {str(n.get('productName'))[:60]} | R$ {n.get('priceMin') or n.get('price')} | nota {n.get('ratingStar')} | "
            f"{n.get('sales')} vendas | link de afiliado: {'sim' if n.get('offerLink') else 'NÃO veio'}")
    bons = shopee_cands(nodes, REGRAS or {}, "Áudio e TV", 0)
    log(f"SHOPEE: {len(bons)} desses passariam no filtro de qualidade do site")
    return 0


def comparar_teste(titulo, preco):
    """python robo.py --comparar-teste "título do produto" 120  -> mostra o que o comparador acha nas 3 lojas."""
    ident = identidade(titulo)
    log("Identidade:", {k: v for k, v in ident.items() if k != "tokens"}, "| tipo:", tipo_do_produto(titulo))
    grupo = grupo_pelo_titulo(titulo) or ""
    v = {"por": preco, "t": titulo, "n": titulo}
    for loja in ("Mercado Livre", "Shopee", "AliExpress"):
        try:
            o = comparar_loja(loja, v, ident, grupo, REGRAS)
            log(f"  {loja}: " + (f"{'PARECIDO' if o.get('sim') else 'IGUAL'} · R$ {o['por']:.2f} · nota {o['nota']} · {o['vendas']} vendas · "
                                 f"{o['t'][:60]} · link {'ok' if o['link'] else 'SEM LINK'}" if o else "não achou"))
        except Exception as e:
            log(f"  {loja}: ERRO -> {str(e)[:200]}")
    return 0


def garimpo_ml(regras, st):
    """Garimpo do Mercado Livre: pega as 'Ofertas do dia' de TODAS as categorias (em rodízio), aplica o filtro
    de qualidade (nota, vendas, faixa de preço, palavras proibidas), nunca repete em 30 dias.
    O melhor vira o post; os outros bons vão pra aba 'Mercado Livre' da página do WhatsApp."""
    cats = regras.get("ml_garimpo_categorias")
    if cats is None:
        cats = [""]
    ja = st.setdefault("garimpo", {})
    limite = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    for tentativa in range(2):
        n = st.get("ml_n", 0)
        cat, pagina = cats[n % len(cats)], (n // len(cats)) % 3 + 1
        st["ml_n"] = n + 1
        log(f"  Garimpo ML: lendo ofertas {ML_CATS.get(cat, (cat,))[0]} (página {pagina})")
        try:
            cards = ml_cards_ofertas(ml_url_ofertas(cat, pagina))
        except Exception as e:
            log("  Garimpo ML: não consegui ler a página ->", e)
            return None
        cands = ml_filtra(cards, regras, ja, limite)
        log(f"  Garimpo ML: {len(cards)} ofertas lidas, {len(cands)} passaram no filtro")
        if not cands:
            continue
        posts = []
        for c in cands[:13]:
            ja["ml:" + c["pid"]] = hoje()
            of = {"loja": "Mercado Livre", "de": c["por"], "por": c["por"], "pid": c["pid"], "titulo": c["titulo"],
                  "nota": c["nota"], "vendas": c["vendas"], "img": c["img"],
                  "link": ml_link_afiliado(c["url"], c["item"], regras)}
            posts.append({"id": "ml:" + c["pid"], "n": titulo_curto(c["titulo"]), "garimpo": 1, "oferta_dia": c["oferta_dia"],
                          "nota": c["nota"], "vendas": c["vendas"], "ofertas": [of]})
        for k in sorted(ja, key=ja.get)[:-3000]:
            del ja[k]
        agora = datetime.now(TZ_BR).strftime("%d/%m %H:%M")
        st["ml_extras"] = [{"d": agora, "n": p["n"], "txt": montar_post(p, "zap")} for p in posts[1:]]
        return posts[0]
    return None


def divulgar(saida, historico, regras):
    """Canal do Telegram (e página do WhatsApp):
    - posta das 'telegram_inicio' às 'telegram_fim' (Brasília), 1 post a cada 'telegram_intervalo_min' minutos
    - prioridade 1: produto do site que CAIU de preço de verdade (10%+)
    - prioridade 2: destaque do site às 12h e às 20h
    - resto do tempo: GARIMPO — produto novo (Mercado Livre e AliExpress, todas as categorias) com nota alta e muita venda, nunca repetido
    - máximo 'telegram_max_dia' posts por dia"""
    st = historico.setdefault("_divulgacao", {})
    for k, v in (("postados", {}), ("slots", {}), ("fila_zap", []), ("garimpo", {}), ("kw", 0), ("ultimo", ""), ("dia", {})):
        st.setdefault(k, v)
    agora = datetime.now(TZ_BR)
    dia = agora.strftime("%Y-%m-%d")
    if st["dia"].get("d") != dia:
        st["dia"] = {"d": dia, "n": 0}
    forcar = "--tg-teste" in sys.argv
    ini, fim = int(regras.get("telegram_inicio", 8)), int(regras.get("telegram_fim", 23))
    intervalo = int(regras.get("telegram_intervalo_min", 45))
    max_dia = int(regras.get("telegram_max_dia", 18))
    try:
        desde = (agora.replace(tzinfo=None) - datetime.strptime(st["ultimo"], "%Y-%m-%d %H:%M")).total_seconds() / 60
    except ValueError:
        desde = 9999

    def dias_desde(pid):
        v = st["postados"].get(pid)
        if not v:
            return 999
        return (agora.replace(tzinfo=None) - datetime.strptime(v["d"], "%Y-%m-%d %H:%M")).days

    pode = forcar or (ini <= agora.hour < fim and st["dia"]["n"] < max_dia and desde >= intervalo - 3)
    if not pode:
        log(f"  Canal: sem post agora ({st['dia']['n']}/{max_dia} hoje, último há {int(min(desde, 9999))} min)")
        return pagina_zap(st["fila_zap"], st.get("ml_extras"))

    post = None
    # 1) queda real de preço num produto do site
    for p in sorted([p for p in saida if queda(p) >= int(regras.get("queda_minima_alerta", 10))], key=queda, reverse=True):
        ult = st["postados"].get(p["id"])
        if dias_desde(p["id"]) >= 3 or (ult and p["ofertas"][0]["por"] <= ult["p"] * 0.95):
            post = p; break
    # 2) destaque do site às 12h e às 20h
    slot = f"{dia}-{agora.hour}"
    if not post and agora.hour in (12, 20) and slot not in st["slots"]:
        livres = [p for p in saida if dias_desde(p["id"]) >= 5] or list(saida)
        livres.sort(key=lambda p: (p.get("vendas", 0), len(p["ofertas"])), reverse=True)
        if livres:
            post = livres[0]
        st["slots"][slot] = 1
    # 3) garimpo: produto novo, girando entre Mercado Livre, AliExpress e Shopee (se um falhar, tenta o próximo)
    if not post:
        fontes = [garimpo_ml, garimpo_ali, garimpo_shopee]
        if not regras.get("ml_garimpo_ativo", True):
            fontes = [garimpo_ali, garimpo_shopee]
        vez = int(st.get("vez", 0))
        for i in range(len(fontes)):                       # a loja da vez; se ela falhar, tenta a próxima
            idx = (vez + i) % len(fontes)
            post = fontes[idx](regras, st)
            if post:
                st["vez"] = idx + 1                        # a próxima vez começa na loja seguinte
                break
        else:
            st["vez"] = vez + 1
    # 4) se o garimpo falhar, um produto do site que não aparece há mais tempo
    if not post and saida:
        post = sorted(saida, key=lambda p: -dias_desde(p["id"]))[0]
        if dias_desde(post["id"]) < 2:
            post = None

    if post:
        tem_tg = os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")
        if tem_tg and not DEMO:
            tg_postar(post)
        else:
            log("  Telegram: faltam TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (post só foi pra página do WhatsApp)")
        if not post.get("garimpo"):
            st["postados"][post["id"]] = {"d": agora.strftime("%Y-%m-%d %H:%M"), "p": post["ofertas"][0]["por"]}
        st["ultimo"] = agora.strftime("%Y-%m-%d %H:%M")
        st["dia"]["n"] += 1
        st["fila_zap"].insert(0, {"d": agora.strftime("%d/%m %H:%M"), "n": post["n"], "txt": montar_post(post, "zap")})
    st["fila_zap"] = st["fila_zap"][:30]
    for k in sorted(st["slots"])[:-20]:
        del st["slots"][k]
    return pagina_zap(st["fila_zap"], st.get("ml_extras"))


def pagina_zap(fila, extras=None):
    """Página kaivolt.com.br/ofertas/zap.html: posts prontos pra copiar e colar no canal do WhatsApp.
    Ao copiar, o post fica marcado como 'postado' (só neste celular) pra você não repetir."""
    import html as H
    extras = extras or []
    itens_ml = "".join(
        f'<div class="p x" data-id="{H.escape("ml|" + x["n"])}"><div class="h"><b>{H.escape(x["n"])}</b><span>{x["d"]}</span></div>'
        f'<pre>{H.escape(x["txt"])}</pre>'
        f'<div class="b"><button class="c">Copiar</button><button class="m">Já postei</button></div><div class="ok">✓ Postado no canal</div></div>'
        for x in extras)
    if itens_ml:
        itens_ml = ('<h2>Mais achados do Mercado Livre</h2><p class=s>Ofertas do dia que passaram no filtro de qualidade. '
                    'Já estão com o teu link de afiliado: escolhe as melhores e posta.</p>' + itens_ml)
    itens = "".join(
        f'<div class="p" data-id="{H.escape(x["d"] + "|" + x["n"])}"><div class="h"><b>{H.escape(x["n"])}</b><span>{x["d"]}</span></div>'
        f'<pre>{H.escape(x["txt"])}</pre>'
        f'<div class="b"><button class="c">Copiar</button><button class="m">Já postei</button></div><div class="ok">✓ Postado no canal</div></div>'
        for x in fila) or "<p>Nenhum post ainda. O robô prepara um novo a cada 20 minutos, das 8h às 23h.</p>"
    return ("<!doctype html><html lang=pt-BR><head><meta charset=utf-8><meta name=robots content=noindex>"
            "<meta name=viewport content='width=device-width,initial-scale=1'><meta name=theme-color content='#07060b'>"
            "<meta name=apple-mobile-web-app-capable content=yes><title>Kaivolt WhatsApp</title>"
            "<style>body{margin:0;background:#07060b;color:#f4f2ff;font-family:system-ui,sans-serif;padding:0 16px 40px}"
            ".top{position:sticky;top:0;background:#07060b;padding:14px 0 10px;z-index:2;display:flex;justify-content:space-between;align-items:center;gap:10px}"
            "h1{font-size:19px;margin:0}.n{background:#22c55e;color:#04150a;font-weight:800;border-radius:99px;padding:5px 12px;font-size:14px}"
            ".f{display:flex;gap:8px;margin:0 0 14px}.f button{flex:1;padding:10px;border-radius:10px;border:1px solid #2a2340;background:#13101d;color:#d9d4ee;font-weight:700}"
            ".f button.on{background:#8b5cf6;color:#fff;border-color:#8b5cf6}"
            ".p{background:#13101d;border:1px solid #2a2340;border-radius:14px;padding:14px;margin:0 0 14px}"
            ".h{display:flex;justify-content:space-between;gap:10px;margin-bottom:8px}.h span{color:#9d97b3;font-size:13px;white-space:nowrap}"
            "pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.45 system-ui,sans-serif;margin:0 0 12px;color:#d9d4ee}"
            ".b{display:flex;gap:8px}.b button{flex:1;padding:12px;border-radius:10px;border:0;font-weight:700;font-size:15px}"
            ".c{background:#8b5cf6;color:#fff}.m{background:#221c35;color:#d9d4ee}"
            ".ok{display:none;color:#22c55e;font-weight:700;margin-top:10px}.p.feito{opacity:.45}.p.feito .ok{display:block}.p.feito .b{display:none}"
            "body.so-novos .p.feito{display:none}h2{font-size:17px;margin:26px 0 4px}.s{color:#9d97b3;font-size:13px;margin:0 0 12px}"
            ".p.x{border-color:#3b2d6b}</style></head><body>"
            "<div class=top><h1>Posts pro WhatsApp</h1><span class=n id=n></span></div>"
            "<div class=f><button id=fn class=on>Só os novos</button><button id=ft>Todos</button></div>"
            + itens + itens_ml +
            "<script>var K='kv-zap-postados',S={};try{S=JSON.parse(localStorage.getItem(K)||'{}')}catch(e){}"
            "function salva(){try{localStorage.setItem(K,JSON.stringify(S))}catch(e){}}"
            "function atualiza(){var n=0;document.querySelectorAll('.p').forEach(function(p){var f=!!S[p.dataset.id];p.classList.toggle('feito',f);if(!f)n++});"
            "document.getElementById('n').textContent=n+(n==1?' novo':' novos')}"
            "function marca(p){S[p.dataset.id]=1;salva();atualiza()}"
            "document.body.classList.add('so-novos');"
            "document.getElementById('fn').onclick=function(){document.body.classList.add('so-novos');this.classList.add('on');document.getElementById('ft').classList.remove('on')};"
            "document.getElementById('ft').onclick=function(){document.body.classList.remove('so-novos');this.classList.add('on');document.getElementById('fn').classList.remove('on')};"
            "document.querySelectorAll('.p').forEach(function(p){var t=p.querySelector('pre').innerText;"
            "p.querySelector('.c').onclick=function(){var b=this;(navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(function(){b.textContent='Copiado!';setTimeout(function(){marca(p)},900)},"
            "function(){var r=document.createRange();r.selectNodeContents(p.querySelector('pre'));var s=getSelection();s.removeAllRanges();s.addRange(r);document.execCommand('copy');b.textContent='Copiado!';setTimeout(function(){marca(p)},900)})};"
            "p.querySelector('.m').onclick=function(){marca(p)}});atualiza();</script></body></html>")


def limpa_host(h):
    h = (h or "").strip()
    for pre in ("ftps://", "ftp://", "sftp://", "http://", "https://"):
        if h.lower().startswith(pre):
            h = h[len(pre):]
    return h.strip("/").split("/")[0].split(":")[0]


def enviar_ftp(resultado, extras=None):
    """resultado=None -> manda só os arquivos extras (ex.: zap.html do workflow do canal)."""
    host = limpa_host(os.environ["FTP_HOST"])
    user, senha = os.environ["FTP_USER"].strip(), os.environ["FTP_PASS"]
    dominio = os.environ.get("SITE_DOMINIO", "kaivolt.com.br")
    dados = json.dumps(resultado, ensure_ascii=False).encode("utf-8") if resultado else None
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
        if dados:
            ftp.storbinary("STOR ofertas.json", io.BytesIO(dados))
            log(f"enviado via {modo} para:", ftp.pwd() + "/ofertas.json")
        extras = dict(extras or {})
        # sem cache no navegador pra zap.html e ofertas.json (senão o celular mostra versão velha por dias)
        extras.setdefault(".htaccess", '<IfModule mod_headers.c>\n<FilesMatch "\\.(html|json)$">\n  Header set Cache-Control "no-cache, must-revalidate"\n</FilesMatch>\n</IfModule>\n')
        # página do grupo (anúncios): se existir grupo.html no repositório, vai junto pro site
        arq_grupo = os.path.join(PASTA, "grupo.html")
        if os.path.exists(arq_grupo):
            extras.setdefault("grupo.html", open(arq_grupo, encoding="utf-8").read())
        for nome, conteudo in extras.items():
            ftp.storbinary(f"STOR {nome}", io.BytesIO(conteudo.encode("utf-8") if isinstance(conteudo, str) else conteudo))
            log("enviado também:", ftp.pwd() + "/" + nome)
    # confere se o site já está servindo o arquivo
    if not dados:
        return
    try:
        url = f"https://{dominio}/ofertas/ofertas.json?v={int(time.time())}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 robo-kaivolt"})
        with urllib.request.urlopen(req, timeout=30) as r:
            log("site OK:", url.split("?")[0], "->", r.status)
    except Exception as e:
        log("ATENÇÃO: o site ainda não mostra o arquivo:", e)


ARQ_CANAL = os.path.join(PASTA, "canal.json")


def ler_ofertas_site():
    dominio = os.environ.get("SITE_DOMINIO", "kaivolt.com.br")
    try:
        req = urllib.request.Request(f"https://{dominio}/ofertas/ofertas.json?v={int(time.time())}",
                                     headers={"User-Agent": "Mozilla/5.0 robo-kaivolt"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8")).get("produtos", [])
    except Exception as e:
        log("Canal: não consegui ler as ofertas do site (", e, ") — sigo só com o garimpo")
        return []


def canal():
    """Workflow do canal: roda 1 vez por hora e fica ~45 min no ar postando no ritmo certo
    (1 oferta a cada 'telegram_intervalo_min'). Assim os atrasos do agendador do GitHub
    não fazem o canal ficar parado. O estado do canal fica em canal.json (separado do histórico
    de preços, pra os dois robôs nunca brigarem pelo mesmo arquivo)."""
    base = ler_json(ARQ_PRODUTOS, None)
    regras = base["regras"]
    REGRAS.update(regras)
    estado = ler_json(ARQ_CANAL, {})
    if not estado:                                   # primeira vez: traz o que estava no histórico
        antigo = ler_json(ARQ_HISTORICO, {}).get("_divulgacao")
        if antigo:
            estado = {"_divulgacao": antigo}
    teste = "--tg-teste" in sys.argv
    rodadas = 1 if teste else int(os.environ.get("CANAL_RODADAS", "3"))
    intervalo = int(regras.get("telegram_intervalo_min", 20))
    for r in range(rodadas):
        log(f"Canal: rodada {r + 1}/{rodadas}")
        zap = divulgar(ler_ofertas_site(), estado, regras)
        salvar_json(ARQ_CANAL, estado)
        if not DEMO and os.environ.get("FTP_HOST") and zap:
            try:
                enviar_ftp(None, {"zap.html": zap})
            except Exception as e:
                log("Canal: não consegui atualizar a página do WhatsApp:", e)
        if r < rodadas - 1:
            # espera até a próxima vaga (último post + intervalo)
            try:
                ult = datetime.strptime(estado["_divulgacao"]["ultimo"], "%Y-%m-%d %H:%M")
                falta = intervalo * 60 - (datetime.now(TZ_BR).replace(tzinfo=None) - ult).total_seconds()
            except (KeyError, ValueError):
                falta = intervalo * 60
            espera = int(min(max(falta + 30, 60), intervalo * 60 + 30))
            log(f"Canal: próxima rodada em {espera // 60} min")
            time.sleep(espera)


if __name__ == "__main__":
    if "--shopee-teste" in sys.argv:
        REGRAS.update((ler_json(ARQ_PRODUTOS, {}) or {}).get("regras", {}))
        sys.exit(shopee_teste())
    if "--comparar-teste" in sys.argv:
        REGRAS.update((ler_json(ARQ_PRODUTOS, {}) or {}).get("regras", {}))
        i = sys.argv.index("--comparar-teste")
        sys.exit(comparar_teste(sys.argv[i + 1], float(sys.argv[i + 2].replace(",", "."))))
    canal() if "--canal" in sys.argv else main()
