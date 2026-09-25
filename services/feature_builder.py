"""
Подготовка признаков для модели «Пожарный риск» — по ноутбуку
«2. Построение модели» (разделы 3.3–3.7 и «История единицы мониторинга»).

В ноутбуке признаки считаются сразу по всему журналу за годы (пакетно).
В сервисе события приходят по одному, поэтому здесь те же формулы «потоковые»:

  1. add_event() принимает событие и копит счётчики текущего часа
     по каждому объекту (в ноутбуке — «единица мониторинга», unit).
  2. Когда приходит событие из следующего часа, текущий час закрывается:
     для каждого объекта получается одна строка почасовой сетки.
  3. build_latest_features() строит полный набор признаков
     для последнего закрытого часа: суммы за окна, тренды, историю
     пожарных эпизодов, горячие работы и погоду.
"""

import bisect
import statistics
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd

from services.event_classifier import (
    FIRE_DETECTOR_TYPES,
    LEVEL_DROP,
    LEVEL_FIRE,
    LEVEL_NO_DATA,
    LEVEL_OK,
    LEVEL_PRESENCE,
    LEVEL_RISK,
    PRECURSORS,
    SIGNAL_LEVELS,
    TEMP_PERSIST_C,
    TEMP_PERSIST_MINUTES,
    TYPE_SHORT,
    UNKNOWN_TYPE_SHORT,
    classify_pair,
    fire_detector_state,
    normalize_value,
    precursor_name,
    type_short_name,
)

# ---------------------------------------------------------------------------
# Настройки из ноутбука (раздел 1.2)
# ---------------------------------------------------------------------------
EPISODE_GAP = timedelta(hours=6)              # EP_GAP: разрыв, после которого начинается новый эпизод
EPISODE_CONFIRM_WINDOW = timedelta(minutes=15)  # EP_CONFIRM_WINDOW: 2 канала в этом окне = подтверждение
CONFIRM_BY_HEAT = True                        # тепловой извещатель подтверждает эпизод
INCIDENT_MATCH_WINDOW = timedelta(hours=2)    # инцидент из реестра ±2 ч от эпизода
TEMPERATURE_BASELINE_WINDOW = timedelta(days=7)  # «привычная» температура канала — медиана за 7 дней
TEMPERATURE_BASELINE_MIN_READINGS = 5
MIN_SLOPE_STEP_HOURS = 1/6                    # скорость температуры считаем при шаге ≥ 10 мин.
TEMPERATURE_DEVIATION_LIMIT = 5.0             # «отклонение больше 5 °C» для n_temp_dev5
HISTORY_SINCE_CLIP_HOURS = 24 * 365           # «часов с последнего эпизода» не больше года
HOT_WORK_LOOKAHEAD = timedelta(hours=24)      # «горячие работы в ближайшие 24 часа»

# Окна для скользящих сумм и максимумов (раздел 3.7)
SUM_WINDOWS_HOURS = {"6h": 6, "24h": 24, "72h": 72, "7d": 168}
ROLL_WINDOW_NAMES = ["6h", "24h", "7d"]

# Отопительный сезон по ноутбуку: октябрь — апрель
HEATING_SEASON_MONTHS = {10, 11, 12, 1, 2, 3, 4}

# ---------------------------------------------------------------------------
# Списки признаков
# ---------------------------------------------------------------------------
ALL_TYPE_SHORTS = sorted(set(TYPE_SHORT.values()) | {UNKNOWN_TYPE_SHORT})

# Почасовые счётчики. Для них считаются суммы за окна (roll_src в ноутбуке).
HOURLY_COUNT_FEATURES = (
    ["n_events", "n_alarm_flag", "n_risk", "n_no_data", "n_presence", "n_gas"]
    + [f"risk_{short}" for short in ALL_TYPE_SHORTS]
    + [f"pres_{short}" for short in ALL_TYPE_SHORTS]
    + sorted(set(PRECURSORS.values()))
    + ["uniq_ch_risk", "uniq_ch_no_data", "n_temp_dev5"]
)

