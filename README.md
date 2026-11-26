# Trading
Trading diário

## Backtest US100 (`backtest_us100.py`)

Testa em dados históricos de 5 minutos os três setups de day trading no CFD US100 (US Tech 100 do Banco BiG):

| Setup | Sinal | Stop |
|---|---|---|
| **ORB** | 1.º fecho de vela de 5m fora do opening range dos primeiros 15 min, a favor do viés | Meio do range |
| **VWAP** | Em dia de tendência, a vela toca no VWAP/EMA20 e fecha a rejeitar | Além do último fundo/topo (6 velas) |
| **Falso rompimento** | Passa o PDH/PDL e fecha de volta para dentro, com vela de rejeição | Além do extremo do dia |

**Regras aplicadas**
- Viés do dia: fecho de ontem acima da SMA20 e a SMA20 acima da SMA50 no diário = long (ao contrário = short; misto = só falsos rompimentos).
- Risco de 2% por trade, com quantidade = risco USD ÷ (pontos de stop + spread) e 1 unidade = 1 USD/ponto.
- Stops só entre 25 e 60 pontos.
- Metade da posição fecha em +1R e o stop do resto sobe para a entrada; o resto fecha em +2R.
- Paragem diária com 2 perdas seguidas ou −4%.
- Entradas só entre 14:30 e 16:30 e entre 19:30 e 21:00 (Lisboa). Tudo fecha antes do fecho de NY.
- O spread (3,5 pts por defeito) é descontado em cada trade.
- Opcional: sem entradas 15 min antes/depois de notícias de impacto alto (`--noticias`).
- Quando o stop e o alvo calham na mesma vela, conta como stop (cenário conservador).

### Instalar
```bash
pip install -r requirements.txt
```

### Usar
```bash
# Testar o script com dados sintéticos
python backtest_us100.py --demo --grafico

# Com um CSV teu (datas em UTC)
python backtest_us100.py --csv dados/us100_5m.csv --tz UTC --grafico

# Últimos 60 dias do Nasdaq 100 pelo Yahoo (precisa de yfinance)
python backtest_us100.py --yahoo ^NDX

# Só alguns setups, outro capital, sem filtro de viés
python backtest_us100.py --csv dados.csv --capital 5000 --setups orb,falso --sem-vies
```

### Onde arranjar dados
- **Dukascopy** (grátis): Historical Data Feed → instrumento *USA 100 Tech Index* (USATECHIDXUSD), velas de 1 ou 5 min, em formato CSV e hora GMT. O script lê esse formato diretamente; se os dados vierem em 1 min, converte-os para 5 min.
- **TradingView**: exportar os dados do gráfico (*Export chart data*); a coluna `time` em unix é lida automaticamente.
- Qualquer CSV com colunas de data/hora, open, high, low, close e, se houver, volume. Se as datas não tiverem fuso, indica-o com `--tz` (ex.: `--tz Europe/Lisbon`).

Ficheiro de notícias (`--noticias`): um CSV com uma coluna de datas/horas de Lisboa, por exemplo:
```
data
2026-12-10 13:30
2026-12-16 19:00
```

### Resultados
- No ecrã: trades, taxa de acerto, R médio, profit factor, drawdown máximo e números por setup e por direção.
- `trades_backtest.csv`: cada trade com a entrada, o stop, os alvos, a quantidade, a saída e o resultado em USD e em R.
- `curva_capital.png`: a curva de capital (com `--grafico`).

> Ferramenta de estudo. Os resultados passados não garantem resultados futuros, e com menos de 30 trades a amostra não prova nada.
