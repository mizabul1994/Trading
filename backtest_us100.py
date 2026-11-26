#!/usr/bin/env python3
"""
Backtest dos setups de day trading no US100 (CFD US Tech 100 do Banco BiG).

Setups testados (velas de 5 minutos):
  1. ORB   - rompimento do opening range dos primeiros 15 min, a favor do viés;
             stop no meio do range.
  2. VWAP  - em dia de tendência, pullback ao VWAP ou à média de 20 (EMA20)
             com vela de rejeição; stop além do último fundo/topo.
  3. FALSO - falso rompimento do máximo/mínimo do dia anterior (PDH/PDL)
             com vela de rejeição; stop além do extremo do dia.

Regras de risco:
  - 2% do capital por operação; quantidade = risco USD / (pontos de stop + spread)
    (1 unidade = 1 USD por ponto).
  - Stop entre 25 e 60 pontos (fora disso o trade é ignorado).
  - Metade da posição fecha no alvo 1 (1R) e o stop da outra metade sobe para a
    entrada; a outra metade sai no alvo 2 (2R).
  - Paragem diária: 2 perdas seguidas ou -4% no dia.
  - Só entra nas janelas 14:30-16:30 e 19:30-21:00 (hora de Lisboa) e fecha tudo
    antes do fecho de Nova Iorque.
  - Opcional: bloqueia entradas 15 min antes/depois de notícias de impacto alto.

Os horários são calculados na hora de Nova Iorque (09:30-16:00 ET), por isso
ficam sempre certos mesmo nas semanas em que a diferença para Lisboa muda.

Exemplos:
  python backtest_us100.py --demo
  python backtest_us100.py --csv dados/us100_5m.csv --tz UTC
  python backtest_us100.py --yahoo ^NDX           (precisa de: pip install yfinance)
  python backtest_us100.py --csv dados.csv --capital 5000 --setups orb,falso --grafico

Ferramenta de estudo: resultados passados não garantem resultados futuros.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass
from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd

NY = "America/New_York"
LISBOA = "Europe/Lisbon"

ABERTURA = time(9, 30)          # 14:30 Lisboa (normalmente)
FIM_OR = time(9, 45)            # fim do opening range de 15 min
FECHO = time(16, 0)             # 21:00 Lisboa
ULTIMA_VELA = time(15, 55)      # fecha tudo no fecho desta vela
JANELAS_ET = [(time(9, 45), time(11, 30)), (time(14, 30), time(15, 50))]


# --------------------------------------------------------------------------- #
# Configuração
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    capital: float = 5000.0          # USD (≈ 4.737€ de margem disponível)
    risco_pct: float = 0.02          # 2% por operação
    spread: float = 3.5              # pontos de spread do CFD
    stop_min: float = 25.0           # pontos
    stop_max: float = 60.0           # pontos
    folga_stop: float = 2.0          # pontos além do fundo/topo/extremo
    alvo1_r: float = 1.0             # fecha metade em +1R
    alvo2_r: float = 2.0             # resto em +2R
    perdas_seguidas_max: int = 2
    perda_diaria_max: float = 0.04   # -4% no dia = parar
    qtd_passo: float = 0.1           # a app aceita quantidades com 1 casa decimal
    setups: tuple = ("orb", "vwap", "falso")
    usar_vies: bool = True
    margem_noticias_min: int = 15


# --------------------------------------------------------------------------- #
# Dados
# --------------------------------------------------------------------------- #
NOMES = {
    "open": ("open", "abertura", "o"),
    "high": ("high", "maximo", "máximo", "max", "h"),
    "low": ("low", "minimo", "mínimo", "min", "l"),
    "close": ("close", "fecho", "fechamento", "c", "last"),
    "volume": ("volume", "vol", "v", "tick volume"),
}


def _ler_datas(serie: pd.Series, tz_origem: str) -> pd.DatetimeIndex:
    """Aceita unix (s/ms), ISO com ou sem fuso e o formato da Dukascopy
    ("01.11.2026 14:30:00.000" ou "... GMT+0000")."""
    if pd.api.types.is_numeric_dtype(serie):
        unidade = "ms" if serie.iloc[0] > 1e11 else "s"
        return pd.DatetimeIndex(pd.to_datetime(serie, unit=unidade, utc=True))

    s = serie.astype(str).str.strip()
    exemplo = s.iloc[0]
    if re.match(r"^\d{2}\.\d{2}\.\d{4}", exemplo):           # Dukascopy
        m = re.search(r"GMT([+-]\d{4})$", exemplo)
        base = s.str.replace(r"\s*GMT[+-]\d{4}$", "", regex=True)
        dt = pd.to_datetime(base, format="%d.%m.%Y %H:%M:%S.%f")
        if m:
            sinal = 1 if m.group(1)[0] == "+" else -1
            horas, minutos = int(m.group(1)[1:3]), int(m.group(1)[3:5])
            dt = dt - sinal * pd.Timedelta(hours=horas, minutes=minutos)
            return pd.DatetimeIndex(dt).tz_localize("UTC")
        return pd.DatetimeIndex(dt).tz_localize(tz_origem, ambiguous="NaT",
                                                nonexistent="NaT")

    dt = pd.to_datetime(s, format="mixed", utc=False)
    idx = pd.DatetimeIndex(dt)
    if idx.tz is None:
        return idx.tz_localize(tz_origem, ambiguous="NaT", nonexistent="NaT")
    return idx


def carregar_csv(caminho: str, tz_origem: str = "UTC") -> pd.DataFrame:
    df = pd.read_csv(caminho, sep=None, engine="python")
    df.columns = [str(c).strip() for c in df.columns]
    minusc = {c.lower(): c for c in df.columns}

    col_tempo = next((minusc[k] for k in minusc
                      if any(w in k for w in ("time", "date", "data", "hora"))),
                     df.columns[0])
    out = pd.DataFrame(index=_ler_datas(df[col_tempo], tz_origem))

    for campo, aliases in NOMES.items():
        col = next((minusc[a] for a in aliases if a in minusc), None)
        if col is None:
            if campo == "volume":
                out[campo] = 0.0
                continue
            sys.exit(f"Erro: não encontrei a coluna '{campo}' no CSV "
                     f"(colunas: {list(df.columns)})")
        out[campo] = pd.to_numeric(df[col].values, errors="coerce")

    out = out[out.index.notna()].dropna(subset=["open", "high", "low", "close"])
    return out.sort_index()


def carregar_yahoo(ticker: str) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("Para usar --yahoo instala primeiro: pip install yfinance")
    raw = yf.download(ticker, period="60d", interval="5m", progress=False,
                      auto_adjust=False)
    if raw.empty:
        sys.exit(f"O Yahoo não devolveu dados para {ticker}.")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw.columns = [c.lower() for c in raw.columns]
    df = raw[["open", "high", "low", "close", "volume"]].astype(float)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


def dados_demo(dias: int = 260, semente: int = 7) -> pd.DataFrame:
    """Dados sintéticos (passeio aleatório) só para testar o script."""
    rng = np.random.default_rng(semente)
    datas = pd.bdate_range("2025-11-03", periods=dias)
    blocos, preco = [], 21000.0
    for d in datas:
        idx = pd.date_range(pd.Timestamp(d).tz_localize(NY) + pd.Timedelta(hours=9, minutes=30),
                            periods=78, freq="5min")
        preco *= 1 + rng.normal(0.0003, 0.003)                  # gap
        deriva = rng.normal(0, 0.00008)
        ret = rng.normal(deriva, 0.0012, len(idx))
        fechos = preco * np.cumprod(1 + ret)
        aberturas = np.r_[preco, fechos[:-1]]
        amp = np.abs(rng.normal(0, 0.0008, len(idx))) * fechos
        maximos = np.maximum(aberturas, fechos) + amp
        minimos = np.minimum(aberturas, fechos) - amp
        vol = rng.integers(500, 3000, len(idx)).astype(float)
        vol[:6] *= 3
        blocos.append(pd.DataFrame({"open": aberturas, "high": maximos, "low": minimos,
                                    "close": fechos, "volume": vol}, index=idx))
        preco = fechos[-1]
    return pd.concat(blocos)


def para_5m(df: pd.DataFrame) -> pd.DataFrame:
    passo = df.index.to_series().diff().dropna().median()
    if passo > pd.Timedelta(minutes=5):
        sys.exit(f"Os dados têm velas de {passo}; são precisas velas de 5 min (ou 1 min).")
    if passo < pd.Timedelta(minutes=5):
        df = df.resample("5min", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min",
             "close": "last", "volume": "sum"}).dropna(subset=["close"])
    return df


def preparar(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    df = df.copy()
    df.index = df.index.tz_convert(NY)
    df = para_5m(df)
    t = df.index.time
    df = df[(t >= ABERTURA) & (t < FECHO) & (df.index.dayofweek < 5)].copy()
    df["dia"] = df.index.date

    # Velas sem negociação (preço plano e volume 0, comuns nos CSV da Dukascopy)
    plana = (df["high"] == df["low"]) & (df["volume"] <= 0)
    df = df[~plana].copy()

    # VWAP do dia (se não houver volume, usa peso igual por vela)
    peso = df["volume"].where(df["volume"] > 0, 1.0)
    tipico = (df["high"] + df["low"] + df["close"]) / 3
    df["vwap"] = (tipico * peso).groupby(df["dia"]).cumsum() / peso.groupby(df["dia"]).cumsum()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()

    # Dados diários: PDH/PDL e viés pelas médias de 20 e 50 do diário
    diario = df.groupby("dia").agg(high=("high", "max"), low=("low", "min"),
                                   close=("close", "last"))
    diario["sma20"] = diario["close"].rolling(20).mean()
    diario["sma50"] = diario["close"].rolling(50).mean()
    vies = np.where((diario["close"] > diario["sma20"]) & (diario["sma20"] > diario["sma50"]), 1,
                    np.where((diario["close"] < diario["sma20"]) & (diario["sma20"] < diario["sma50"]), -1, 0))
    vies = pd.Series(vies, index=diario.index).where(diario["sma50"].notna())
    diario["pdh"] = diario["high"].shift(1)
    diario["pdl"] = diario["low"].shift(1)
    diario["vies"] = vies.shift(1)        # viés de hoje = fecho de ontem

    df = df.join(diario[["pdh", "pdl", "vies"]], on="dia")
    return df


def carregar_noticias(caminho: str | None) -> pd.DatetimeIndex:
    """CSV com uma coluna de data/hora (hora de Lisboa) por cada notícia de impacto alto."""
    if not caminho:
        return pd.DatetimeIndex([], tz=NY)
    n = pd.read_csv(caminho)
    return _ler_datas(n.iloc[:, 0], LISBOA).tz_convert(NY)


# --------------------------------------------------------------------------- #
# Motor do backtest
# --------------------------------------------------------------------------- #
def _arredondar_qtd(qtd: float, passo: float) -> float:
    return math.floor(qtd / passo + 1e-9) * passo


def _na_janela(t: time) -> bool:
    return any(ini <= t < fim for ini, fim in JANELAS_ET)


def _sinal(dia: pd.DataFrame, i: int, cfg: Config, estado: dict, vies) -> dict | None:
    v = dia.iloc[i]
    ant = dia.iloc[i - 1]
    ok_long = (not cfg.usar_vies) or vies == 1
    ok_short = (not cfg.usar_vies) or vies == -1
    folga = cfg.folga_stop

    # 1) ORB - só o primeiro rompimento do dia conta
    if "orb" in cfg.setups and not estado["orb_feito"] and estado["or_alto"] is not None:
        if v.close > estado["or_alto"] >= ant.close:
            estado["orb_feito"] = True
            if ok_long:
                return {"setup": "ORB", "dir": 1, "stop": estado["or_meio"]}
        elif v.close < estado["or_baixo"] <= ant.close:
            estado["orb_feito"] = True
            if ok_short:
                return {"setup": "ORB", "dir": -1, "stop": estado["or_meio"]}

    # 3) Falso rompimento de PDH/PDL - uma vez por lado por dia; não vai contra o viés
    if "falso" in cfg.setups and not pd.isna(v.pdh):
        contra_long = cfg.usar_vies and vies == -1
        contra_short = cfg.usar_vies and vies == 1
        if (not estado["falso_pdh"] and not contra_short
                and v.high > v.pdh and v.close < v.pdh and v.close < v.open):
            estado["falso_pdh"] = True
            return {"setup": "FALSO_PDH", "dir": -1, "stop": estado["max_dia"] + folga}
        if (not estado["falso_pdl"] and not contra_long
                and v.low < v.pdl and v.close > v.pdl and v.close > v.open):
            estado["falso_pdl"] = True
            return {"setup": "FALSO_PDL", "dir": 1, "stop": estado["min_dia"] - folga}

    # 2) Pullback ao VWAP / EMA20 em dia de tendência
    if "vwap" in cfg.setups and i >= 6:
        ult = dia.iloc[i - 6:i]
        if ok_long:
            tendencia = (ult["close"] > ult["vwap"]).sum() >= 5
            nivel = max(v.vwap, v.ema20)
            if tendencia and v.low <= nivel and v.close > nivel and v.close > v.open:
                return {"setup": "VWAP", "dir": 1,
                        "stop": dia["low"].iloc[i - 5:i + 1].min() - folga}
        if ok_short:
            tendencia = (ult["close"] < ult["vwap"]).sum() >= 5
            nivel = min(v.vwap, v.ema20)
            if tendencia and v.high >= nivel and v.close < nivel and v.close < v.open:
                return {"setup": "VWAP", "dir": -1,
                        "stop": dia["high"].iloc[i - 5:i + 1].max() + folga}
    return None


def backtest(df: pd.DataFrame, cfg: Config, noticias: pd.DatetimeIndex) -> tuple[pd.DataFrame, dict]:
    capital = cfg.capital
    trades = []
    info = {"dias": 0, "dias_parados": 0, "sem_vies": 0, "ignorados_stop": 0}
    margem = pd.Timedelta(minutes=cfg.margem_noticias_min)

    for d, dia in df.groupby("dia", sort=True):
        if len(dia) < 10:
            continue
        info["dias"] += 1
        vies = dia["vies"].iloc[0]
        if pd.isna(vies):
            info["sem_vies"] += 1
            vies = 0
        capital_inicio = capital
        perdas_seguidas, parado = 0, False
        estado = {"orb_feito": False, "falso_pdh": False, "falso_pdl": False,
                  "or_alto": None, "or_baixo": None, "or_meio": None,
                  "max_dia": -np.inf, "min_dia": np.inf}
        pos = None

        for i in range(len(dia)):
            vela = dia.iloc[i]
            ts = dia.index[i]
            t = ts.time()

            # ---- gerir posição aberta ----
            if pos is not None:
                pos = _gerir(pos, vela, ts + pd.Timedelta(minutes=5), t, trades, cfg)
                if pos is None:
                    capital += trades[-1]["resultado_usd"]
                    perdas_seguidas = perdas_seguidas + 1 if trades[-1]["resultado_usd"] < 0 else 0
                    if (perdas_seguidas >= cfg.perdas_seguidas_max or
                            capital - capital_inicio <= -cfg.perda_diaria_max * capital_inicio):
                        parado = True

            estado["max_dia"] = max(estado["max_dia"], vela.high)
            estado["min_dia"] = min(estado["min_dia"], vela.low)
            if t == time(9, 40):       # fecho da 3.ª vela = opening range completo
                or_ = dia[dia.index.time < FIM_OR]
                estado["or_alto"], estado["or_baixo"] = or_["high"].max(), or_["low"].min()
                estado["or_meio"] = (estado["or_alto"] + estado["or_baixo"]) / 2

            if pos is not None or parado or i == 0 or not _na_janela(t):
                continue
            if len(noticias) and (abs(noticias - (ts + pd.Timedelta(minutes=5))) <= margem).any():
                continue

            sinal = _sinal(dia, i, cfg, estado, vies)
            if sinal is None:
                continue
            entrada = vela.close
            dist = (entrada - sinal["stop"]) * sinal["dir"]
            if not (cfg.stop_min <= dist <= cfg.stop_max):
                info["ignorados_stop"] += 1
                continue
            risco_usd = capital * cfg.risco_pct
            qtd = _arredondar_qtd(risco_usd / (dist + cfg.spread), cfg.qtd_passo)
            if qtd < cfg.qtd_passo:
                continue
            metade = _arredondar_qtd(qtd / 2, cfg.qtd_passo)
            pos = {"dia": d, "setup": sinal["setup"], "dir": sinal["dir"],
                   "hora_entrada": ts + pd.Timedelta(minutes=5),   # fecho da vela de sinal
                   "entrada": entrada, "stop_ini": sinal["stop"], "stop": sinal["stop"],
                   "alvo1": entrada + sinal["dir"] * dist * cfg.alvo1_r,
                   "alvo2": entrada + sinal["dir"] * dist * cfg.alvo2_r,
                   "qtd": qtd, "qtd_aberta": qtd,
                   "qtd_parcial": metade if metade >= cfg.qtd_passo and qtd - metade >= cfg.qtd_passo else 0.0,
                   "parcial_feita": False, "realizado": 0.0, "risco_usd": (dist + cfg.spread) * qtd}

        if pos is not None:          # segurança: não deve acontecer
            _fechar(pos, dia["close"].iloc[-1], dia.index[-1], "fecho do dia", trades, cfg)
            capital += trades[-1]["resultado_usd"]
        if parado:
            info["dias_parados"] += 1

    info["capital_final"] = capital
    return pd.DataFrame(trades), info


def _gerir(pos: dict, v, ts, t, trades: list, cfg: Config) -> dict | None:
    """ts = hora de fecho da vela."""
    d = pos["dir"]
    # Stop primeiro (conservador quando stop e alvo estão na mesma vela)
    if (d == 1 and v.low <= pos["stop"]) or (d == -1 and v.high >= pos["stop"]):
        motivo = "stop na entrada" if pos["parcial_feita"] else "stop"
        _fechar(pos, pos["stop"], ts, motivo, trades, cfg)
        return None
    # Alvo 1: fecha metade e sobe o stop para a entrada
    if not pos["parcial_feita"] and ((d == 1 and v.high >= pos["alvo1"]) or
                                     (d == -1 and v.low <= pos["alvo1"])):
        pos["parcial_feita"] = True
        if pos["qtd_parcial"] > 0:
            q = pos["qtd_parcial"]
            pos["realizado"] += (pos["alvo1"] - pos["entrada"]) * d * q
            pos["qtd_aberta"] -= q
        pos["stop"] = pos["entrada"]
    # Alvo 2
    if (d == 1 and v.high >= pos["alvo2"]) or (d == -1 and v.low <= pos["alvo2"]):
        _fechar(pos, pos["alvo2"], ts, "alvo 2", trades, cfg)
        return None
    # Fecho obrigatório antes do fecho de Nova Iorque
    if t >= ULTIMA_VELA:
        _fechar(pos, v.close, ts, "fecho do dia", trades, cfg)
        return None
    return pos


def _fechar(pos: dict, preco: float, ts, motivo: str, trades: list, cfg: Config) -> None:
    d = pos["dir"]
    bruto = pos["realizado"] + (preco - pos["entrada"]) * d * pos["qtd_aberta"]
    custo = cfg.spread * pos["qtd"]
    res = bruto - custo
    trades.append({
        "data": pos["dia"],
        "setup": pos["setup"],
        "direcao": "Comprar" if d == 1 else "Vender",
        "hora_entrada_lisboa": pos["hora_entrada"].tz_convert(LISBOA).strftime("%H:%M"),
        "entrada": round(pos["entrada"], 1),
        "stop": round(pos["stop_ini"], 1),
        "alvo1": round(pos["alvo1"], 1),
        "alvo2": round(pos["alvo2"], 1),
        "quantidade": round(pos["qtd"], 1),
        "hora_saida_lisboa": pd.Timestamp(ts).tz_convert(LISBOA).strftime("%H:%M"),
        "preco_saida": round(preco, 1),
        "parcial_alvo1": pos["parcial_feita"],
        "motivo_saida": motivo,
        "resultado_usd": round(res, 2),
        "resultado_R": round(res / pos["risco_usd"], 2),
    })


# --------------------------------------------------------------------------- #
# Relatório
# --------------------------------------------------------------------------- #
def _metricas(t: pd.DataFrame) -> dict:
    ganhos = t.loc[t["resultado_usd"] > 0, "resultado_usd"].sum()
    perdas = -t.loc[t["resultado_usd"] < 0, "resultado_usd"].sum()
    return {
        "trades": len(t),
        "acerto_%": round(100 * (t["resultado_usd"] > 0).mean(), 1),
        "R_medio": round(t["resultado_R"].mean(), 2),
        "R_total": round(t["resultado_R"].sum(), 1),
        "profit_factor": round(ganhos / perdas, 2) if perdas > 0 else float("inf"),
        "resultado_usd": round(t["resultado_usd"].sum(), 2),
    }


def relatorio(trades: pd.DataFrame, info: dict, cfg: Config) -> str:
    linhas = ["", "=" * 60, " BACKTEST US100 - resultados", "=" * 60]
    if trades.empty:
        linhas.append("Nenhum trade encontrado com estas regras e dados.")
        return "\n".join(linhas)

    equity = cfg.capital + trades["resultado_usd"].cumsum()
    pico = np.maximum.accumulate(np.r_[cfg.capital, equity.values])[1:]
    dd = ((equity - pico) / pico).min()
    m = _metricas(trades)
    ret = info["capital_final"] / cfg.capital - 1

    linhas += [
        f"Período:            {trades['data'].min()} a {trades['data'].max()} ({info['dias']} dias)",
        f"Capital inicial:    {cfg.capital:,.2f} USD   |   final: {info['capital_final']:,.2f} USD ({ret:+.1%})",
        f"Trades:             {m['trades']}   |   acerto: {m['acerto_%']}%",
        f"R médio por trade:  {m['R_medio']:+.2f} R   |   R total: {m['R_total']:+.1f} R",
        f"Profit factor:      {m['profit_factor']}",
        f"Drawdown máximo:    {dd:.1%}",
        f"Dias com paragem (2 perdas ou -4%): {info['dias_parados']}",
        f"Sinais ignorados por stop fora de {cfg.stop_min:.0f}-{cfg.stop_max:.0f} pts: {info['ignorados_stop']}",
    ]
    if cfg.usar_vies and info["sem_vies"]:
        linhas.append(f"Dias sem viés (menos de 50 dias de histórico): {info['sem_vies']} "
                      "- nesses dias só entram falsos rompimentos")
    if m["trades"] < 30:
        linhas.append("AVISO: menos de 30 trades - a amostra é pequena e não prova nada.")

    linhas += ["", "Por setup:"]
    tab = pd.DataFrame({s: _metricas(g) for s, g in trades.groupby("setup")}).T
    linhas.append(tab.to_string())
    linhas += ["", "Por direção:"]
    tab = pd.DataFrame({s: _metricas(g) for s, g in trades.groupby("direcao")}).T
    linhas.append(tab.to_string())
    linhas += ["", "Saídas:", trades["motivo_saida"].value_counts().to_string(), ""]
    return "\n".join(linhas)


def grafico(trades: pd.DataFrame, cfg: Config, caminho: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    eq = cfg.capital + trades["resultado_usd"].cumsum()
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(range(1, len(eq) + 1), eq.values, color="#2a6fdb", linewidth=1.8)
    ax.axhline(cfg.capital, color="#999999", linestyle="--", linewidth=1)
    ax.set_title("Curva de capital - backtest US100")
    ax.set_xlabel("Trade n.º")
    ax.set_ylabel("Capital (USD)")
    ax.grid(alpha=0.3)
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)
    fig.tight_layout()
    fig.savefig(caminho, dpi=130)
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Linha de comandos
# --------------------------------------------------------------------------- #
def main() -> None:
    p = argparse.ArgumentParser(description="Backtest dos setups ORB, VWAP e falso rompimento no US100 (5 min).")
    fonte = p.add_mutually_exclusive_group(required=True)
    fonte.add_argument("--csv", help="ficheiro CSV com velas de 5 min (ou 1 min)")
    fonte.add_argument("--yahoo", help="ticker do Yahoo, ex.: ^NDX (só os últimos 60 dias)")
    fonte.add_argument("--demo", action="store_true", help="usa dados sintéticos para testar")
    p.add_argument("--tz", default="UTC", help="fuso horário das datas do CSV sem fuso (padrão: UTC)")
    p.add_argument("--capital", type=float, default=5000.0, help="capital inicial em USD (padrão: 5000)")
    p.add_argument("--risco", type=float, default=2.0, help="risco por trade em %% (padrão: 2)")
    p.add_argument("--spread", type=float, default=3.5, help="spread em pontos (padrão: 3.5)")
    p.add_argument("--stop-min", type=float, default=25.0)
    p.add_argument("--stop-max", type=float, default=60.0)
    p.add_argument("--alvo1", type=float, default=1.0, help="alvo 1 em R (fecha metade)")
    p.add_argument("--alvo2", type=float, default=2.0, help="alvo 2 em R")
    p.add_argument("--setups", default="orb,vwap,falso", help="lista: orb,vwap,falso")
    p.add_argument("--sem-vies", action="store_true", help="ignora o viés das médias 20/50 do diário")
    p.add_argument("--noticias", help="CSV com datas/horas (Lisboa) de notícias de impacto alto")
    p.add_argument("--saida", default="trades_backtest.csv", help="CSV com a lista de trades")
    p.add_argument("--grafico", nargs="?", const="curva_capital.png",
                   help="grava a curva de capital em PNG")
    a = p.parse_args()

    cfg = Config(capital=a.capital, risco_pct=a.risco / 100, spread=a.spread,
                 stop_min=a.stop_min, stop_max=a.stop_max, alvo1_r=a.alvo1, alvo2_r=a.alvo2,
                 setups=tuple(s.strip().lower() for s in a.setups.split(",") if s.strip()),
                 usar_vies=not a.sem_vies)

    if a.demo:
        bruto = dados_demo()
        print("A usar dados SINTÉTICOS (--demo): os resultados não dizem nada sobre o mercado real.")
    elif a.yahoo:
        bruto = carregar_yahoo(a.yahoo)
    else:
        if not Path(a.csv).exists():
            sys.exit(f"Ficheiro não encontrado: {a.csv}")
        bruto = carregar_csv(a.csv, a.tz)

    df = preparar(bruto, cfg)
    if df.empty:
        sys.exit("Não há velas dentro do horário de Nova Iorque (09:30-16:00 ET). Confirma o --tz.")

    trades, info = backtest(df, cfg, carregar_noticias(a.noticias))
    print(relatorio(trades, info, cfg))

    if not trades.empty:
        trades.to_csv(a.saida, index=False)
        print(f"Lista de trades gravada em: {a.saida}")
        if a.grafico:
            grafico(trades, cfg, a.grafico)
            print(f"Curva de capital gravada em: {a.grafico}")


if __name__ == "__main__":
    main()