# Почасовые показатели температуры и газа. Если показаний не было — NaN, а не 0.
HOURLY_MEASURE_FEATURES = [
    "gas_max", "temp_max", "temp_min", "temp_dev_max", "temp_slope_max", "temp_spread_max",
]

# Какой функцией «сворачивается» каждый показатель в скользящем окне
ROLL_AGGREGATIONS = [
    ("temp_max", "max"), ("temp_min", "min"), ("temp_dev_max", "max"),
    ("temp_slope_max", "max"), ("temp_spread_max", "max"), ("gas_max", "max"),
    ("blind_share", "max"),
]

TREND_FEATURES = ["n_risk", "n_presence", "n_no_data", "risk_smoke", "risk_power", "n_events"]
PER_CHANNEL_FEATURES = ["n_events_sum_24h", "n_risk_sum_24h", "n_no_data_sum_24h", "n_presence_sum_24h"]


# ---------------------------------------------------------------------------
# Вспомогательные структуры
# ---------------------------------------------------------------------------

@dataclass
class TemperatureChannelMemory:
    """Что нужно помнить о датчике температуры между показаниями."""
    last_time: object = None
    last_value: float = None
    # Значения для «привычной» температуры: (момент, значение предыдущего показания)
    baseline_values: deque = field(default_factory=deque)
    # Показание ≥ 60 °C, которое ещё не подтверждено соседним показанием
    pending_spike_time: object = None


@dataclass
class FireEpisode:
    """Эпизод срабатываний: fire-события одного объекта без разрывов больше 6 часов."""
    unit: int
    start: object
    end: object
    last_channel: int
    has_multiple_channels: bool = False  # два разных канала в пределах 15 минут
    has_manual: bool = False             # ручной извещатель
    has_heat: bool = False               # тепловой извещатель
    has_temperature: bool = False        # устойчивая температура ≥ 60 °C

    def is_confirmed_by_rules(self):
        return (self.has_multiple_channels or self.has_manual or self.has_temperature
                or (self.has_heat and CONFIRM_BY_HEAT))


def nan_max(current, new_value):
    """Максимум, который не портится пропусками (NaN)."""
    if new_value is None or np.isnan(new_value):
        return current
    if current is None or np.isnan(current):
        return new_value
    return max(current, new_value)


def is_heating_season(moment):
    return 1 if moment.month in HEATING_SEASON_MONTHS else 0


