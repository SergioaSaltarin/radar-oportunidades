# -*- coding: utf-8 -*-
"""
RADAR DE OPORTUNIDADES — escaner diario del mercado (GitHub Actions, al cierre).

Universo: S&P 500 + Nasdaq-100 + ADRs latinoamericanos liquidos (~540 acciones).

Reglas tecnicas (deteccion de "esta despertando"):
  A) LIDER EN RETROCESO — momentum 6m en el 10% superior, tendencia sana
     (precio > media 50 > media 200) y volvio a su media de 20 dias tras haber
     estado >= 8% por encima en el ultimo mes. Comprar la pausa de un lider.
  B) RUPTURA CON VOLUMEN — maximo de 52 semanas con volumen >= 1.5x el promedio.
  C) DESPERTAR — +5% en el dia con volumen >= 3x y precio sobre la media de 200.

Filtro de CALIDAD (para que nunca lleguen chicharros, criptos ni cohetes sin respaldo):
  - capitalizacion >= $10.000 millones
  - empresa rentable (utilidad por accion positiva)
  - ventas no cayendo (crecimiento de ingresos >= 0 cuando hay dato)
  - no "extendida": menos de +150% en 6 meses (eso es hype, no tendencia)

Mercado: detecta caidas fuertes del S&P (>= -2% dia) y rebotes confirmados
(cayo >= 3% desde su maximo de 5 dias y recupero >= 1.5% desde el minimo).

Salida: notificacion ntfy + candidatos.json (lo lee el analista automatico de Claude).
Maximo 5 candidatas por dia, sin repetir ticker en 14 dias. Herramienta de analisis.
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

NTFY_TOPIC = "spce-radar-sergio-7k3m9x"
BASE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE, "scanner_state.json")
OUT_PATH = os.path.join(BASE, "candidatos.json")
NY = ZoneInfo("America/New_York")
MAX_AVISOS = 5
DIAS_SIN_REPETIR = 14
MIN_CAP = 10e9
MAX_R6M = 1.50

ADRS_LATAM = ["NU", "MELI", "GGAL", "PAM", "YPF", "VIST", "BBAR", "CIB", "EC",
              "PBR", "VALE", "ITUB", "BBD", "STNE", "PAGS", "AMX", "FMX", "GLOB", "DLO"]
FALLBACK = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "BRK-B", "LLY",
            "JPM", "V", "MA", "UNH", "XOM", "COST", "HD", "PG", "NFLX", "JNJ", "ABBV", "BAC",
            "CRM", "ORCL", "CVX", "KO", "AMD", "WMT", "MRK", "PEP", "ADBE", "TMO", "CSCO",
            "ACN", "LIN", "MCD", "ABT", "WFC", "GE", "IBM", "TXN", "QCOM", "PM", "ISRG",
            "CAT", "DIS", "NOW", "INTU", "AMGN", "GS", "UBER", "SPGI", "BKNG", "AXP", "NEE",
            "PFE", "RTX", "LOW", "HON", "AMAT", "UNP", "BLK", "SYK", "ETN", "TJX", "PGR",
            "ANET", "PANW", "MU", "LRCX", "KLAC", "ADI", "PLTR", "VRTX", "SCHW", "BSX", "C",
            "MDT", "ADP", "GILD", "REGN", "SBUX", "CB", "DE", "BA", "TTWO", "EA",
            "SHOP", "CRWD", "SNOW", "DDOG", "ABNB", "COIN", "MRVL", "SMCI", "ARM", "ASML",
            "TSM", "BABA", "PDD", "JD", "SE", "SPOT", "RBLX", "HOOD", "SOFI", "RKLB", "ASTS"]


def notify(title, body, urgent=False):
    try:
        requests.post("https://ntfy.sh", json={"topic": NTFY_TOPIC, "title": title,
                                                "message": body, "priority": 4 if urgent else 3},
                      timeout=15)
    except Exception as e:
        print(f"(fallo ntfy: {e})")
    try:
        print(f"NOTIFICACION: {title}\n{body}\n")
    except UnicodeEncodeError:      # consolas Windows sin UTF-8
        enc = lambda s: s.encode("ascii", "replace").decode()
        print(f"NOTIFICACION: {enc(title)}\n{enc(body)}\n")


def _tablas(url):
    from io import StringIO
    html = requests.get(url, timeout=30,
                        headers={"User-Agent": "Mozilla/5.0 (radar-cartera; contacto: github)"}).text
    return pd.read_html(StringIO(html))


def universo():
    tickers = set(ADRS_LATAM) | set(FALLBACK)
    try:
        sp = _tablas("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]
        tickers |= set(sp["Symbol"].astype(str).str.replace(".", "-", regex=False))
    except Exception as e:
        print(f"(sin lista S&P de Wikipedia: {e})")
    try:
        for t in _tablas("https://en.wikipedia.org/wiki/Nasdaq-100"):
            col = next((c for c in t.columns if str(c).lower() in ("ticker", "symbol")), None)
            if col is not None:
                tickers |= set(t[col].astype(str))
                break
    except Exception as e:
        print(f"(sin lista Nasdaq-100: {e})")
    return sorted(t for t in tickers if t and t.isascii() and len(t) <= 6)


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            return json.load(open(STATE_PATH, encoding="utf-8"))
        except Exception:
            pass
    return {"alerted": {}, "weekly_date": None, "mercado": {}}


def calidad(t):
    """Devuelve (pasa, dict_datos). Usa yfinance .info solo para candidatas (pocas)."""
    try:
        info = yf.Ticker(t).info or {}
    except Exception:
        info = {}
    cap = info.get("marketCap") or 0
    eps = info.get("trailingEps")
    rev_g = info.get("revenueGrowth")
    d = {
        "nombre": info.get("shortName") or t,
        "sector": info.get("sector") or "?",
        "cap_bn": round(cap / 1e9, 1) if cap else None,
        "pe": info.get("trailingPE"),
        "pe_forward": info.get("forwardPE"),
        "eps": eps,
        "crec_ventas_pct": round(rev_g * 100, 1) if rev_g is not None else None,
        "margen_pct": round(info["profitMargins"] * 100, 1) if info.get("profitMargins") is not None else None,
    }
    # dato faltante = desconocido (no descarta); dato presente y malo = descarta
    pasa = (not cap or cap >= MIN_CAP) and (eps is None or eps > 0) and (rev_g is None or rev_g >= 0)
    motivo = []
    if cap and cap < MIN_CAP:
        motivo.append(f"cap ${cap/1e9:.1f}bn < 10bn")
    if eps is not None and eps <= 0:
        motivo.append("no rentable")
    if rev_g is not None and rev_g < 0:
        motivo.append(f"ventas cayendo {rev_g*100:.0f}%")
    d["filtro"] = "OK" if pasa else "; ".join(motivo)
    return pasa, d


def mercado(st, today, spx):
    """Caidas fuertes y rebotes confirmados del S&P 500."""
    if len(spx) < 10:
        return None
    r1d = float(spx.iloc[-1] / spx.iloc[-2] - 1) * 100
    hi5 = float(spx.tail(6).iloc[:-1].max())
    lo = float(spx.tail(5).min())
    caida_desde_hi = (float(spx.iloc[-1]) / hi5 - 1) * 100
    rebote = (float(spx.iloc[-1]) / lo - 1) * 100
    m = st.setdefault("mercado", {})
    nota = None
    if r1d <= -2 and m.get("caida") != today:
        m["caida"] = today
        nota = (f"📉 El mercado cayo {r1d:+.1f}% hoy (S&P {float(spx.iloc[-1]):.0f}). "
                f"Caidas de 2-3% en un dia pasan varias veces al ano y la mayoria se "
                f"recuperan en semanas; solo son el inicio de algo grande cuando vienen "
                f"con ruptura de la media de 200 dias. Contexto, no orden.")
        notify("📉 Dia rojo en el mercado", nota, urgent=True)
    elif hi5 and (lo / hi5 - 1) <= -0.03 and rebote >= 1.5 and m.get("rebote") != today \
            and (m.get("caida") or "") >= (datetime.now(NY).strftime("%Y-%m-") ):
        m["rebote"] = today
        nota = (f"🟢 Rebote del mercado confirmado: el S&P cayo {(lo/hi5-1)*100:.1f}% desde su "
                f"maximo de 5 dias y ya recupero {rebote:+.1f}% desde el minimo. "
                f"Historicamente, entrar tras el giro confirmado tiene mejor relacion "
                f"riesgo/retorno que comprar la caida a ciegas.")
        notify("🟢 Rebote del mercado", nota)
    return nota


def main():
    now = datetime.now(NY)
    today = now.strftime("%Y-%m-%d")
    st = load_state()
    tick = universo()
    print(f"Universo: {len(tick)} tickers")
    data = yf.download(tick + ["^GSPC"], period="400d", interval="1d", auto_adjust=True,
                       progress=False, group_by="column", threads=True)
    close_all, vol_all = data["Close"], data["Volume"]
    spx = close_all["^GSPC"].dropna()
    close = close_all.drop(columns=["^GSPC"]).dropna(axis=1, thresh=230)
    vol = vol_all[close.columns]
    px = close.iloc[-1]
    ok = px[px >= 5].index
    close, vol = close[ok], vol[ok]
    print(f"Con datos suficientes y precio >= $5: {len(ok)}")

    sma20, sma50, sma200 = (close.rolling(n).mean() for n in (20, 50, 200))
    r6m = close.iloc[-1] / close.iloc[-126] - 1
    r1d = close.iloc[-1] / close.iloc[-2] - 1
    hi52 = close.tail(252).max()
    vol20 = vol.rolling(20).mean().iloc[-2]
    vol_hoy = vol.iloc[-1]
    dist20_max = (close.tail(21) / sma20.tail(21) - 1).max()
    dist20_hoy = close.iloc[-1] / sma20.iloc[-1] - 1
    top_decil = r6m.quantile(0.90)

    brutas = []
    for t in close.columns:
        try:
            p, s20, s50, s200 = px[t], sma20.iloc[-1][t], sma50.iloc[-1][t], sma200.iloc[-1][t]
            if any(np.isnan(x) for x in (p, s20, s50, s200)) or r6m[t] > MAX_R6M:
                continue
            tendencia = p > s50 > s200
            if (r6m[t] >= top_decil and tendencia and dist20_max[t] >= 0.08
                    and -0.02 <= dist20_hoy[t] <= 0.02):
                brutas.append((t, "A", "Lider en retroceso", r6m[t]))
            elif p >= hi52[t] * 0.999 and vol_hoy[t] >= 1.5 * vol20[t] and tendencia:
                brutas.append((t, "B", "Ruptura 52 semanas con volumen", r6m[t]))
            elif r1d[t] >= 0.05 and vol_hoy[t] >= 3 * vol20[t] and p > s200:
                brutas.append((t, "C", "Despertar con volumen", r6m[t]))
        except Exception:
            continue

    def reciente(t):
        d = st["alerted"].get(t)
        return d and (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(d, "%Y-%m-%d")).days < DIAS_SIN_REPETIR
    prioridad = {"A": 0, "B": 1, "C": 2}
    brutas = [s for s in brutas if not reciente(s[0])]
    brutas.sort(key=lambda s: (prioridad[s[1]], -s[3]))
    print(f"Senales tecnicas: {len(brutas)}")

    # filtro de calidad sobre las mejores (hasta 12 revisadas, 5 elegidas)
    elegidas, descartadas = [], []
    for t, tipo, etiqueta, r in brutas[:12]:
        pasa, d = calidad(t)
        fila = {"ticker": t, "tipo": tipo, "regla": etiqueta, "precio": round(float(px[t]), 2),
                "ret_6m_pct": round(float(r) * 100, 1),
                "dist_max52_pct": round((float(px[t]) / float(hi52[t]) - 1) * 100, 1),
                "vol_vs_prom": round(float(vol_hoy[t] / vol20[t]), 1) if vol20[t] else None,
                "dia_pct": round(float(r1d[t]) * 100, 1), **d}
        try:
            apt = yf.Ticker(t).analyst_price_targets or {}
            if apt.get("mean"):
                fila["objetivo_prom"] = round(apt["mean"], 1)
                fila["upside_pct"] = round((apt["mean"] / float(px[t]) - 1) * 100, 1)
        except Exception:
            pass
        (elegidas if pasa else descartadas).append(fila)
        if len(elegidas) >= MAX_AVISOS:
            break
    print(f"Pasan calidad: {len(elegidas)} | descartadas: {[(d['ticker'], d['filtro']) for d in descartadas]}")

    # ---- RANKING PERMANENTE: puntaje compuesto sobre el universo ----
    # Responde "cual es la mejor" de forma estable, no por evento del dia.
    # Factores con evidencia academica: momentum (12m menos el ultimo mes),
    # tendencia, calidad (margen, crecimiento, rentabilidad) y valoracion.
    ranking = []
    r12 = close.iloc[-1] / close.iloc[-252] - 1
    r12_1 = close.iloc[-21] / close.iloc[-252] - 1     # momentum sin el ultimo mes
    universo_mom = r12_1.dropna()
    for t in close.columns:
        try:
            p, s50, s200 = px[t], sma50.iloc[-1][t], sma200.iloc[-1][t]
            if np.isnan(p) or np.isnan(s200) or r6m[t] > MAX_R6M:
                continue
            if p < s200:                       # solo tendencia primaria alcista
                continue
            pct_mom = float((universo_mom < r12_1[t]).mean())   # percentil 0-1
            score = 40 * pct_mom + (10 if p > s50 else 0)
            ranking.append((t, score, float(r12[t]) * 100))
        except Exception:
            continue
    ranking.sort(key=lambda x: -x[1])
    top_tec = [t for t, _, _ in ranking[:25]]
    scored = []
    for t in top_tec:
        pasa, d = calidad(t)
        if not pasa or not d.get("cap_bn"):
            continue
        s = next(s for tt, s, _ in ranking if tt == t)
        crec = d.get("crec_ventas_pct") or 0
        marg = d.get("margen_pct") or 0
        s += min(20, crec / 2) + min(15, marg / 3)          # calidad
        pe_f = d.get("pe_forward")
        if pe_f and 0 < pe_f < 60:
            s += max(0, 15 - pe_f / 4)                       # valoracion
        try:
            apt = yf.Ticker(t).analyst_price_targets or {}
            if apt.get("mean"):
                up = (apt["mean"] / float(px[t]) - 1) * 100
                d["objetivo_prom"], d["upside_pct"] = round(apt["mean"], 1), round(up, 1)
                s += max(-10, min(20, up / 2))               # upside del consenso
        except Exception:
            pass
        veces = st.setdefault("historial_rank", {}).get(t, 0)
        d.update(ticker=t, score=round(s, 1), precio=round(float(px[t]), 2),
                 ret_12m_pct=round(float(r12[t]) * 100, 1), veces_en_top=veces + 1)
        scored.append(d)
        st["historial_rank"][t] = veces + 1
        if len(scored) >= 8:
            break
    scored.sort(key=lambda d: -d["score"])

    nota_mercado = mercado(st, today, spx)

    if elegidas:
        lineas = []
        for f in elegidas:
            extra = f" Objetivo analistas ${f['objetivo_prom']} ({f['upside_pct']:+.0f}%)." if f.get("objetivo_prom") else ""
            lineas.append(f"[{f['tipo']}] {f['ticker']} ({f['nombre']}, {f['sector']}): {f['regla']}. "
                          f"${f['precio']}, {f['ret_6m_pct']:+.0f}% en 6m, a {f['dist_max52_pct']:+.0f}% del max 52s. "
                          f"Cap ${f['cap_bn']}bn, P/E {f['pe'] and round(f['pe'])}, ventas {f['crec_ventas_pct']}%.{extra}")
            st["alerted"][f["ticker"]] = today
        notify("🔎 Radar de oportunidades (pasaron el filtro de calidad)",
               "\n\n".join(lineas) + "\n\nEl analista automatico de Claude revisara estas "
               "candidatas esta tarde y te mandara su analisis.")

    top10 = []
    if now.weekday() == 0 and st.get("weekly_date") != today:
        st["weekly_date"] = today
        top = r6m.dropna().sort_values(ascending=False).head(10)
        top10 = [{"ticker": t, "ret_6m_pct": round(v * 100, 1), "precio": round(float(px[t]), 2)}
                 for t, v in top.items()]
        notify("🏆 Top-10 momentum del universo (6 meses)",
               "\n".join(f"{d['ticker']}: +{d['ret_6m_pct']:.0f}% 6m (${d['precio']})" for d in top10))

    json.dump({"fecha": today, "generado_utc": datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M"),
               "candidatas": elegidas, "descartadas_por_calidad": descartadas,
               "ranking_permanente": scored, "mercado": nota_mercado,
               "top10_momentum": top10, "spx_cierre": round(float(spx.iloc[-1]), 2)},
              open(OUT_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(st, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("OK", today)


if __name__ == "__main__":
    main()
