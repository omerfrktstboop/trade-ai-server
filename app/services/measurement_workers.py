"""Ölçüm hattı arka plan worker'ları (Faz 0).

Bu modül mevcut, döngüsüz ``run_once`` fonksiyonlarını (outcome labeling ve
measurement reconciliation) süreç-ömrü boyunca periyodik çalışan worker'lara
bağlar. Fonksiyonların kendisi burada tanımlanmaz — yalnızca zamanlanır.

Üçüncü ölçüm parçası olan market observation collector zaten scanner tick'inde
çağrıldığı için (``scanner.py``) burada tekrar başlatılmaz.

Her iki worker da salt-okuma/ölçüm işidir; emir gönderemez, trade akışına
dokunamaz. ``config.py``'deki ``*_enabled`` bayraklarıyla açılıp kapanır.
"""

from __future__ import annotations

from app.config import settings
from app.services.measurement_reconciliation import run_once as run_reconciliation_once
from app.services.outcome_labeler import run_once as run_outcome_labeler_once
from app.services.periodic_worker import PeriodicWorker

outcome_labeler_worker = PeriodicWorker(
    name="outcome-labeler",
    run_once=run_outcome_labeler_once,
    interval_seconds=settings.outcome_labeler_interval_seconds,
)

measurement_reconciliation_worker = PeriodicWorker(
    name="measurement-reconciliation",
    run_once=run_reconciliation_once,
    interval_seconds=settings.measurement_reconciliation_interval_seconds,
)
