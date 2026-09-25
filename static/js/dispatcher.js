/*
 * Рабочее место диспетчера.
 *
 * Скрипт отвечает за три блока страницы:
 *   1. карта Москвы с объектами (библиотека Leaflet, подложка OpenStreetMap);
 *   2. список датчиков выбранного объекта и их последние показания;
 *   3. прогноз пожарного риска.
 *
 * Данные берутся с сервера по адресам /api/... и обновляются
 * автоматически каждые несколько секунд («опрос», polling).
 */

"use strict";

// ---------------------------------------------------------------------------
// Настройки
// ---------------------------------------------------------------------------

const REFRESH_INTERVAL_MS = {
  objects: 5000,   // точки на карте
  sensors: 3000,   // показания выбранного объекта
  forecast: 5000,  // блок прогноза
  status: 2000,    // часы эмуляции и последняя тревога
};

// С какого масштаба карты показывать части коллекторов (уровень 3)
const ZOOM_TO_SHOW_OBJECT_PARTS = 13;


//количество факторов на объекте
const max_count_factors_per_object_on_pzge = 20;

// Подписи состояний датчика
const SENSOR_STATUS_LABELS = {
  alarm: "Тревога",
  warning: "Внимание",
  presence: "Люди / работы",
  ok: "Норма",
  nodata: "Нет данных",
  silent: "Показаний не было",
};

// Цвета берём из CSS-переменных, чтобы они совпадали с оформлением страницы
const pageStyles = getComputedStyle(document.documentElement);
const MARKER_COLORS = {
  normal: pageStyles.getPropertyValue("--pipe").trim(),
  warning: pageStyles.getPropertyValue("--warn").trim(),
  alarm: pageStyles.getPropertyValue("--alarm").trim(),
  risk: pageStyles.getPropertyValue("--fire").trim(),
  outline: pageStyles.getPropertyValue("--panel").trim(),
};

// ---------------------------------------------------------------------------
// Состояние страницы
// ---------------------------------------------------------------------------

const config = window.DISPATCHER_CONFIG;
let map = null;
let collectorsLayer = null;   // коллекторы (уровень 2)
let objectPartsLayer = null;  // части коллекторов (уровень 3)
const markersByObjectId = new Map();
const objectsById = new Map();

let selectedObjectId = null;
let lastSensorsResponse = null;
let sensorsTimer = null;

// ---------------------------------------------------------------------------
// Общие помощники
// ---------------------------------------------------------------------------

/** Экранирует текст, чтобы его можно было безопасно вставить в HTML. */
function escapeHtml(text) {
  const element = document.createElement("span");
  element.textContent = text == null ? "" : String(text);
  return element.innerHTML;
}

/** Запрашивает JSON с сервера. Если сессия истекла — отправляет на страницу входа. */
async function fetchJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (response.redirected && response.url.includes("/login")) {
    window.location.href = response.url;
    return null;
  }
  if (!response.ok) {
    throw new Error(`Сервер ответил ${response.status} на ${url}`);
  }
  return response.json();
}

/** Переводит вероятность 0..1 в проценты с одним знаком после запятой. */
function formatPercent(probability) {
  return `${(probability * 100).toFixed(1)}%`;
}

/** Запускает функцию сразу и потом повторяет её с заданным интервалом. */
function runRepeatedly(task, intervalMs) {
  const safeTask = () => task().catch((error) => console.error(error));
  safeTask();
  return setInterval(safeTask, intervalMs);
}

// ---------------------------------------------------------------------------
// Блок 1. Карта
// ---------------------------------------------------------------------------

function initMap() {
  map = L.map("map", { zoomControl: true }).setView(config.mapCenter, config.mapZoom);

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '<a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);

  collectorsLayer = L.layerGroup().addTo(map);
  objectPartsLayer = L.layerGroup();

  // Части коллекторов показываем только при достаточном приближении,
  // иначе точки сливаются в кашу
  map.on("zoomend", updatePartsVisibility);
  updatePartsVisibility();
}

