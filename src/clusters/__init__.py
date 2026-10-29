"""Кластерный фильтр входа «Кластеры» на событиях ClusterSource.

ClusterSource публикует в Redis Stream ``cluster.events`` события ленты сделок
Мосбиржи (крупный агрессор, всплеск объёма, лестница дисбалансов, поглощение)
и события качества данных. Робот сохраняет их в ``cluster-events.sqlite3``
(:mod:`src.clusters.store`, :mod:`src.clusters.consumer`), по 1m свечам
считает вердикт для кандидата нового входа (:mod:`src.clusters.verdict`,
:mod:`src.clusters.gate`) и отдаёт отчёт по REST (:mod:`src.clusters.report`).
"""
