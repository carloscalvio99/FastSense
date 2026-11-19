"""ModelLoader: descarga model.pkl desde S3, lo carga con joblib+mmap y lo
recarga en caliente cuando aparece una nueva versión (sin reiniciar el pod).

El artefacto es un SVD de scikit-surprise. Para servir en <400 ms no se usa
`model.predict()` fila a fila: se extraen los factores y se calcula el score
de los 5.000 ítems con una multiplicación matricial de NumPy.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import joblib
import numpy as np

from app.aws import s3_client
from app.config import settings
from app.logging_utils import get_logger

logger = get_logger("fastsense.model_loader")


class ModelNotLoaded(RuntimeError):
    pass


class UnknownUser(KeyError):
    pass


class ModelManifestUnavailable(RuntimeError):
    pass


@dataclass
class ModelSnapshot:
    version: str
    rmse: float
    precision_at_10: float
    trained_at: str
    s3_uri: str
    loaded_at: float
    global_mean: float
    user_factors: np.ndarray
    item_factors: np.ndarray
    user_biases: np.ndarray
    item_biases: np.ndarray
    user_index: dict[str, int]
    item_ids: np.ndarray
    item_index: dict[str, int]
    seen_items: dict[int, np.ndarray] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def n_users(self) -> int:
        return int(self.user_factors.shape[0])

    @property
    def n_items(self) -> int:
        return int(self.item_factors.shape[0])

    def predict(self, user_id: str, limit: int = 10) -> list[tuple[str, float]]:
        inner = self.user_index.get(str(user_id))
        if inner is None:
            raise UnknownUser(user_id)
        if inner >= self.user_factors.shape[0]:
            raise UnknownUser(user_id)

        scores = (
            self.global_mean
            + self.user_biases[inner]
            + self.item_biases
            + self.item_factors @ self.user_factors[inner]
        ).astype(np.float64, copy=True)

        base_scores = scores.copy()
        seen = self.seen_items.get(inner)
        if seen is not None and seen.size:
            scores[seen] = -np.inf
        if np.isfinite(scores).sum() < min(limit, self.n_items):
            # Usuario con casi todo el catálogo consumido: no excluir vistos.
            scores = base_scores

        limit = max(1, min(int(limit), self.n_items))
        if limit == self.n_items:
            order = np.argsort(scores)[::-1]
        else:
            candidates = np.argpartition(scores, -limit)[-limit:]
            order = candidates[np.argsort(scores[candidates])[::-1]]

        results: list[tuple[str, float]] = []
        for index in order[:limit]:
            raw_score = float(scores[index])
            normalized = max(0.0, min(1.0, (raw_score - 1.0) / 4.0))
            results.append((str(self.item_ids[index]), normalized))
        return results


class ModelLoader:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshot: Optional[ModelSnapshot] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._manifest_missing_logged = False
        self._last_error: Optional[str] = None
        os.makedirs(settings.model_dir, exist_ok=True)

    # ------------------------------------------------------------------
    @property
    def model_loaded(self) -> bool:
        with self._lock:
            return self._snapshot is not None

    @property
    def version(self) -> str:
        with self._lock:
            return self._snapshot.version if self._snapshot else "unavailable"

    @property
    def snapshot(self) -> Optional[ModelSnapshot]:
        with self._lock:
            return self._snapshot

    def status(self) -> dict[str, Any]:
        with self._lock:
            snap = self._snapshot
            if snap is None:
                return {
                    "model_loaded": False,
                    "version": "unavailable",
                    "rmse": None,
                    "precision_at_10": None,
                    "trained_at": None,
                    "s3_uri": None,
                    "last_error": self._last_error,
                    "n_users": 0,
                    "n_items": 0,
                }
            return {
                "model_loaded": True,
                "version": snap.version,
                "rmse": snap.rmse,
                "precision_at_10": snap.precision_at_10,
                "trained_at": snap.trained_at,
                "s3_uri": snap.s3_uri,
                "loaded_at": snap.loaded_at,
                "n_users": snap.n_users,
                "n_items": snap.n_items,
                "last_error": self._last_error,
            }

    # ------------------------------------------------------------------
    def _read_manifest(self) -> dict[str, Any]:
        response = s3_client().get_object(
            Bucket=settings.s3_bucket, Key=settings.s3_manifest_key
        )
        return json.loads(response["Body"].read().decode("utf-8"))

    def _download_artifact(self, manifest: dict[str, Any]) -> str:
        model_key = manifest.get("model_key") or settings.s3_model_key
        fd, path = tempfile.mkstemp(prefix="model-", suffix=".pkl", dir=settings.model_dir)
        os.close(fd)
        try:
            s3_client().download_file(settings.s3_bucket, model_key, path)
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return path

    @staticmethod
    def _load_surprise_model(path: str):
        # mmap reduce el RSS cuando el artefacto no está comprimido. Con
        # compresión zlib joblib no puede mapear y se hace load normal.
        try:
            return joblib.load(path, mmap_mode="r")
        except Exception:
            return joblib.load(path)

    def _build_snapshot(self, model, manifest: dict[str, Any]) -> ModelSnapshot:
        if not hasattr(model, "qi") or not hasattr(model, "pu"):
            raise TypeError("El artefacto no es un modelo SVD de scikit-surprise")

        trainset = model.trainset
        item_ids = np.asarray(
            [str(trainset.to_raw_iid(inner)) for inner in range(trainset.n_items)],
            dtype=object,
        )
        item_index = {item_id: index for index, item_id in enumerate(item_ids)}
        user_index = {
            str(raw_id): int(inner)
            for raw_id, inner in trainset._raw2inner_id_users.items()  # noqa: SLF001
        }
        seen_items: dict[int, np.ndarray] = {}
        for inner_user, ratings in trainset.ur.items():
            seen_items[int(inner_user)] = np.asarray(
                [inner_item for inner_item, _ in ratings], dtype=np.int64
            )

        return ModelSnapshot(
            version=str(manifest.get("version", "unknown")),
            rmse=float(manifest.get("rmse", 0.0)),
            precision_at_10=float(manifest.get("precision_at_10", 0.0)),
            trained_at=str(manifest.get("trained_at", "")),
            s3_uri=str(manifest.get("s3_uri", "")),
            loaded_at=time.time(),
            global_mean=float(trainset.global_mean),
            user_factors=np.asarray(model.pu, dtype=np.float32),
            item_factors=np.asarray(model.qi, dtype=np.float32),
            user_biases=np.asarray(model.bu, dtype=np.float32),
            item_biases=np.asarray(model.bi, dtype=np.float32),
            user_index=user_index,
            item_ids=item_ids,
            item_index=item_index,
            seen_items=seen_items,
            manifest=manifest,
        )

    def reload(self, force: bool = False) -> bool:
        """Devuelve True si cargó una versión nueva (o forzada)."""
        try:
            manifest = self._read_manifest()
        except Exception as exc:
            self._last_error = f"manifest_unavailable:{type(exc).__name__}"
            if not self._manifest_missing_logged:
                logger.info("model_manifest_not_available yet (se reintenta en background)")
                self._manifest_missing_logged = True
            return False

        version = str(manifest.get("version", "unknown"))
        with self._lock:
            current = self._snapshot
        if current is not None and current.version == version and not force:
            return False

        path = None
        try:
            path = self._download_artifact(manifest)
            model = self._load_surprise_model(path)
            snapshot = self._build_snapshot(model, manifest)
            with self._lock:
                self._snapshot = snapshot
            self._last_error = None
            self._manifest_missing_logged = False
            logger.info(
                "model_loaded version=%s rmse=%s precision_at_10=%s users=%d items=%d",
                snapshot.version,
                snapshot.rmse,
                snapshot.precision_at_10,
                snapshot.n_users,
                snapshot.n_items,
            )
            return True
        except Exception as exc:
            self._last_error = f"load_failed:{type(exc).__name__}:{exc}"
            logger.error(
                "model_load_failed version=%s error=%s", version, type(exc).__name__
            )
            return False
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    def _watch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.reload()
            except Exception as exc:  # pragma: no cover - robustez del hilo
                self._last_error = f"watcher:{type(exc).__name__}"
                logger.error("model_watcher_error error=%s", type(exc).__name__)
            self._stop.wait(max(2, settings.model_poll_seconds))

    def start(self) -> None:
        # Intento inicial en el hilo principal para que /ready refleje el
        # estado real lo antes posible.
        self.reload()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._watch_loop, name="model-watcher", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


model_loader = ModelLoader()
