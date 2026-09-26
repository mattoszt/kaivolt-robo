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


def palavras_ok(titulo, palavras, excluir=None):
    t = (titulo or "").lower()
    if any(x.lower() in t for x in (excluir or [])):
        return False
    return all(p.lower() in t for p in (palavras or []))


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
        if nota < regras["nota_minima"] or vendas < regras["vendas_minimas_shopee"]:
            continue
        if not palavras_ok(n.get("productName"), cfg.get("palavras"), cfg.get("excluir")):
            continue
        por = float(n.get("priceMin") or n.get("price") or 0)
        if por <= 0:
            continue
        if cfg.get("preco_min") and por < float(cfg["preco_min"]):
            continue
        if cfg.get("preco_max") and por > float(cfg["preco_max"]):
            continue
        taxa = float(n.get("priceDiscountRate") or 0)
        de = round(por / (1 - taxa / 100), 2) if 0 < taxa < 95 else por
        link = n.get("offerLink") or shopee_link_curto(n.get("productLink"))
        candidatos.append({"loja": "Shopee", "de": de, "por": por, "link": link,
                           "nota": nota, "vendas": vendas, "titulo": n.get("productName"),
                           "img": n.get("imageUrl")})
    ordem = custo_beneficio(candidatos)
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
        desc = (1 - c["por"] / c["de"]) if c.get("de") and c["de"] > c["por"] else 0
        return (c.get("nota") or 0) * 2 + math.log10(max(c.get("vendas") or 1, 1)) + desc * 2 - (c["por"] / menor - 1) * 4
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
            continue
        if vendas < vmin:                                               # poucas vendas = descarta
            continue
        if not palavras_ok(it.get("product_title"), cfg.get("palavras"), cfg.get("excluir")):
            continue
        por = ali_num(it.get("target_sale_price") or it.get("sale_price"))
        de = ali_num(it.get("target_original_price") or it.get("original_price")) or por
        link = it.get("promotion_link") or ""
        if por <= 0 or not link:
            continue
        if cfg.get("preco_min") and por < float(cfg["preco_min"]):     # barato demais = suspeito
            continue
        if cfg.get("preco_max") and por > float(cfg["preco_max"]):
            continue
        pid = str(it.get("product_id") or "")
        url_prod = it.get("product_detail_url") or (f"https://pt.aliexpress.com/item/{pid}.html" if pid else "")
        candidatos.append({"loja": "AliExpress", "de": de, "por": por, "link": link, "url_prod": url_prod, "pid": pid,
                           "nota": round(aval / 20, 1), "vendas": vendas,
                           "titulo": it.get("product_title"),
                           "img": it.get("product_main_image_url")})
    ordem = custo_beneficio(candidatos)
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
# MERCADO LIVRE / AMAZON
# ======================================================================
def manual_melhor(loja, cfg):
    # Compatibilidade: se você já colocou um link de afiliado manual do Meli,
    # ele continua tendo prioridade e a API só atualiza o preço.
    if loja == "Mercado Livre" and cfg.get("link") and (cfg.get("url") or cfg.get("item")):
        o = ml_melhor(cfg)
        if o:
            return o
    if not cfg.get("link") or not cfg.get("por"):
        return None
    por = float(cfg["por"])
    return {"loja": loja, "de": float(cfg.get("de") or por), "por": por, "link": cfg["link"]}


# ======================================================================
# MERCADO LIVRE — API oficial
# Pode atualizar um anúncio escolhido OU procurar a melhor oferta sozinho.
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
        # A busca pública ainda pode funcionar sem token; não derruba o robô.
        log("  Mercado Livre: token indisponível; tentando consulta pública:", e)
        _ML_TOKEN["t"] = None
    return _ML_TOKEN["t"]