function updatePartsVisibility() {
  const showParts = map.getZoom() >= ZOOM_TO_SHOW_OBJECT_PARTS;
  if (showParts && !map.hasLayer(objectPartsLayer)) {
    map.addLayer(objectPartsLayer);
  } else if (!showParts && map.hasLayer(objectPartsLayer)) {
    map.removeLayer(objectPartsLayer);
  }
}

/** Определяет, каким цветом рисовать объект. Самое важное состояние побеждает. */
function getObjectState(mapObject) {
  if (mapObject.is_high_risk) return "risk";
  if (mapObject.alarms > 0) return "alarm";
  if (mapObject.warnings > 0) return "warning";
  return "normal";
}

function getMarkerStyle(mapObject) {
  const state = getObjectState(mapObject);
  const isCollector = mapObject.level <= 2;
  const isSelected = mapObject.id === selectedObjectId;

  return {
    radius: isCollector ? 10 : 6,
    color: isSelected ? MARKER_COLORS.alarm : MARKER_COLORS.outline,
    weight: isSelected ? 4 : 2,
    fillColor: MARKER_COLORS[state],
    fillOpacity: 0.95,
  };
}

function buildTooltipText(mapObject) {
  const lines = [
    `<strong>${escapeHtml(mapObject.name)}</strong>`,
    `${escapeHtml(mapObject.kind_label)}, датчиков: ${mapObject.channels_count}`,
  ];
  if (mapObject.alarms) lines.push(`Датчиков в тревоге: ${mapObject.alarms}`);
  if (mapObject.probability != null) lines.push(`Риск пожара: ${formatPercent(mapObject.probability)}`);
  return lines.join("<br>");
}

async function refreshObjects() {
  const objects = await fetchJson("/api/objects");
  if (!objects) return;

  for (const mapObject of objects) {
    objectsById.set(mapObject.id, mapObject);

    // Район (уровень 1) — это вся территория, отдельной точкой его не рисуем
    const hasCoordinates = mapObject.lat != null && mapObject.lon != null;
    if (mapObject.level < 2 || !hasCoordinates) continue;

    let marker = markersByObjectId.get(mapObject.id);
    if (!marker) {
      marker = L.circleMarker([mapObject.lat, mapObject.lon], getMarkerStyle(mapObject));
      marker.on("click", () => selectObject(mapObject.id));
      const targetLayer = mapObject.level <= 2 ? collectorsLayer : objectPartsLayer;
      marker.addTo(targetLayer);
      markersByObjectId.set(mapObject.id, marker);
    }

    marker.setLatLng([mapObject.lat, mapObject.lon]);
    marker.setStyle(getMarkerStyle(mapObject));
    marker.bindTooltip(buildTooltipText(mapObject), { direction: "top", offset: [0, -6] });

    // Объекты с риском и тревогой — поверх остальных
    if (getObjectState(mapObject) !== "normal") marker.bringToFront();
  }
}

// ---------------------------------------------------------------------------
// Блок 2. Датчики выбранного объекта
// ---------------------------------------------------------------------------

function selectObject(objectId) {
  selectedObjectId = objectId;

  // Перерисовываем обводку: у выбранного объекта она толще
  for (const [id, marker] of markersByObjectId) {
    marker.setStyle(getMarkerStyle(objectsById.get(id)));
  }

  document.getElementById("sensor-filters").hidden = false;
  document.getElementById("sensors-body").innerHTML = '<p class="empty-state">Загрузка датчиков…</p>';

  // Пока объект выбран, его показания обновляются сами
  clearInterval(sensorsTimer);
  sensorsTimer = runRepeatedly(() => loadSensors(objectId), REFRESH_INTERVAL_MS.sensors);
}

async function loadSensors(objectId) {
  // Если диспетчеру нужны только датчики с показаниями, просим сервер
  // не присылать остальные: на больших объектах их тысячи
  const onlyWithReadings = document.getElementById("sensor-only-active").checked;
  const url = `/api/objects/${objectId}/sensors${onlyWithReadings ? "?only_active=1" : ""}`;
  const response = await fetchJson(url);
  if (!response || objectId !== selectedObjectId) return; // пока грузилось, выбрали другой объект

  lastSensorsResponse = response;
  fillSystemFilter(response.sensors);
  renderSensors();
}

