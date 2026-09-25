"""
Загрузка модели прогнозирования из файла .joblib и получение прогноза.

Поддерживаются два формата файла:

1. «Пакет» в виде словаря — как в fire_risk_model_v2.joblib:
       models          — {"lgb": ..., "cb": ..., "baseline": ...}
       main_model      — какую модель использовать: "cb", "lgb", "baseline"
                         или "ens" (среднее lgb и cb)
       features        — список имён признаков
       baseline_features — признаки для простой модели "baseline"
       calibrator      — калибровка вероятности (IsotonicRegression)
       threshold       — порог: выше него риск считается повышенным

2. Обычная модель scikit-learn с методом predict_proba и
   атрибутом feature_names_in_.
"""

import threading
import warnings

import joblib
import numpy as np
import pandas as pd

# Порог по умолчанию, если в файле модели его нет
DEFAULT_THRESHOLD = 0.5


class ModelLoadError(Exception):
    """Файл не удалось прочитать или он не похож на модель прогнозирования."""


class FireRiskModel:
    """Обёртка над загруженной моделью: единый метод predict() для любых форматов."""

    def __init__(self, file_path):
        self.file_path = str(file_path)
        self._bundle = self._load_file(file_path)

        if isinstance(self._bundle, dict):
            self._init_from_bundle(self._bundle)
        elif hasattr(self._bundle, "predict_proba") and hasattr(self._bundle, "feature_names_in_"):
            self._init_from_sklearn_estimator(self._bundle)
        else:
            raise ModelLoadError(
                "Файл не похож на модель: нужен словарь с ключами models/features "
                "или модель scikit-learn с predict_proba."
            )

    # ------------------------------------------------------------------
    # Загрузка
    # ------------------------------------------------------------------
    @staticmethod
    def _load_file(file_path):
        try:
            # Предупреждения о разных версиях scikit-learn не мешают работе,
            # поэтому не засоряем ими консоль
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                return joblib.load(file_path)
        except ModuleNotFoundError as error:
            raise ModelLoadError(
                f"Для модели нужна библиотека «{error.name}». Установите её: pip install {error.name}"
            ) from error
        except Exception as error:  # noqa: BLE001 — показываем пользователю любую причину
            raise ModelLoadError(f"Не удалось прочитать файл модели: {error}") from error

    def _init_from_bundle(self, bundle):
        if "models" not in bundle or "features" not in bundle:
            raise ModelLoadError("В файле нет обязательных ключей «models» и «features».")

        self.models = bundle["models"]
        self.main_model_name = bundle.get("main_model", "ens")
        self.feature_names = list(bundle["features"])
        self.baseline_feature_names = list(bundle.get("baseline_features", []))
        self.calibrator = bundle.get("calibrator")
        self.threshold = float(bundle.get("threshold", DEFAULT_THRESHOLD))
        self.kind = "bundle"

    def _init_from_sklearn_estimator(self, estimator):
        self.models = {"sklearn": estimator}
        self.main_model_name = "sklearn"
        self.feature_names = list(estimator.feature_names_in_)
        self.baseline_feature_names = []
        self.calibrator = None
        self.threshold = DEFAULT_THRESHOLD
        self.kind = "sklearn"

    # ------------------------------------------------------------------
    # Описание для страницы настроек
    # ------------------------------------------------------------------
    def describe(self):
        return {
            "kind": self.kind,
            "main_model": self.main_model_name,
            "sub_models": ", ".join(self.models.keys()),
            "features_count": len(self.feature_names),
            "threshold": round(self.threshold, 4),
            "has_calibrator": self.calibrator is not None,
        }

    # ------------------------------------------------------------------
    # Прогноз
    # ------------------------------------------------------------------
    def predict(self, feature_table):
        """
        Считает вероятность повышенного пожарного риска для каждой строки.

        feature_table — DataFrame с признаками (одна строка = один объект).
        Отсутствующие признаки заполняются NaN (модели это допускают).

        Возвращает DataFrame с колонками:
            probability — итоговая вероятность (после калибровки);
            is_high_risk — True, если вероятность не ниже порога.
        """
        model_input = feature_table.reindex(columns=self.feature_names).astype(float)

        raw_probability = self._predict_raw_probability(model_input, feature_table)

        # Калибровка переводит «сырые» числа модели в честную вероятность
        if self.calibrator is not None:
            probability = np.asarray(self.calibrator.predict(raw_probability), dtype=float)
        else:
            probability = raw_probability

        return pd.DataFrame(
            {
                "raw_score": raw_probability,
                "probability": probability,
                "is_high_risk": probability >= self.threshold,
            },
            index=feature_table.index,
        )

    def _predict_raw_probability(self, model_input, feature_table):
        """
        Вызывает нужную модель (или ансамбль) и возвращает массив вероятностей.
        Повторяет функцию raw_predict из ноутбука «2. Построение модели v2».
        """
        name = self.main_model_name

        if name == "ens":
            # Ансамбль: среднее вероятностей LightGBM и CatBoost
            if "lgb" not in self.models or "cb" not in self.models:
                raise ModelLoadError("Для ансамбля (ens) в файле нужны модели lgb и cb.")
            lightgbm_probability = self._predict_lightgbm(model_input)
            catboost_probability = self._predict_with_predict_proba(self.models["cb"], model_input)
            return (lightgbm_probability + catboost_probability) / 2

        if name == "lgb":
            return self._predict_lightgbm(model_input)

        if name == "baseline":
            return self._predict_baseline(feature_table)

        # cb, sklearn и любая другая модель с методом predict_proba
        return self._predict_with_predict_proba(self.models[name], model_input)

    def _predict_lightgbm(self, model_input):
        booster = self.models["lgb"]
        # Как в ноутбуке: используем лучшую итерацию, найденную при обучении
        best_iteration = getattr(booster, "best_iteration", 0) or None
        return np.asarray(booster.predict(model_input, num_iteration=best_iteration), dtype=float)

    def _predict_baseline(self, feature_table):
        """
        Простая логистическая модель. В ноутбуке она обучалась на log(1 + x)
        от признаков, где отрицательные значения заменены нулём, — делаем так же.
        """
        baseline_input = feature_table.reindex(columns=self.baseline_feature_names).astype(float)
        transformed_input = np.log1p(baseline_input.clip(lower=0))
        return self._predict_with_predict_proba(self.models["baseline"], transformed_input)

    @staticmethod
    def _predict_with_predict_proba(model, model_input):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return np.asarray(model.predict_proba(model_input)[:, 1], dtype=float)


# ---------------------------------------------------------------------------
# Хранилище «текущей» модели. Одна на всё приложение.
# ---------------------------------------------------------------------------

class ActiveModelHolder:
    """Держит в памяти выбранную модель, чтобы не читать файл при каждом прогнозе."""

    def __init__(self):
        self._lock = threading.Lock()
        self._model = None
        self._model_path = None

    def get(self, model_path):
        """Возвращает модель для указанного файла; при смене файла перечитывает его."""
        with self._lock:
            if model_path is None:
                return None
            if self._model is None or self._model_path != str(model_path):
                self._model = FireRiskModel(model_path)
                self._model_path = str(model_path)
            return self._model

    def forget(self):
        with self._lock:
            self._model = None
            self._model_path = None


active_model_holder = ActiveModelHolder()