class HourlyFeatureBuilder:
    """Копит события по часам и строит признаки для модели."""

    def __init__(self, channel_rows, object_rows, history_hours=168):
        """
        channel_rows  — строки справочника датчиков (channel_id, sensor_type, object_id);
        object_rows   — строки справочника объектов;
        history_hours — сколько последних часов хранить (неделя = 168 — самое длинное окно).
        """
        # Канал -> (объект, тип датчика). Объект = «единица мониторинга» (unit) в ноутбуке.
        self.channel_info = {
            row["channel_id"]: (row["object_id"], row["sensor_type"].strip())
            for row in channel_rows
            if row["object_id"] is not None
        }
        self.object_info = {row["object_id"]: row for row in object_rows}
        self.static_features = self._calculate_static_features()

        # Внешние данные (см. set_external_data)
        self.hot_works_by_unit = None
        self.weather_by_hour = None
        self.weather_columns = []
        self.incidents = None

        # Состояние, которое живёт дольше одного часа
        self.temperature_memory = {}         # канал -> TemperatureChannelMemory
        self.fire_detector_states = {}       # канал пожарного извещателя -> 0/1 («слепой»)
        self.episodes = []                   # все эпизоды срабатываний
        self.open_episode_by_unit = {}       # объект -> последний эпизод
        self.last_fire_event_by_unit = {}    # объект -> (момент, канал) последнего fire-события
        self.first_hour_by_unit = {}         # с какого часа объект «наблюдается»
        self.first_event_time = None

        # История закрытых часов по каждому объекту
        self.history_hours = history_hours
        self.closed_rows_by_unit = {}

        # Текущий, ещё не закрытый час
        self.current_hour = None
        self._reset_hour_accumulators()

    # ==================================================================
    # Постоянные признаки объекта (раздел 2.1 ноутбука)
    # ==================================================================
    def _calculate_static_features(self):
        counts_by_unit = {}
        for unit, sensor_type in self.channel_info.values():
            counts_by_unit.setdefault(unit, Counter())[type_short_name(sensor_type)] += 1

        # Типы, которые вообще встречаются в справочнике (как колонки после unstack в ноутбуке)
        present_shorts = sorted({short for counts in counts_by_unit.values() for short in counts})

        # Вид объекта кодируется номером в отсортированном списке видов (astype('category').cat.codes)
        unit_kinds = sorted({
            self.object_info[unit]["object_kind"] for unit in counts_by_unit if unit in self.object_info
        })
        kind_codes = {kind: code for code, kind in enumerate(unit_kinds)}

        static_features = {}
        for unit, counts in counts_by_unit.items():
            features = {f"n_ch_{short}": counts.get(short, 0) for short in present_shorts}
            features["n_ch_total"] = sum(counts.values())
            features["n_ch_fire_det"] = counts.get("smoke", 0) + counts.get("heat", 0) + counts.get("manual", 0)

            object_row = self.object_info.get(unit)
            features["obj_kind"] = kind_codes.get(object_row["object_kind"], -1) if object_row else -1
            parent_id = object_row["parent_id"] if object_row else None
            features["obj_parent"] = parent_id if parent_id is not None else -1
            features["obj_level"] = object_row["hierarchy_level"] if object_row else -1
            static_features[unit] = features
        return static_features

    # ==================================================================
    # Внешние данные: горячие работы, погода, реестр инцидентов
    # ==================================================================
    def set_external_data(self, hot_works=None, weather=None, incidents=None):
        """
        Подключает дополнительные данные. Каждый аргумент — DataFrame или None.

        hot_works — колонки object_id, start, end   (АРМ-Контроль, горячие работы);
        weather   — колонка hour_start и любые числовые колонки (температура, влажность…);
        incidents — колонки object_id, incident_time (реестр пожарных инцидентов).

        Если данных нет — соответствующие признаки не создаются, как в ноутбуке.
        """
        if hot_works is not None and not hot_works.empty:
            self.hot_works_by_unit = {}
            for unit, works in hot_works.groupby("object_id"):
                self.hot_works_by_unit[int(unit)] = (
                    sorted(works["start"].tolist()),
                    sorted(works["end"].tolist()),
                )
        else:
            self.hot_works_by_unit = None

        if weather is not None and not weather.empty:
            self.weather_columns = [column for column in weather.columns if column != "hour_start"]
            hourly_mean = weather.groupby("hour_start")[self.weather_columns].mean()
            self.weather_by_hour = hourly_mean.to_dict(orient="index")
        else:
            self.weather_by_hour = None
            self.weather_columns = []

        self.incidents = incidents if incidents is not None and not incidents.empty else None

    # ==================================================================
    # Приём событий (process_journal в ноутбуке)
    # ==================================================================
    def _reset_hour_accumulators(self):
        self.hour_counts = {}           # объект -> Counter счётчиков
        self.hour_unique_channels = {}  # объект -> {"risk": set(), "no_data": set()}
        self.hour_gas_max = {}          # объект -> максимум газа
        self.hour_temperature = {}      # объект -> {канал: {"v", "dev", "slope"}}
        self.hour_seen_events = set()   # для удаления дубликатов

    def add_event(self, event_time, channel_id, value, is_alarm_flag):
        """
        Учитывает одно событие журнала.

        Возвращает список часов, закрытых из-за этого события
        (не пустой, когда событие пришло из следующего часа).
        """
        event_hour = event_time.replace(minute=0, second=0, microsecond=0)
        closed_hours = []

        if self.current_hour is None:
            self.current_hour = event_hour
            self.first_event_time = event_time
        # ОТЛИЧИЕ: опоздавшее событие из уже закрытого часа пропускаем —
        # закрытую строку сетки изменить уже нельзя
        if event_hour < self.current_hour:
            return closed_hours
        while event_hour > self.current_hour:
            closed_hours.append(self.close_current_hour())

        channel = self.channel_info.get(channel_id)
        if channel is None:
            return closed_hours  # канала нет в справочнике или он не привязан к объекту
        unit, sensor_type = channel

        # Шаг 1. Дубликаты (канал, время, значение) удаляются, как в ноутбуке
        duplicate_key = (channel_id, event_time, normalize_value(value))
        if duplicate_key in self.hour_seen_events:
            return closed_hours
        self.hour_seen_events.add(duplicate_key)

        # Шаг 2. Уровень события
        level, number = classify_pair(sensor_type, value)
        if level == LEVEL_DROP:
            return closed_hours

        # Объект начинает «наблюдаться» с часа своего первого события
        self.first_hour_by_unit.setdefault(unit, self.current_hour)

        # Шаг 3. Температура: проверка устойчивости пожара и признаки по датчику
        is_temperature_reading = sensor_type == "Датчик температуры" and not np.isnan(number)
        if is_temperature_reading:
            level = self._add_temperature(unit, channel_id, event_time, number, level)

        # Шаг 4. fire-события идут только в эпизоды (историю пожаров)
        if level == LEVEL_FIRE:
            self._register_fire_event(unit, channel_id, sensor_type, event_time)

        # Шаг 5. Общая активность: без штатных и fire-событий пожарных извещателей
        is_fire_detector = sensor_type in FIRE_DETECTOR_TYPES
        counts = self.hour_counts.setdefault(unit, Counter())
        if not (is_fire_detector and level in (LEVEL_FIRE, LEVEL_OK)):
            counts["n_events"] += 1
            if is_alarm_flag:
                counts["n_alarm_flag"] += 1

        # Шаг 6. Счётчики по уровням, типам датчиков и предвестникам
        if level in SIGNAL_LEVELS:
            short = type_short_name(sensor_type)
            counts[f"n_{level}"] += 1
            if level == LEVEL_RISK:
                counts[f"risk_{short}"] += 1
            if level == LEVEL_PRESENCE:
                counts[f"pres_{short}"] += 1
            precursor = precursor_name(sensor_type, value)
            if precursor:
                counts[precursor] += 1
            if level in (LEVEL_RISK, LEVEL_NO_DATA):
                unique_channels = self.hour_unique_channels.setdefault(unit, {LEVEL_RISK: set(), LEVEL_NO_DATA: set()})
                unique_channels[level].add(channel_id)

        # Шаг 7. Газ: максимум показаний за час
        if sensor_type == "Газовый датчик" and not np.isnan(number):
            self.hour_gas_max[unit] = nan_max(self.hour_gas_max.get(unit), number)

        # Шаг 8. «Слепота» пожарных извещателей: запоминаем последнее состояние
        if is_fire_detector:
            state = fire_detector_state(value)
            if state is not None:
                self.fire_detector_states[channel_id] = state

        return closed_hours

    def _add_temperature(self, unit, channel_id, event_time, temperature, level):
        """
        Учитывает показание датчика температуры. Возвращает уровень события
        (fire может превратиться в risk, если перегрев не подтвердился).

        По ноутбуку:
          * «привычная» температура (base_7d) — медиана предыдущих показаний
            канала за 7 дней, если их не меньше 5;
          * отклонение (dev) = показание − привычная температура;
          * скорость (slope) = изменение / время между показаниями, °C/ч,
            если между показаниями не меньше 5 минут;
          * 60 °C и выше — пожар, только если соседнее показание того же
            датчика в пределах 30 минут тоже не ниже 50 °C. Иначе это
            одиночный скачок, и он считается риском.
        """
        memory = self.temperature_memory.setdefault(channel_id, TemperatureChannelMemory())
        persist_window = timedelta(minutes=TEMP_PERSIST_MINUTES)

        # --- Привычная температура: окно (t − 7 дней, t] по предыдущим показаниям ---
        if memory.last_value is not None:
            memory.baseline_values.append((event_time, memory.last_value))
        while memory.baseline_values and memory.baseline_values[0][0] <= event_time - TEMPERATURE_BASELINE_WINDOW:
            memory.baseline_values.popleft()
        if len(memory.baseline_values) >= TEMPERATURE_BASELINE_MIN_READINGS:
            baseline = statistics.median(value for _time, value in memory.baseline_values)
            deviation = temperature - baseline
        else:
            deviation = np.nan

        # --- Скорость изменения ---
        slope = np.nan
        if memory.last_time is not None:
            step_hours = (event_time - memory.last_time).total_seconds() / 3600
            if step_hours >= MIN_SLOPE_STEP_HOURS:
                slope = (temperature - memory.last_value) / step_hours

        # --- Подтверждение пожара соседним показанием ---
        previous_is_hot = (
            memory.last_value is not None
            and memory.last_value >= TEMP_PERSIST_C
            and event_time - memory.last_time <= persist_window
        )
        # Предыдущий скачок ≥ 60 °C подтверждается текущим показанием ≥ 50 °C
        if memory.pending_spike_time is not None:
            if temperature >= TEMP_PERSIST_C and event_time - memory.pending_spike_time <= persist_window:
                # ОТЛИЧИЕ: в ноутбуке скачок сразу стал бы fire и не попал в счётчики риска.
                # Здесь час со скачком мог уже закрыться, поэтому исправляем только эпизоды.
                self._register_fire_event(unit, channel_id, "Датчик температуры", memory.pending_spike_time)
            memory.pending_spike_time = None

        if level == LEVEL_FIRE and not previous_is_hot:
            level = LEVEL_RISK
            memory.pending_spike_time = event_time

        # --- Запоминаем для следующего показания ---
        memory.last_time = event_time
        memory.last_value = temperature

        # --- Показатели канала за час: максимум показания, отклонения и скорости ---
        channels = self.hour_temperature.setdefault(unit, {})
        channel_hour = channels.setdefault(channel_id, {"v": np.nan, "dev": np.nan, "slope": np.nan})
        channel_hour["v"] = nan_max(channel_hour["v"], temperature)
        channel_hour["dev"] = nan_max(channel_hour["dev"], deviation)
        channel_hour["slope"] = nan_max(channel_hour["slope"], slope)
        return level

    def _register_fire_event(self, unit, channel_id, sensor_type, event_time):
        """Добавляет fire-событие в эпизод (build_episodes в ноутбуке)."""
        episode = self.open_episode_by_unit.get(unit)
        if episode is None or event_time - episode.end > EPISODE_GAP:
            episode = FireEpisode(unit=unit, start=event_time, end=event_time, last_channel=channel_id)
            self.episodes.append(episode)
            self.open_episode_by_unit[unit] = episode

        # Два разных канала подряд в пределах 15 минут — подтверждение
        previous_event = self.last_fire_event_by_unit.get(unit)
        if previous_event is not None:
            previous_time, previous_channel = previous_event
            if previous_channel != channel_id and abs(event_time - previous_time) <= EPISODE_CONFIRM_WINDOW:
                episode.has_multiple_channels = True
        self.last_fire_event_by_unit[unit] = (event_time, channel_id)

        episode.start = min(episode.start, event_time)
        episode.end = max(episode.end, event_time)
        episode.has_manual |= sensor_type == "Ручной извещатель"
        episode.has_heat |= sensor_type == "Тепловой датчик"
        episode.has_temperature |= sensor_type == "Датчик температуры"

    # ==================================================================
    # Закрытие часа: строка почасовой сетки (раздел 3.5 ноутбука)
    # ==================================================================
    def close_current_hour(self):
        """Превращает накопленное за час в строки сетки и начинает новый час."""
        blind_count_by_unit = Counter(
            self.channel_info[channel_id][0]
            for channel_id, state in self.fire_detector_states.items()
            if state == 1
        )

        for unit, first_hour in self.first_hour_by_unit.items():
            if first_hour > self.current_hour:
                continue
            row = {"unit": unit, "hour_start": self.current_hour}

            # Счётчики: пропуск = 0
            counts = self.hour_counts.get(unit, Counter())
            for feature_name in HOURLY_COUNT_FEATURES:
                row[feature_name] = float(counts.get(feature_name, 0))
            unique_channels = self.hour_unique_channels.get(unit, {})
            row["uniq_ch_risk"] = float(len(unique_channels.get(LEVEL_RISK, ())))
            row["uniq_ch_no_data"] = float(len(unique_channels.get(LEVEL_NO_DATA, ())))

            # Газ и температура: пропуск остаётся NaN
            row["gas_max"] = self.hour_gas_max.get(unit, np.nan)
            row.update(self._summarize_temperature(self.hour_temperature.get(unit, {})))

            # «Слепые» пожарные извещатели на конец часа
            fire_detectors_total = self.static_features[unit]["n_ch_fire_det"]
            row["n_blind_fd"] = float(blind_count_by_unit.get(unit, 0))
            row["blind_share"] = row["n_blind_fd"] / fire_detectors_total if fire_detectors_total else np.nan

            history = self.closed_rows_by_unit.setdefault(unit, deque(maxlen=self.history_hours))
            history.append(row)

        closed_hour = self.current_hour
        self.current_hour = self.current_hour + timedelta(hours=1)
        self._reset_hour_accumulators()
        return closed_hour

    @staticmethod
    def _summarize_temperature(channels):
        """
        Сводит показатели датчиков температуры объекта за час.

        spread — насколько самый горячий датчик выше медианы датчиков объекта;
        n_temp_dev5 — сколько датчиков отклонились от привычного больше чем на 5 °C.
        """
        if not channels:
            return {
                "temp_max": np.nan, "temp_min": np.nan, "temp_dev_max": np.nan,
                "temp_slope_max": np.nan, "temp_spread_max": np.nan, "n_temp_dev5": 0.0,
            }

        values = np.array([item["v"] for item in channels.values()], dtype=float)
        deviations = np.array([item["dev"] for item in channels.values()], dtype=float)
        slopes = np.array([item["slope"] for item in channels.values()], dtype=float)
        spreads = values - np.median(values)

        def max_or_nan(array):
            return float(np.nanmax(array)) if np.any(~np.isnan(array)) else np.nan

        return {
            "temp_max": float(values.max()),
            "temp_min": float(values.min()),
            "temp_dev_max": max_or_nan(deviations),
            "temp_slope_max": max_or_nan(slopes),
            "temp_spread_max": float(spreads.max()),
            "n_temp_dev5": float(np.sum(deviations > TEMPERATURE_DEVIATION_LIMIT)),
        }

    # ==================================================================
    # Полный набор признаков для последнего закрытого часа
    # ==================================================================
    def build_latest_features(self):
        """
        Возвращает DataFrame: одна строка на объект (индекс — ид объекта).
        Колонка hour_start — начало часа, t_pred — момент прогноза (конец часа).
        """
        all_rows = [row for rows in self.closed_rows_by_unit.values() for row in rows]
        if not all_rows:
            return pd.DataFrame()

        history = pd.DataFrame(all_rows).sort_values(["unit", "hour_start"]).reset_index(drop=True)
        by_unit = history.groupby("unit", sort=False)

        # Шаг 1. Последняя строка каждого объекта — это и есть «текущий час»
        latest = by_unit.tail(1).set_index("unit")

        # Шаг 2. Суммы счётчиков за окна. Строки идут по часам без пропусков,
        # поэтому «последние N строк» = «последние N часов»
        new_columns = {}
        for window_name, window_hours in SUM_WINDOWS_HOURS.items():
            window_sums = by_unit.tail(window_hours).groupby("unit")[HOURLY_COUNT_FEATURES].sum()
            for feature_name in HOURLY_COUNT_FEATURES:
                new_columns[f"{feature_name}_sum_{window_name}"] = window_sums[feature_name]

        # Шаг 3. Скользящие максимумы/минимумы температуры, газа и «слепоты»
        for window_name in ROLL_WINDOW_NAMES:
            window_rows = by_unit.tail(SUM_WINDOWS_HOURS[window_name]).groupby("unit")
            for feature_name, how in ROLL_AGGREGATIONS:
                new_columns[f"{feature_name}_roll_{window_name}"] = getattr(window_rows[feature_name], how)()

        latest = latest.join(pd.DataFrame(new_columns))

        # Шаг 4. Постоянные признаки объекта
        latest = latest.join(pd.DataFrame.from_dict(self.static_features, orient="index"))

        # Шаг 5. Тренды: последние сутки против средней суточной за неделю
        for feature_name in TREND_FEATURES:
            latest[f"{feature_name}_trend"] = (
                latest[f"{feature_name}_sum_24h"] / (latest[f"{feature_name}_sum_7d"] / 7 + 1)
            )

        # Шаг 6. Нормировка на число каналов
        channels_total = latest["n_ch_total"].replace(0, np.nan)
        for feature_name in PER_CHANNEL_FEATURES:
            latest[f"{feature_name}_per_ch"] = latest[feature_name] / channels_total
        if "n_ch_smoke" in latest:
            latest["risk_smoke_sum_7d_per_ch"] = latest["risk_smoke_sum_7d"] / latest["n_ch_smoke"].replace(0, np.nan)

        # Шаг 7. Календарь. Момент прогноза — конец часа
        latest["t_pred"] = latest["hour_start"] + pd.Timedelta(hours=1)
        latest["hour"] = latest["hour_start"].apply(lambda moment: moment.hour)
        latest["heating_season"] = latest["hour_start"].apply(is_heating_season)

        # Шаг 8. Внешние данные и история эпизодов
        self._add_hot_work_features(latest)
        self._add_weather_features(latest)
        self._add_history_features(latest)

        latest.index.name = "object_id"
        return latest

    # ------------------------------------------------------------------
    def _add_hot_work_features(self, latest):
        """
        Горячие работы (раздел 3.6 ноутбука), на момент прогноза t_pred:
          hot_work_active      — работы идут сейчас;
          hours_since_hot_work — часов с окончания последних работ;
          hot_work_next_24h    — работы начнутся в ближайшие 24 часа.
        """
        if self.hot_works_by_unit is None:
            return

        active_values, since_values, next_values = [], [], []
        for unit, prediction_time in zip(latest.index, latest["t_pred"]):
            starts, ends = self.hot_works_by_unit.get(unit, ([], []))

            started_count = bisect.bisect_right(starts, prediction_time)
            ended_count = bisect.bisect_right(ends, prediction_time)
            active_values.append(float(started_count > ended_count))

            if ended_count > 0:
                last_end = ends[ended_count - 1]
                since_values.append((prediction_time - last_end).total_seconds() / 3600)
            else:
                since_values.append(np.nan)

            next_start = starts[started_count] if started_count < len(starts) else None
            is_soon = next_start is not None and next_start - prediction_time <= HOT_WORK_LOOKAHEAD
            next_values.append(float(is_soon))

        latest["hot_work_active"] = active_values
        latest["hours_since_hot_work"] = since_values
        latest["hot_work_next_24h"] = next_values

    def _add_weather_features(self, latest):
        """Погода за час строки: колонки называются так же, как в загруженном файле."""
        if self.weather_by_hour is None:
            return
        for column in self.weather_columns:
            latest[column] = [
                self.weather_by_hour.get(hour, {}).get(column, np.nan) for hour in latest["hour_start"]
            ]

    def _add_history_features(self, latest):
        """
        История эпизодов объекта — только по эпизодам, начавшимся ДО момента прогноза:
          h_since_any_ep  — часов с начала последнего эпизода;
          h_since_conf_ep — часов с начала последнего подтверждённого эпизода;
          n_unconf_ep_30d — неподтверждённых эпизодов за 30 дней («шумность» объекта);
          n_conf_ep_365d  — подтверждённых эпизодов за год.
        """
        episode_starts = self._episode_starts_by_unit()

        columns = {"h_since_any_ep": [], "h_since_conf_ep": [], "n_unconf_ep_30d": [], "n_conf_ep_365d": []}
        for unit, prediction_time in zip(latest.index, latest["t_pred"]):
            starts = episode_starts.get(unit, {"all": [], "confirmed": [], "unconfirmed": []})
            columns["h_since_any_ep"].append(_hours_since_last(starts["all"], prediction_time))
            columns["h_since_conf_ep"].append(_hours_since_last(starts["confirmed"], prediction_time))
            columns["n_unconf_ep_30d"].append(_count_in_window(starts["unconfirmed"], prediction_time, 30))
            columns["n_conf_ep_365d"].append(_count_in_window(starts["confirmed"], prediction_time, 365))

        for column_name, values in columns.items():
            latest[column_name] = values

    def _episode_starts_by_unit(self):
        """
        Начала эпизодов по объектам, разделённые на подтверждённые и нет.

        Подтверждение — как в ноутбуке: если загружен реестр пожарных инцидентов,
        подтверждённым считается только эпизод, совпавший с инцидентом (±2 ч).
        Иначе — по правилам (2 канала, ручной, тепловой, устойчивая температура).

        ОТЛИЧИЕ: в ноутбуке журнал покрывает всю историю. В сервисе журнал
        начинается «сейчас», поэтому инциденты из реестра, случившиеся до начала
        журнала, добавляются как подтверждённые эпизоды — это история объекта.
        """
        incidents_by_unit = {}
        if self.incidents is not None:
            for unit, rows in self.incidents.groupby("object_id"):
                incidents_by_unit[int(unit)] = rows["incident_time"].tolist()

        starts_by_unit = {}

        def add_start(unit, start, is_confirmed):
            starts = starts_by_unit.setdefault(unit, {"all": [], "confirmed": [], "unconfirmed": []})
            starts["all"].append(start)
            starts["confirmed" if is_confirmed else "unconfirmed"].append(start)

        for episode in self.episodes:
            if self.incidents is not None:
                is_confirmed = any(
                    episode.start <= incident_time + INCIDENT_MATCH_WINDOW
                    and episode.end >= incident_time - INCIDENT_MATCH_WINDOW
                    for incident_time in incidents_by_unit.get(episode.unit, [])
                )
            else:
                is_confirmed = episode.is_confirmed_by_rules()
            add_start(episode.unit, episode.start, is_confirmed)

        if self.first_event_time is not None:
            for unit, incident_times in incidents_by_unit.items():
                for incident_time in incident_times:
                    if incident_time < self.first_event_time:
                        add_start(unit, incident_time, True)

        for starts in starts_by_unit.values():
            for key in starts:
                starts[key].sort()
        return starts_by_unit


def _hours_since_last(sorted_starts, prediction_time):
    """Часов с последнего начала эпизода строго до момента прогноза (не больше года)."""
    count_before = bisect.bisect_left(sorted_starts, prediction_time)
    if count_before == 0:
        return np.nan
    hours = (prediction_time - sorted_starts[count_before - 1]).total_seconds() / 3600
    return min(hours, HISTORY_SINCE_CLIP_HOURS)


def _count_in_window(sorted_starts, prediction_time, window_days):
    """Сколько эпизодов началось в окне [t − N дней, t)."""
    count_before = bisect.bisect_left(sorted_starts, prediction_time)
    count_before_window = bisect.bisect_left(sorted_starts, prediction_time - timedelta(days=window_days))
    return float(count_before - count_before_window)
