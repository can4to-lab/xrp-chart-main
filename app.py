import streamlit as pd
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from datetime import datetime, timedelta

# Web Sayfası Ayarları (Geniş Ekran Modu)
st.set_page_config(layout="wide", page_title="Teknik Analiz Paneli")

# Web Sitesi Başlıkları
st.title("📊 Gelişmiş Teknik Analiz & Mum Grafiği Paneli")
st.markdown("---")

# Yan Menü (Sidebar) - Kullanıcı Etkileşimi İçin
st.sidebar.header("Grafik Ayarları")
gun_sayisi = st.sidebar.slider("Görüntülenecek Gün Sayısı", min_value=30, max_value=120, value=60)

# 1. Web Sitesi İçin Sahte Mum Verileri Üretelim
@st.cache_data # Sayfa her yenilendiğinde verinin değişmemesi için sabitleme
def veri_ureti(gun):
    tarih_listesi = [datetime.now() - timedelta(days=i) for i in range(gun)]
    tarih_listesi.reverse()
    
    np.random.seed(42)
    acilis = np.random.uniform(100, 150, gun)
    kapanis = acilis + np.random.uniform(-12, 12, gun)
    yuksek = np.maximum(acilis, kapanis) + np.random.uniform(1, 7, gun)
    dusuk = np.minimum(acilis, kapanis) - np.random.uniform(1, 7, gun)
    
    df = pd.DataFrame({
        'Tarih': tarih_listesi,
        'Open': acilis,
        'High': yuksek,
        'Low': dusuk,
        'Close': kapanis
    })
    
    # Hareketli Ortalamaları Hesaplama
    df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['EMA40'] = df['Close'].ewm(span=40, adjust=False).mean()
    df['SMA50'] = df['Close'].rolling(window=50).mean()
    df['SMA150'] = df['Close'].rolling(window=150).mean()
    
    return df

df = veri_ureti(gun_sayisi)

# 2. Plotly ile Grafik Oluşturma
fig = go.Figure()

# Mum Grafiği Ekleme
fig.add_trace(go.Candlestick(
    x=df['Tarih'],
    open=df['Open'], high=df['High'], low=df['Low'], close=df['Close'],
    name="Fiyat (Mum)",
    increasing_line_color='#26a69a', # Profesyonel TradingView yeşili
    decreasing_line_color='#ef5350'  # Profesyonel TradingView kırmızısı
))

# Hareketli Ortalamaları Çizgilere Ekleme
fig.add_trace(go.Scatter(x=df['Tarih'], y=df['EMA20'], name='20 EMA', line=dict(color='green', width=1.5)))
fig.add_trace(go.Scatter(x=df['Tarih'], y=df['EMA40'], name='40 EMA', line=dict(color='blue', width=1.5)))
fig.add_trace(go.Scatter(x=df['Tarih'], y=df['SMA50'], name='50 SMA', line=dict(color='orange', width=1.5)))

# Grafik Görsel Tasarım Ayarları (Karanlık Tema/TradingView Esintisi)
fig.update_layout(
    template="plotly_dark",
    xaxis_rangeslider_visible=False,
    height=650,
    margin=dict(l=10, r=10, t=30, b=10),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
)

# 3. Grafiği Web Sitesine Basma
st.plotly_chart(fig, use_container_width=True)

# Alt Kısma Bilgi Kartları Ekleme
col1, col2, col3 = st.columns(3)
with col1:
    st.metric(label="Son Kapanış Fiyatı", value=f"${df['Close'].iloc[-1]:.2f}")
with col2:
    trend_durumu = "🟢 Boğa (Pozitif)" if df['EMA20'].iloc[-1] > df['EMA40'].iloc[-1] else "🔴 Ayı (Negatif)"
    st.metric(label="Kısa Vadeli Trend (20/40 EMA)", value=trend_durumu)
with col3:
    st.metric(label="Veri Seti Boyutu", value=f"{gun_sayisi} Mum")
