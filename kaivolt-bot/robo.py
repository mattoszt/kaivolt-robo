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
import hashlib, hmac, json, math, os, re, sys, time, urllib.parse, urllib.request, ftplib, io
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


def montar_post(p, estilo):
    """estilo 'tg' = HTML do Telegram · 'zap' = negrito do WhatsApp (*assim*)."""
    o, q = p["ofertas"][0], queda(p)
    b = (lambda s: f"<b>{s}</b>") if estilo == "tg" else (lambda s: f"*{s}*")
    esc = (lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")) if estilo == "tg" else (lambda s: s)
    L = [b(esc(p["n"])), ""]
    if q:
        L.append(f"🔻 Caiu {q}%: era {reais(o['de'])} nos últimos dias")
    L.append(f"💰 {b(reais(o['por']))} no {o['loja']}")
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
        L.append(f'👉 <a href="{o["link"]}">Ver oferta no {o["loja"]}</a>')
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
    for bloco in pagina.split('class="andes-card poly-card')[1:]:
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


def achados_site(regras, historico):
    """Seção 'Achados do dia' do site: a cada rodada lê 2 categorias das Ofertas do dia do Mercado Livre,
    aplica o mesmo filtro de qualidade e guarda por até 'achados_horas'. Mistura as categorias no resultado."""
    if not regras.get("achados_ativo", True):
        return []
    cats = regras.get("achados_categorias") or [k for k in ML_CATS if k]
    meta = historico.setdefault("_achados_meta", {"n": 0})
    guard = historico.setdefault("_achados", {})
    agora = datetime.now(timezone.utc)
    for _ in range(int(regras.get("achados_categorias_por_rodada", 2))):
        cat = cats[meta["n"] % len(cats)]
        meta["n"] += 1
        try:
            cards = ml_cards_ofertas(ml_url_ofertas(cat))
        except Exception as e:
            log(f"Achados: não consegui ler {ML_CATS.get(cat, (cat,))[0]} ->", e)
            continue
        bons = ml_filtra(cards, regras)[:int(regras.get("achados_por_categoria", 8))]
        log(f"Achados: {ML_CATS.get(cat, (cat,))[0]} -> {len(cards)} lidas, {len(bons)} aprovadas")
        for c in bons:
            nome, ic = ML_CATS.get(cat, ("Ofertas", "🔥"))
            guard[c["pid"]] = {"n": titulo_curto(c["titulo"], 70), "t": c["titulo"][:140], "cat": nome, "ic": ic,
                               "loja": "Mercado Livre", "por": round(c["por"], 2), "nota": c["nota"],
                               "vendas": c["vendas"], "img": c["img"], "od": 1 if c["oferta_dia"] else 0,
                               "link": ml_link_afiliado(c["url"], c["item"], regras),
                               "visto": agora.isoformat(timespec="minutes")}
    horas = float(regras.get("achados_horas", 36))
    for k in [k for k, v in guard.items()
              if (agora - datetime.fromisoformat(v["visto"])).total_seconds() > horas * 3600]:
        del guard[k]
    # mistura as categorias (um de cada por vez) pra vitrine não ficar só de uma coisa
    por_cat = {}
    for v in sorted(guard.values(), key=lambda v: (v["od"], v["nota"], math.log10(max(v["vendas"], 1))), reverse=True):
        por_cat.setdefault(v["cat"], []).append(v)
    saida, maximo = [], int(regras.get("achados_max_site", 48))
    while len(saida) < maximo and any(por_cat.values()):
        for cat in list(por_cat):
            if por_cat[cat] and len(saida) < maximo:
                saida.append({k: v for k, v in por_cat[cat].pop(0).items() if k != "visto"})
    return saida


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
    # 3) garimpo: produto novo, alternando Mercado Livre e AliExpress (se um falhar, tenta o outro)
    if not post:
        ordem = [garimpo_ml, garimpo_ali] if st.get("vez_ml", True) else [garimpo_ali, garimpo_ml]
        if not regras.get("ml_garimpo_ativo", True):
            ordem = [garimpo_ali]
        for f in ordem:
            post = f(regras, st)
            if post:
                break
        st["vez_ml"] = not st.get("vez_ml", True)
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
    canal() if "--canal" in sys.argv else main()
