"""
--- ЭМУЛЯТОР ---
Эмуляция (моделирование) работы системы по записанному журналу событий.

Идея: берём CSV-файл с событиями датчиков и «проигрываем» его,
как запись, в ускоренном времени. Для приложения это выглядит так,
будто данные приходят с датчиков прямо сейчас:

  * текущие показания датчиков обновляются (их видно на карте);
  * каждый раз, когда в модельном времени заканчивается час,
    строятся признаки и — если прогнозирование включено — модель
    считает риск для каждого объекта.

Эмуляция работает в отдельном потоке, чтобы веб-страницы
продолжали открываться, пока идёт моделирование.
"""

import threading
import time
from collections import deque
from datetime import datetime, timedelta

import pandas as pd

from services.feature_builder import HourlyFeatureBuilder
from services.model_service import ModelLoadError

# Структура файла журнала
REQUIRED_LOG_COLUMNS = ["ид_канала_данных", "дата", "время", "тревожное", "значение_датчика"]

# Доступные скорости
SPEED_OPTIONS = {
    1: "Реальное время (×1)",
    60: "Минута за секунду (×60)",
    600: "10 минут за секунду (×600)",
    3600: "Час за секунду (×3600)",
    7200: "2 Часа за секунду (×7200)",    
    10800: "3 Часа за секунду (×10800)",     
}

# Как часто (в реальных секундах) поток эмуляции «просыпается»
TICK_SECONDS = 0.25

# Показатели, которые объясняют диспетчеру, откуда взялся прогноз.
# (имя признака, подпись, как показать: 
#  "count" — целое, 
#  "number" — с единицей,
#  "percent" — доля в процентах, 
#  "flag" — да/нет)
EXPLAINING_FEATURES = [

    ("n_conf_ep_365d", "Подтверждённых пожаров за год", "count", ""),
    ("n_risk_sum_24h", "Событий-рисков за сутки", "count", ""),
    ("n_presence_sum_24h", "Признаков присутствия людей за сутки", "count", ""),
    ("n_unconf_ep_30d", "Неподтверждённых срабатываний за 30 дней", "count", ""),

    # Если предоставлен график горячих работ
    ("hot_work_active", "Идут горячие работы", "flag", ""),
    ("hot_work_next_24h", "Горячие работы в ближайшие сутки", "flag", ""),

    # Температура
    ("temp_min_roll_24h", "Мин. температура за сутки", "number", "°C"),
    ("temp_max_roll_24h", "Макс. температура за сутки", "number", "°C"),
    ("temp_slope_max_roll_24h", "Самый быстрый рост температуры", "number", "°C/ч"),
    ("temp_dev_max_roll_24h", "Отклонение от привычной температуры", "number", "°C"),

    # Газ
    ("gas_max_roll_24h", "Макс. значение газа", "number", "%"),
    
    # Датчик дыма
    ("pre_smoke_broken_sum_24h", "Неисправностей дымовых датчиков за 24h", "count", ""),        
    ("pre_smoke_broken_sum_7d", "Неисправностей дымовых датчиков за неделю", "count", ""),    
    ("pre_smoke_off_sum_24h", "Выключен дымовой датчик за 24h", "count", ""),        
    ("pre_smoke_off_sum_7d", "Выключен дымовой датчик за неделю", "count", ""),   

    ("blind_share", "Доля неисправных пожарных извещателей", "percent", ""),
]


def read_event_log(file_path):
    """
    Читает CSV-журнал событий и готовит его к проигрыванию.

    Возвращает DataFrame, отсортированный по времени, с колонками:
        event_time, channel_id, is_alarm, value.
    Если в файле нет нужных колонок — выбрасывает ValueError с понятным текстом.
    """
    log = pd.read_csv(file_path, dtype=str, encoding="utf-8-sig")
    log.columns = [column.strip() for column in log.columns]

    missing_columns = [column for column in REQUIRED_LOG_COLUMNS if column not in log.columns]
    if missing_columns:
        raise ValueError(
            "В файле журнала нет колонок: " + ", ".join(missing_columns)
            + ". Ожидаются: " + ", ".join(REQUIRED_LOG_COLUMNS)
        )

    # Дата и время лежат в разных колонках — склеиваем их
    event_time = pd.to_datetime(log["дата"] + " " + log["время"], errors="coerce")
    channel_id = pd.to_numeric(log["ид_канала_данных"], errors="coerce")
    is_alarm = log["тревожное"].str.strip().str.lower().isin(["true", "1", "да", "t"])

    prepared = pd.DataFrame({
        "event_time": event_time,
        "channel_id": channel_id,
        "is_alarm": is_alarm,
        "value": log["значение_датчика"].fillna(""),
    })

    # Строки с нераспознанной датой или каналом пропускаем
    prepared = prepared.dropna(subset=["event_time", "channel_id"])
    prepared["channel_id"] = prepared["channel_id"].astype(int)

    # kind="stable" сохраняет исходный порядок событий с одинаковым временем
    return prepared.sort_values("event_time", kind="stable").reset_index(drop=True)


