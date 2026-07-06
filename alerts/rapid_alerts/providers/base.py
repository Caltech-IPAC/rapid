"""
Data-access interface for alert assembly.

assemble.py only ever calls these methods, so switching storage backends
(operations database, pipeline files on disk, sqlite, ...) means writing a
new provider subclass -- the schema registry, builders, and assembly logic
do not change.
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from ..records import Detection, ObjectRecord, ForcedPhot, Cutouts


class AlertDataProvider(ABC):

    @abstractmethod
    def get_detection(self, sid) -> Detection:
        """Return the triggering detection. Raises ValueError if not found."""

    @abstractmethod
    def get_object_for_source(self, detection) -> Optional[ObjectRecord]:
        """Return the associated persistent object, or None if unassociated."""

    @abstractmethod
    def get_prv_detections(self, detection, obj,
                           window_days=365.25) -> List[Detection]:
        """Return prior detections of obj within window_days before the
        triggering detection, oldest first, excluding the trigger itself."""

    @abstractmethod
    def get_forced_photometry(self, detection, obj) -> List[ForcedPhot]:
        """Return forced-photometry history at the object position."""

    @abstractmethod
    def get_cutouts(self, detection) -> Cutouts:
        """Return image stamps for the detection (members None if missing)."""

    def iter_detections(self, job_or_visit):
        """Yield all Detections for one processing unit (batch alert
        production, as in roman_rapid_alerts). Optional per backend."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support batch iteration")
