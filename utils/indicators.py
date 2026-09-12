"""
Indicadores técnicos usados por la estrategia.
Todas las funciones reciben/devuelven pandas.Series indexadas 0..n-1
en el mismo orden que las velas (0 = más antigua, -1 = más reciente).
"""

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    """Media móvil simple (usada para la MA20)."""
    return series.rolling(window=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Media móvil exponencial (usada para EMA50 / EMA200)."""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int) -> pd.Series:
    """RSI clásico (suavizado de Wilder) sobre la serie de cierres."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_val = 100 - (100 / (1 + rs))
    rsi_val = rsi_val.fillna(100)
    return rsi_val


def williams_r(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Williams %R. Devuelve valores en el rango [-100, 0]."""
    highest_high = high.rolling(window=period).max()
    lowest_low = low.rolling(window=period).min()
    denom = (highest_high - lowest_low).replace(0, np.nan)
    wr = -100 * (highest_high - close) / denom
    return wr.fillna(0)