class EmulationRunner:
    """Управляет одной эмуляцией: старт, пауза, остановка, текущее состояние."""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop_requested = threading.Event()
        self._pause_requested = threading.Event()
        # Построитель признаков используют и поток эмуляции, и веб-запрос
        # «пересчитать прогноз», поэтому работаем с ним только под этим замком
        self._builder_lock = threading.Lock()
        self._feature_builder = None
        self._get_prediction_model = None
        self._reset_state()

    def _reset_state(self):
        self.status = "idle"            # idle / running / paused / finished / stopped / error
        self.status_message = ""
        self.log_file_name = ""
        self.speed = 600
        self.total_events = 0
        self.processed_events = 0
        self.simulation_time = None     # текущее модельное время
        self.first_event_time = None
        self.last_event_time = None

        # channel_id -> {"value", "is_alarm", "time"}: последнее показание датчика
        self.live_readings = {}
        # object_id -> прогноз по объекту
        self.predictions = {}
        self.prediction_hour = None
        self.prediction_message = ""
        # Последние тревожные события (для ленты на главной странице)
        self.recent_alarms = deque(maxlen=30)

    # ------------------------------------------------------------------
    # Управление
    # ------------------------------------------------------------------
    def start(self, log_file_path, log_display_name, speed, channel_rows, object_rows,
              get_prediction_model, external_data=(None, None, None)):
        """
        Запускает эмуляцию в отдельном потоке.

        external_data — (горячие_работы, погода, инциденты): таблицы pandas или None.

        get_prediction_model — функция без параметров, которая возвращает
        модель (если прогнозирование включено) или None. Её вызывают
        в конце каждого модельного часа, поэтому включить или выключить
        прогноз можно прямо во время эмуляции.
        """
        self.stop()

        events = read_event_log(log_file_path)
        if events.empty:
            raise ValueError("В файле журнала нет ни одного события с корректной датой и каналом.")

        with self._lock:
            self._reset_state()
            self.status = "running"
            self.log_file_name = log_display_name
            self.speed = speed
            self.total_events = len(events)
            self.first_event_time = events["event_time"].iloc[0].to_pydatetime()
            self.last_event_time = events["event_time"].iloc[-1].to_pydatetime()
            self.simulation_time = self.first_event_time

        self._stop_requested.clear()
        self._pause_requested.clear()
        feature_builder = HourlyFeatureBuilder(channel_rows, object_rows)
        hot_works, weather, incidents = external_data
        feature_builder.set_external_data(hot_works=hot_works, weather=weather, incidents=incidents)
        self._feature_builder = feature_builder
        self._get_prediction_model = get_prediction_model

        self._thread = threading.Thread(
            target=self._run_loop,
            args=(events, feature_builder, get_prediction_model),
            name="emulation",
            daemon=True,
        )
        self._thread.start()

    def pause(self):
        if self.status == "running":
            self._pause_requested.set()
            self.status = "paused"

    def resume(self):
        if self.status == "paused":
            self._pause_requested.clear()
            self.status = "running"

    def stop(self):
        if self._thread and self._thread.is_alive():
            self._stop_requested.set()
            self._pause_requested.clear()
            self._thread.join(timeout=5)
            self.status = "stopped"

    def set_speed(self, speed):
        if speed in SPEED_OPTIONS:
            self.speed = speed

    # ------------------------------------------------------------------
    # Основной цикл
    # ------------------------------------------------------------------
    def _run_loop(self, events, feature_builder, get_prediction_model):
        """Проигрывает события, пока не кончится журнал или не нажмут «Остановить»."""
        # Переводим колонки в обычные списки Python: так цикл работает быстрее
        event_times = [moment.to_pydatetime() for moment in events["event_time"]]
        channel_ids = events["channel_id"].tolist()
        alarm_flags = events["is_alarm"].tolist()
        values = events["value"].tolist()

        next_event_index = 0
        last_tick = time.monotonic()

        try:
            while next_event_index < len(event_times):
                if self._stop_requested.is_set():
                    return

                # На паузе модельное время не идёт
                if self._pause_requested.is_set():
                    time.sleep(TICK_SECONDS)
                    last_tick = time.monotonic()
                    continue

                # Шаг 1. Двигаем модельные часы вперёд
                now = time.monotonic()
                real_seconds_passed = now - last_tick
                last_tick = now
                self.simulation_time += timedelta(seconds=real_seconds_passed * self.speed)

                # Шаг 2. Обрабатываем все события, чьё время уже наступило
                while (next_event_index < len(event_times)
                       and event_times[next_event_index] <= self.simulation_time):
                    self._process_event(
                        feature_builder,
                        event_times[next_event_index],
                        channel_ids[next_event_index],
                        values[next_event_index],
                        alarm_flags[next_event_index],
                        get_prediction_model,
                    )
                    next_event_index += 1
                    self.processed_events = next_event_index

                time.sleep(TICK_SECONDS)

            # Журнал закончился: закрываем последний час и делаем финальный прогноз
            with self._builder_lock:
                feature_builder.close_current_hour()
                self._update_predictions(feature_builder, get_prediction_model)
            self.simulation_time = self.last_event_time
            self.status = "finished"
            self.status_message = "Журнал проигран до конца."
        except Exception as error:  # noqa: BLE001 — любую ошибку показываем в интерфейсе
            self.status = "error"
            self.status_message = f"Эмуляция остановлена из-за ошибки: {error}"

    def _process_event(self, feature_builder, event_time, channel_id, value, is_alarm, get_prediction_model):
        """Обновляет показание датчика и передаёт событие в построитель признаков."""
        self.live_readings[channel_id] = {
            "value": value,
            "is_alarm": bool(is_alarm),
            "time": event_time.isoformat(sep=" ", timespec="seconds"),
        }
        if is_alarm:
            # Лента тревог читается и из веб-запросов, поэтому меняем её под замком
            with self._lock:
                self.recent_alarms.appendleft({
                    "channel_id": channel_id,
                    "value": value,
                    "time": event_time.isoformat(sep=" ", timespec="seconds"),
                })

        with self._builder_lock:
            closed_hours = feature_builder.add_event(event_time, channel_id, value, is_alarm)
            if closed_hours:
                self._update_predictions(feature_builder, get_prediction_model)

    # ------------------------------------------------------------------
    # Прогноз
    # ------------------------------------------------------------------
    def _update_predictions(self, feature_builder, get_prediction_model):
        """Строит признаки за последний закрытый час и запрашивает прогноз у модели."""
        try:
            model = get_prediction_model()
        except ModelLoadError as error:
            self.prediction_message = str(error)
            return

        if model is None:
            self.prediction_message = "Прогнозирование выключено."
            return

        feature_table = feature_builder.build_latest_features()
        if feature_table.empty:
            return

        prediction_table = model.predict(feature_table)

        new_predictions = {}
        for object_id, prediction_row in prediction_table.iterrows():
            feature_row = feature_table.loc[object_id]
            new_predictions[int(object_id)] = {
                "probability": round(float(prediction_row["probability"]), 4),
                "is_high_risk": bool(prediction_row["is_high_risk"]),
                "factors": self._explain(feature_row),
            }

        hour_start = feature_table["hour_start"].iloc[0]
        self.predictions = new_predictions
        self.prediction_hour = f"{hour_start:%d.%m.%Y %H:00}–{hour_start + timedelta(hours=1):%H:00}"
        self.prediction_message = ""

    def recalculate_predictions(self):
        """
        Пересчитывает прогноз по последнему закрытому часу прямо сейчас.
        Нужно, когда прогноз включили во время или после эмуляции,
        чтобы не ждать конца следующего модельного часа.
        """
        if self._feature_builder is None:
            return
        with self._builder_lock:
            self._update_predictions(self._feature_builder, self._get_prediction_model)

    def update_external_data(self, hot_works, weather, incidents):
        """
        Подменяет дополнительные данные в идущей эмуляции (после импорта
        в настройках) и сразу пересчитывает прогноз.
        """
        if self._feature_builder is None:
            return
        with self._builder_lock:
            self._feature_builder.set_external_data(hot_works=hot_works, weather=weather, incidents=incidents)
            self._update_predictions(self._feature_builder, self._get_prediction_model)

    @staticmethod
    def _explain(feature_row):
        """Выбирает понятные человеку показатели, которые стоит показать рядом с прогнозом."""
        factors = []
        for feature_name, label, kind, unit in EXPLAINING_FEATURES:
            value = feature_row.get(feature_name)
            if value is None or pd.isna(value) or value == 0:
                continue

            if kind == "flag":
                text_value = "да"
            elif kind == "percent":
                text_value = f"{value * 100:.1f}%"
            elif kind == "count":
                text_value = f"{value:.0f}"
            else:
                text_value = f"{value:.1f} {unit}".strip()
            factors.append({"label": label, "value": text_value})
        return factors

    # ------------------------------------------------------------------
    # Состояние для веб-страниц
    # ------------------------------------------------------------------
    def get_recent_alarms(self):
        """Копия ленты последних тревог (безопасно читать из другого потока)."""
        with self._lock:
            return list(self.recent_alarms)

    def snapshot(self):
        """Краткое состояние эмуляции (для панели управления и шапки)."""
        progress_percent = 0
        if self.total_events:
            progress_percent = round(100 * self.processed_events / self.total_events, 1)

        return {
            "status": self.status,
            "status_message": self.status_message,
            "log_file_name": self.log_file_name,
            "speed": self.speed,
            "total_events": self.total_events,
            "processed_events": self.processed_events,
            "progress_percent": progress_percent,
            "simulation_time": _format_time(self.simulation_time),
            "first_event_time": _format_time(self.first_event_time),
            "last_event_time": _format_time(self.last_event_time),
            "prediction_hour": self.prediction_hour,
            "prediction_message": self.prediction_message,
        }


def _format_time(moment):
    return moment.strftime("%d.%m.%Y %H:%M:%S") if isinstance(moment, datetime) else None


# Одна эмуляция на всё приложение
emulation_runner = EmulationRunner()