/** Заполняет выпадающий список подсистем теми, что есть на объекте. */
function fillSystemFilter(sensors) {
  const select = document.getElementById("sensor-system-filter");
  const currentValue = select.value;
  const systemNames = [...new Set(sensors.map((sensor) => sensor.system_type))].sort();

  select.innerHTML = '<option value="">Все подсистемы</option>' +
    systemNames.map((name) => `<option>${escapeHtml(name)}</option>`).join("");
  select.value = systemNames.includes(currentValue) ? currentValue : "";
}

function renderSensors() {
  if (!lastSensorsResponse) return;
  const { object, sensors, prediction } = lastSensorsResponse;

  // Заголовок блока
  document.getElementById("sensors-title").textContent = object.name;
  document.getElementById("sensors-subtitle").textContent =
    `${object.kind_label}. Датчиков: ${lastSensorsResponse.total_sensors}, ` +
    `с показаниями: ${lastSensorsResponse.sensors_with_readings}`;

  // Фильтры, выбранные диспетчером
  const searchText = document.getElementById("sensor-search").value.trim().toLowerCase();
  const systemFilter = document.getElementById("sensor-system-filter").value;
  const onlyWithReadings = document.getElementById("sensor-only-active").checked;

  const visibleSensors = sensors.filter((sensor) => {
    if (onlyWithReadings && sensor.status === "silent") return false;
    if (systemFilter && sensor.system_type !== systemFilter) return false;
    if (searchText && !sensor.name.toLowerCase().includes(searchText)) return false;
    return true;
  });

  const body = document.getElementById("sensors-body");
  const predictionHtml = (prediction ? buildObjectPredictionHtml(prediction) : "")
    + buildHotWorksHtml(lastSensorsResponse.hot_works || []);

  if (lastSensorsResponse.total_sensors === 0) {
    body.innerHTML = predictionHtml + '<p class="empty-state">К этому объекту не привязан ни один датчик.</p>';
    return;
  }
  if (visibleSensors.length === 0) {
    const hint = onlyWithReadings
      ? "Показаний ещё не было. Запустите эмуляцию в настройках или снимите галочку «Только с показаниями»."
      : "Под фильтр не попал ни один датчик.";
    body.innerHTML = predictionHtml + `<p class="empty-state">${hint}</p>`;
    return;
  }

  const rowsHtml = visibleSensors.map((sensor) => `
      <tr class="sensor-row status-${sensor.status}">
        <td>
          <div class="sensor-name">${escapeHtml(sensor.name)}</div>
          <div class="sensor-meta">${escapeHtml(sensor.sensor_type)}, ${escapeHtml(sensor.object_name)}</div>
        </td>
        <td class="sensor-value">
          <div>${sensor.value == null ? "—" : escapeHtml(sensor.value)}</div>
          <div class="sensor-meta">${sensor.time ? escapeHtml(sensor.time.slice(11)) : ""}</div>
        </td>
        <td><span class="status-pill status-${sensor.status}">${SENSOR_STATUS_LABELS[sensor.status]}</span></td>
      </tr>`).join("");

  body.innerHTML = predictionHtml + `
    <table class="sensor-table">
      <thead><tr><th>Датчик</th><th>Показание</th><th>Состояние</th></tr></thead>
      <tbody>${rowsHtml}</tbody>
    </table>`;
}

function buildObjectPredictionHtml(prediction) {
  const levelClass = prediction.is_high_risk ? "is-high" : "is-normal";
  const levelText = prediction.is_high_risk ? "Повышенный риск пожара" : "Риск в пределах нормы";
  return `
    <div class="object-prediction ${levelClass}">
      <strong>${levelText}</strong>
      <span>${formatPercent(prediction.probability)}</span>
    </div>`;
}

