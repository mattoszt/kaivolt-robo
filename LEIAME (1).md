# Robô da Kaivolt

Busca o **menor preço com boa qualidade** de cada produto na Shopee e no AliExpress,
gera os **seus links de afiliado**, guarda o **histórico de preços** e atualiza o site sozinho
(2x por dia: 08h e 19h, horário de Brasília).

## Como funciona

```
GitHub (grátis) roda o robô 2x por dia
  → robo.py busca as ofertas nas APIs (Shopee, AliExpress)
  → filtra qualidade (nota, vendas, avaliação) e pega o menor preço
  → junta com os links manuais (Mercado Livre, Amazon)
  → envia ofertas.json pro seu site (Hostinger, via FTP)
Site da Kaivolt → lê kaivolt.com.br/ofertas/ofertas.json e se atualiza sozinho
```

Enquanto o arquivo não existir, o site mostra os produtos de exemplo.

## Arquivos

| Arquivo | Pra que serve |
|---|---|
| `produtos.json` | **Você edita aqui**: quais produtos acompanhar e as regras de qualidade |
| `robo.py` | O robô (não precisa mexer) |
| `.github/workflows/robo.yml` | Agenda do robô no GitHub (não precisa mexer) |
| `historico.json` | Criado sozinho: preço de cada dia (usado no selo "menor preço em 30 dias") |

## Passo a passo pra ligar (fazer uma vez só)

**1. Chaves das lojas** (depois que os programas aprovarem)
- Shopee: painel de afiliado → Open API → copie **AppID** e **Secret**
- AliExpress: portals.aliexpress.com → API → **App Key**, **App Secret** e **Tracking ID**

**2. Acesso FTP da Hostinger**
- hPanel → Sites → seu site → **Arquivos → Contas FTP**
- Anote o **host**, **usuário** e **senha**

**3. Colocar o robô no GitHub**
- Crie uma conta em github.com e um repositório **privado** chamado `kaivolt-robo`
- Envie todos os arquivos desta pasta (inclusive a pasta `.github`)

**4. Guardar as chaves com segurança**
No repositório: **Settings → Secrets and variables → Actions → New repository secret**.
Crie um por um (nome exatamente assim):

| Nome | Valor |
|---|---|
| `SHOPEE_APP_ID` | AppID da Shopee |
| `SHOPEE_SECRET` | Secret da Shopee |
| `ALI_APP_KEY` | App Key do AliExpress |
| `ALI_SECRET` | App Secret do AliExpress |
| `ALI_TRACKING_ID` | Tracking ID do AliExpress |
| `FTP_HOST` | host do FTP da Hostinger |
| `FTP_USER` | usuário FTP |
| `FTP_PASS` | senha FTP |
| `FTP_PASTA` | `public_html/ofertas` |

> Nunca coloque essas chaves dentro dos arquivos nem mande pra ninguém.

**5. Rodar pela primeira vez**
Aba **Actions → Robô Kaivolt → Run workflow**. Em ~1 minuto o site já mostra as ofertas reais.
Se algo der errado, clique na execução e leia o log: o robô diz qual loja falhou e por quê.

## Adicionar ou trocar produtos (`produtos.json`)

```json
{
  "id": "mouse-gamer-rgb",
  "n": "Mouse gamer RGB 7200 DPI",
  "c": "pc",
  "ic": "mouse",
  "img": "",
  "lojas": {
    "Shopee":        { "busca": "mouse gamer rgb 7200 dpi", "palavras": ["mouse"] },
    "AliExpress":    { "busca": "mouse gamer rgb 7200dpi", "palavras": ["mouse"] },
    "Mercado Livre": { "link": "SEU LINK", "por": 74.90, "de": 109.90 },
    "Amazon":        { "link": "SEU LINK", "por": 69.90, "de": 99.90 }
  }
}
```

- `c` = categoria: `celular`, `pc`, `tablet` ou `audio`
- `ic` = desenho: `powerbank, charger, mouse, earbuds, stylus, keyboard, cable, speaker, headset, tabletcase, ssd, carmount, hub, stand, webcam, pelicula`
- **Shopee/AliExpress**: use `busca` (palavras-chave) + `palavras` (o título PRECISA ter essas palavras,
  pra não pegar produto errado). Se quiser travar num produto exato: `"itemId"`/`"shopId"` (Shopee) ou `"productId"` (AliExpress).
- **Mercado Livre/Amazon**: link e preço à mão (até liberarem API). Deixe `"link": ""` pra não aparecer.

## Regras de qualidade (`regras` no produtos.json)

| Regra | Padrão | O que faz |
|---|---|---|
| `nota_minima` | 4.5 | Shopee: descarta produto com nota menor |
| `vendas_minimas_shopee` | 100 | Shopee: descarta produto com poucas vendas |
| `avaliacao_minima_aliexpress` | 90 | AliExpress: % de avaliações positivas mínima |

Entre os que passam no filtro, o robô escolhe o **mais barato**.

## Testar no computador (opcional)

```
python robo.py --demo     (sem APIs, usa preços de exemplo → saida/ofertas.json)
python robo.py --teste    (com as chaves configuradas, mostra a resposta bruta das APIs)
```

## Segurança
- Se uma loja der erro, as outras continuam normalmente.
- Se nenhuma oferta for encontrada, o robô **não envia nada** e o site continua com as ofertas anteriores.
