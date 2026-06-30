"""
data/feed.py - Atlantis Veri Besleme Modülü

Piyasa verilerini çeker, önbelleğe alır ve strateji motoruna besler.
Şu an için veri çekme işlemi doğrudan AtlantisStrategyRunner içinde
yapılmaktadır. Gelecekte bu modül aktif hale getirilecektir.
"""
import logging

logger = logging.getLogger(__name__)

__all__ = []