/** Горячие работы на объекте: идущие сейчас и запланированные на ближайшие сутки. */
function buildHotWorksHtml(hotWorks) {
  if (!hotWorks.length) return "";
  const itemsHtml = hotWorks.map((work) => `
      <li class="${work.is_active ? "is-active" : ""}">
        <strong>${work.is_active ? "Идут сейчас" : "Запланированы"}:</strong>
        ${escapeHtml(work.work_type)}, ${escapeHtml(work.start)} — ${escapeHtml(work.end.slice(11))}
        <span class="sensor-meta">${escapeHtml(work.object_name)}</span>
      </li>`).join("");
  return `<ul class="hot-works">${itemsHtml}</ul>`;
}

// ---------------------------------------------------------------------------
// Блок 3. Прогноз
// ---------------------------------------------------------------------------

async function refreshForecast() {
  const forecast = await fetchJson("/api/forecast");
  if (!forecast) return;

  const body = document.getElementById("forecast-body");
  const subtitle = document.getElementById("forecast-subtitle");

  // Прогноз выключен в настройках
  if (!forecast.enabled) {
    subtitle.textContent = "Выключен";
    const link = config.canManageSettings
      ? ` <a href="${config.settingsModelUrl}">Включить в настройках</a>.`
      : " Обратитесь к администратору.";
    body.innerHTML = `<p class="empty-state">Модель прогнозирования не подключена.${link}</p>`;
    return;
  }

  // Прогноз включён, но данных ещё нет
  if (!forecast.objects.length) {
    subtitle.textContent = forecast.message || "Ожидание данных";
    const link = config.canManageSettings
      ? ` <a href="${config.settingsEmulationUrl}">Запустить эмуляцию</a>.`
      : "";
    body.innerHTML =
      `<p class="empty-state">Прогноз появится после первого полного часа данных с датчиков.${link}</p>`;
    return;
  }

  const highRiskObjects = forecast.objects.filter((item) => item.is_high_risk);
  console.log('highRiskObjects forecast', forecast);
  subtitle.textContent = `За час ${forecast.hour || ""}`;

  body.innerHTML = buildForecastSummaryHtml(highRiskObjects.length, forecast.threshold)
    + buildRiskLadderHtml(forecast.objects, forecast.threshold);

  // Нажатие на строку прогноза выбирает объект и показывает его на карте
  body.querySelectorAll("[data-object-id]").forEach((row) => {
    const objectId = Number(row.dataset.objectId);
    row.addEventListener("click", () => focusObject(objectId));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter") focusObject(objectId);
    });
  });
}

function buildForecastSummaryHtml(highRiskCount, threshold) {
  const thresholdText = threshold != null ? ` Порог срабатывания — ${formatPercent(threshold)}.` : "";
  if (highRiskCount === 0) {
    return `<p class="forecast-summary is-calm">Повышенного риска нет.${thresholdText}</p>`;
  }
  return `<p class="forecast-summary is-alert">Повышенный риск: ${highRiskCount} ${pluralObjects(highRiskCount)}.${thresholdText}</p>`;
}

function pluralObjects(count) {
  const lastTwo = count % 100;
  const last = count % 10;
  if (lastTwo >= 11 && lastTwo <= 14) return "объектов";
  if (last === 1) return "объект";
  if (last >= 2 && last <= 4) return "объекта";
  return "объектов";
}

/**
 * ОЧЕНЬ ВАЖНАЯ ЧАСТЬ!!!
 * «Лестница риска»: объекты по убыванию вероятности.
 * Длина полосы — вероятность; вертикальная черта — порог модели.
 * Шкалу растягиваем так, чтобы порог был примерно на трети ширины:
 * иначе маленькие вероятности сливаются в ноль.
 */