def ml_get(caminho):
    tok = ml_token()
    h = {"Accept": "application/json", "User-Agent": "robo-kaivolt/1.0"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    return http(ML_API + caminho, None, h)


def ml_melhor(cfg):
    """Atualiza o preço do anúncio escolhido. O link continua sendo o link configurado por você."""
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
                log("  Mercado Livre: anúncio pausado/finalizado, usando busca automática")
                return None
            por, de = d.get("price"), d.get("original_price")
        else:
            return None
        if not por:
            return None
        por = float(por); de = float(de or cfg.get("de") or por)
        log(f"  Mercado Livre: preço atualizado pela API -> R$ {por:.2f}")
        return {"loja": "Mercado Livre", "de": max(de, por), "por": por, "link": cfg["link"]}
    except Exception as e:
        log("  Mercado Livre: anúncio configurado não respondeu:", e)
        return None


def ml_nota_reputacao(item):
    """Converte a reputação disponível na busca em uma nota aproximada de 0 a 5."""
    seller = item.get("seller") or {}
    rep = seller.get("seller_reputation") or seller.get("reputation") or {}
    nivel = (rep.get("level_id") or "").lower()
    mapa = {
        "5_green": 5.0,
        "4_light_green": 4.7,
        "3_yellow": 4.3,
        "2_orange": 3.8,
        "1_red": 3.0,
    }
    return mapa.get(nivel, 4.5)


def ml_buscar_melhor(cfg, regras):
    """Procura anúncios do Mercado Livre e escolhe uma boa oferta automaticamente."""
    busca = (cfg.get("busca") or "").strip()
    if not busca:
        return None

    params = {
        "q": busca,
        "limit": int(cfg.get("limite_busca", 50)),
    }
    if cfg.get("frete_gratis"):
        params["shipping_cost"] = "free"
    # relevância costuma ser melhor para evitar acessórios aleatórios; o ranking
    # final do Kaivolt decide entre os candidatos aprovados.
    caminho = "/sites/MLB/search?" + urllib.parse.urlencode(params)
    resp = ml_get(caminho)
    itens = resp.get("results") or []
    log(f"    Mercado Livre trouxe {len(itens)} resultados para '{busca}'")

    candidatos = []
    for it in itens:
        titulo = it.get("title") or ""
        if not palavras_ok(titulo, cfg.get("palavras"), cfg.get("excluir")):
            continue
        if (it.get("condition") == "used") and not cfg.get("aceitar_usado", False):
            continue

        try:
            por = float(it.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if por <= 0:
            continue
        if cfg.get("preco_min") and por < float(cfg["preco_min"]):
            continue
        if cfg.get("preco_max") and por > float(cfg["preco_max"]):
            continue

        try:
            de = float(it.get("original_price") or por)
        except (TypeError, ValueError):
            de = por
        de = max(de, por)

        link = it.get("permalink") or ""
        if not link:
            continue

        # Em buscas públicas, quantidade vendida nem sempre vem disponível.
        vendas = int(it.get("sold_quantity") or 0)
        nota = ml_nota_reputacao(it)
        candidatos.append({
            "loja": "Mercado Livre",
            "de": de,
            "por": por,
            "link": link,
            "nota": nota,
            "vendas": vendas,
            "titulo": titulo,
            "img": it.get("thumbnail") or "",
        })

    ordem = custo_beneficio(candidatos)
    for c in ordem[:3]:
        desc = round((1 - c["por"] / c["de"]) * 100) if c["de"] > c["por"] else 0
        log(f"    ML opção R$ {c['por']:.2f} · {desc}% off · {(c['titulo'] or '')[:58]}")
    return ordem[0] if ordem else None


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
            elif loja == "Mercado Livre":
                # 1) preserva link de afiliado manual, se existir;
                # 2) senão, pesquisa sozinho usando a configuração do ML;
                # 3) se o ML estiver vazio, reaproveita busca/filtros da Shopee.
                o = manual_melhor(loja, cfg)
                if not o:
                    auto = dict(prod.get("lojas", {}).get("Shopee", {}))
                    auto.update({k: v for k, v in cfg.items() if v not in (None, "", [], 0)})
                    o = ml_buscar_melhor(auto, regras)
            else:
                o = manual_melhor(loja, cfg)
            if o:
                ofertas.append(o)
            elif loja in ("Shopee", "AliExpress", "Mercado Livre"):
                log(f"  {loja}: nenhuma oferta passou nos filtros")
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


def hoje():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def main():
    base = ler_json(ARQ_PRODUTOS, None)
    if not base:
        sys.exit("produtos.json não encontrado")
    regras = base["regras"]
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
        notas = [o["nota"] for o in ofertas if o.get("nota")]
        img = prod.get("img") or ""
        saida.append({
            "id": prod["id"], "n": prod["n"], "c": prod["c"], "ic": prod.get("ic", ""),
            "img": img, "nota": round(max(notas), 1) if notas else 4.5,
            "selo": calcular_selo(h, menor),
            "ofertas": [{"loja": o["loja"], "de": round(o["de"], 2), "por": round(o["por"], 2),
                         "link": o["link"]} for o in ofertas],
        })
        log("  menor preço:", menor, "em", ofertas[0]["loja"])

    if not saida:
        log("NENHUM produto encontrado — o site continua com as ofertas anteriores (nada foi enviado).")
        salvar_json(ARQ_HISTORICO, historico)
        sys.exit(1)

    resultado = {"atualizado": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                 "produtos": saida}
    salvar_json(ARQ_SAIDA, resultado)
    salvar_json(ARQ_HISTORICO, historico)
    log(f"ok: {len(saida)} produtos em {ARQ_SAIDA}")

    if not DEMO and os.environ.get("FTP_HOST"):
        enviar_ftp(resultado)


def limpa_host(h):
    h = (h or "").strip()
    for pre in ("ftps://", "ftp://", "sftp://", "http://", "https://"):
        if h.lower().startswith(pre):
            h = h[len(pre):]
    return h.strip("/").split("/")[0].split(":")[0]


def enviar_ftp(resultado):
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