function buildRiskLadderHtml(objects, threshold) {
  const largestProbability = Math.max(...objects.map((item) => item.probability));
  const scaleMaximum = Math.max(largestProbability, (threshold || 0.1) * 3, 0.01);
  const thresholdPosition = threshold != null ? (threshold / scaleMaximum) * 100 : null;

  const rowsHtml = objects.map((item) => {
    const barWidth = Math.min(100, (item.probability / scaleMaximum) * 100);
    const factorsHtml = item.is_high_risk && item.factors.length
      ? `<ul class="risk-factors">${item.factors.slice(0, max_count_factors_per_object_on_pzge).map((factor) =>
          `<li>${escapeHtml(factor.label)}: <b>${escapeHtml(factor.value)}</b></li>`).join("")}</ul>`
      : "";

    return `
      <li class="risk-row ${item.is_high_risk ? "is-high" : ""}" data-object-id="${item.id}" tabindex="0">
        <div class="risk-row-top">
          <span class="risk-name">${escapeHtml(item.name)}</span>
          <span class="risk-value">${formatPercent(item.probability)}</span>
        </div>
        <div class="risk-track">
          <div class="risk-bar" style="width:${barWidth}%"></div>
          ${thresholdPosition != null ? `<div class="risk-threshold" style="left:${thresholdPosition}%"></div>` : ""}
        </div>
        ${factorsHtml}
      </li>`;
  }).join("");

  return `<ol class="risk-ladder">${rowsHtml}</ol>`;
}

/** Выбирает объект и приближает к нему карту. */
function focusObject(objectId) {
  const mapObject = objectsById.get(objectId);
  if (mapObject && mapObject.lat != null) {
    const zoom = mapObject.level >= 3 ? Math.max(map.getZoom(), ZOOM_TO_SHOW_OBJECT_PARTS + 1) : map.getZoom();
    map.flyTo([mapObject.lat, mapObject.lon], zoom, { duration: 0.6 });
  }
  selectObject(objectId);
}

// ---------------------------------------------------------------------------
// Шапка: часы потока данных и последняя тревога
// ---------------------------------------------------------------------------

const EMULATION_STATUS_TEXT = {
  running: "Идёт поток данных",
  paused: "Поток на паузе",
  finished: "Журнал проигран",
  stopped: "Поток остановлен",
  error: "Ошибка эмуляции",
};

async function refreshStatus() {
  const status = await fetchJson("/api/status");
  if (!status) return;

  const emulation = status.emulation;
  const clockText = document.getElementById("sim-clock-text");
  const dot = document.getElementById("sim-dot");

  if (emulation.status === "idle") {
    clockText.textContent = "Данные не поступают";
  } else {
    clockText.textContent = `${EMULATION_STATUS_TEXT[emulation.status] || ""}: ${emulation.simulation_time || ""}`;
  }
  dot.className = `sim-dot is-${emulation.status}`;

  const lastAlarmBox = document.getElementById("last-alarm");
  const lastAlarm = status.alarms[0];
  if (lastAlarm) {
    lastAlarmBox.hidden = false;
    lastAlarmBox.innerHTML = `
      <span class="last-alarm-label">Последняя тревога</span>
      <span>${escapeHtml(lastAlarm.time.slice(11))}</span>
      <button type="button" class="link-button" data-object-id="${lastAlarm.object_id ?? ""}">
        ${escapeHtml(lastAlarm.object)}
      </button>
      <span>${escapeHtml(lastAlarm.sensor)}: ${escapeHtml(lastAlarm.value)}</span>`;
    const button = lastAlarmBox.querySelector("button");
    if (lastAlarm.object_id != null) {
      button.addEventListener("click", () => focusObject(lastAlarm.object_id));
    }
  } else {
    lastAlarmBox.hidden = true;
  }
}

// ---------------------------------------------------------------------------
// Запуск
// ---------------------------------------------------------------------------

function attachSensorFilterHandlers() {
  document.getElementById("sensor-search").addEventListener("input", renderSensors);
  document.getElementById("sensor-system-filter").addEventListener("change", renderSensors);
  // Смена этой галочки меняет запрос к серверу, поэтому загружаем заново
  document.getElementById("sensor-only-active").addEventListener("change", () => {
    if (selectedObjectId != null) loadSensors(selectedObjectId).catch(console.error);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initMap();
  attachSensorFilterHandlers();

  runRepeatedly(refreshObjects, REFRESH_INTERVAL_MS.objects);
  runRepeatedly(refreshForecast, REFRESH_INTERVAL_MS.forecast);
  runRepeatedly(refreshStatus, REFRESH_INTERVAL_MS.status);
});
