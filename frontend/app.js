/* Eco-Fraction dashboard.
   No framework, no CDN, no build step: the whole dashboard is three static files
   served by the API, so it also works with the network unplugged. The chart is
   drawn directly as SVG for the same reason. */

(function () {
  "use strict";

  var API = "/api/v1";
  var FAST_POLL_MS = 5000;
  var SERIES_POLL_MS = 15000;

  var DEMO_ADDRESS = "0xDEMO000000000000000000000000000000000001";

  var state = {
    assetId: null,
    asset: null,
    windowHours: 24,
    bucketMinutes: 15,
    series: null,
    offline: false
  };

  var el = {};
  ["asset-name", "asset-location", "asset-capacity", "status", "status-label",
   "status-detail", "plant-clock", "banner", "banner-text", "power-value",
   "power-meter", "power-percent", "energy-today", "peak-today", "yield-today",
   "energy-total", "co2-total", "irradiance", "module-temp", "ambient-temp",
   "chart", "chart-total", "tooltip", "readings-body", "reading-count",
   "trust-rate", "trust-rejected", "trust-samples", "attack-caught", "attack-rate",
   "merkle-root", "batch-readings", "anchor-target", "verify-link", "checks",
   "attack-buttons", "attack-result", "attack-verdict", "attack-story", "attack-checks",
   "holder-tokens", "holder-percent", "holder-invested", "holder-claimable",
   "holder-claimed", "token-sold", "token-supply", "token-price", "token-holders",
   "buy-btn", "claim-btn", "anchor-btn", "invest-note"
  ].forEach(function (id) {
    el[id] = document.getElementById(id);
  });

  /* ---------------- helpers ---------------- */

  function request(path) {
    return fetch(API + path, { headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) {
          return response.json().catch(function () { return {}; })
            .then(function (body) {
              throw new Error(body.detail || ("HTTP " + response.status));
            });
        }
        return response.json();
      });
  }

  function postJson(path, body) {
    return fetch(API + path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) throw new Error(data.detail || ("HTTP " + response.status));
        return data;
      });
    });
  }

  function offsetHours() {
    return state.asset ? state.asset.utc_offset_hours : 0;
  }

  /* Read a UTC instant as it appears on the plant's wall clock. */
  function plantDate(value) {
    var utc = new Date(value);
    return new Date(utc.getTime() + offsetHours() * 3600000);
  }

  function pad(n) { return n < 10 ? "0" + n : String(n); }

  function clockOf(value) {
    var d = plantDate(value);
    return pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds());
  }

  function hourMinuteOf(value) {
    var d = plantDate(value);
    return pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
  }

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function dayLabelOf(value) {
    var d = plantDate(value);
    return d.getUTCDate() + " " + MONTHS[d.getUTCMonth()];
  }

  function num(value, digits) {
    if (value === null || value === undefined || isNaN(value)) return "—";
    return Number(value).toFixed(digits === undefined ? 2 : digits);
  }

  function showBanner(message) {
    el["banner-text"].textContent = message;
    el.banner.hidden = false;
  }

  function hideBanner() {
    el.banner.hidden = true;
  }

  function svgNode(name, attrs) {
    var node = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.keys(attrs || {}).forEach(function (key) {
      node.setAttribute(key, attrs[key]);
    });
    return node;
  }

  /* ---------------- rendering: header + readouts ---------------- */

  function renderAsset(asset) {
    state.asset = asset;
    el["asset-name"].textContent = asset.name;
    el["asset-location"].textContent = asset.location_name;
    el["asset-capacity"].textContent =
      num(asset.dc_capacity_kw, 1) + " kWp DC / " + num(asset.ac_capacity_kw, 1) +
      " kW AC · tilt " + num(asset.tilt_deg, 0) + "°";
    document.title = asset.name + " — Eco-Fraction";
  }

  var STATUS_TEXT = {
    generating: "Generating",
    standby: "Standby",
    night: "Night",
    offline: "Meter offline",
    no_data: "No data"
  };

  function renderSummary(summary) {
    var stateName = summary.status;
    el.status.setAttribute("data-state", stateName);
    el["status-label"].textContent = STATUS_TEXT[stateName] || stateName;

    var detail = summary.status_detail;
    if (summary.data_age_seconds !== null && summary.data_age_seconds !== undefined) {
      detail += " · last reading " + num(summary.data_age_seconds, 0) + " s ago";
    }
    el["status-detail"].textContent = detail;

    el["power-value"].textContent = num(summary.current_power_w / 1000, 2);
    el["power-percent"].textContent = num(summary.current_power_percent_of_ac_capacity, 1) + "%";
    el["power-meter"].style.width =
      Math.max(0, Math.min(100, summary.current_power_percent_of_ac_capacity)) + "%";

    el["energy-today"].textContent = num(summary.energy_today_kwh, 2);
    el["peak-today"].textContent = num(summary.peak_power_today_w / 1000, 2);
    el["yield-today"].textContent = num(summary.specific_yield_today_kwh_per_kwp, 2);

    el["energy-total"].textContent = num(summary.energy_total_kwh, 1);
    el["co2-total"].textContent = num(summary.co2_avoided_total_kg, 1);

    el.irradiance.textContent = num(summary.poa_irradiance_w_m2, 0);
    el["module-temp"].textContent = num(summary.module_temp_c, 1);
    el["ambient-temp"].textContent = num(summary.ambient_temp_c, 1);

    el["reading-count"].textContent = summary.reading_count.toLocaleString("en-US");
  }

  function renderReadings(page) {
    var body = el["readings-body"];
    body.textContent = "";

    if (!page.readings.length) {
      var empty = document.createElement("tr");
      var cell = document.createElement("td");
      cell.className = "empty";
      cell.colSpan = 7;
      cell.textContent = "No readings stored yet. The simulator writes one every few seconds.";
      empty.appendChild(cell);
      body.appendChild(empty);
      return;
    }

    page.readings.forEach(function (reading) {
      var row = document.createElement("tr");
      if (!reading.is_trusted) row.className = "row--rejected";
      var cells = [
        { text: clockOf(reading.recorded_at), cls: "" },
        { text: num(reading.ac_power_w, 1), cls: "num" },
        { text: num(reading.poa_irradiance_w_m2, 1), cls: "num" },
        { text: num(reading.module_temp_c, 1), cls: "num" },
        { text: num(reading.energy_wh, 3), cls: "num" },
        { text: String(reading.sequence), cls: "num dim" },
        {
          text: reading.is_trusted ? "verified" : ("rejected: " + reading.trust_flags),
          cls: reading.is_trusted ? "verified-yes" : "verified-no"
        }
      ];
      cells.forEach(function (spec) {
        var td = document.createElement("td");
        td.className = spec.cls;
        td.textContent = spec.text;
        row.appendChild(td);
      });
      body.appendChild(row);
    });
  }

  /* ---------------- rendering: chart ---------------- */

  function niceCeiling(value) {
    if (value <= 0) return 1;
    var exponent = Math.pow(10, Math.floor(Math.log10(value)));
    var steps = [1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10];
    for (var i = 0; i < steps.length; i += 1) {
      if (steps[i] * exponent >= value) return steps[i] * exponent;
    }
    return 10 * exponent;
  }

  function tickStepHours(windowHours) {
    if (windowHours <= 6) return 1;
    if (windowHours <= 24) return 3;
    if (windowHours <= 72) return 12;
    return 24;
  }

  function drawChart() {
    var svg = el.chart;
    svg.textContent = "";

    var width = svg.clientWidth || svg.parentNode.clientWidth || 900;
    var height = svg.clientHeight || 300;
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);

    var series = state.series;
    if (!series || !series.points.length) {
      svg.appendChild(svgNode("rect", {
        x: 0, y: 0, width: width, height: height, fill: "#f6f8f7"
      }));
      var note = svgNode("text", {
        x: width / 2, y: height / 2, "text-anchor": "middle",
        fill: "#879592", "font-size": "13", "font-family": "system-ui, sans-serif"
      });
      note.textContent = "No readings in this window yet.";
      svg.appendChild(note);
      return;
    }

    var padLeft = 54, padRight = 16, padTop = 14, padBottom = 26;
    var plotW = Math.max(10, width - padLeft - padRight);
    var plotH = Math.max(10, height - padTop - padBottom);

    var nowMs = Date.now();
    var startMs = nowMs - series.window_hours * 3600000;
    var spanMs = Math.max(1, nowMs - startMs);

    var peak = series.points.reduce(function (acc, point) {
      return Math.max(acc, point.average_power_w);
    }, 0);
    var capacityW = state.asset ? state.asset.ac_capacity_kw * 1000 : peak;
    var yMax = niceCeiling(Math.max(peak * 1.08, capacityW * 0.25, 100));

    function xOf(ms) {
      return padLeft + ((ms - startMs) / spanMs) * plotW;
    }
    function yOf(watts) {
      return padTop + plotH - Math.max(0, Math.min(1, watts / yMax)) * plotH;
    }

    /* Night bands: contiguous buckets with no plane-of-array irradiance. */
    var bucketMs = series.bucket_minutes * 60000;
    var runStart = null;
    series.points.forEach(function (point, index) {
      var t = new Date(point.bucket_start).getTime();
      var isNight = point.average_poa_w_m2 <= 0.5;
      if (isNight && runStart === null) runStart = t;
      var isLast = index === series.points.length - 1;
      if ((!isNight || isLast) && runStart !== null) {
        var runEnd = isNight && isLast ? t + bucketMs : t;
        var x1 = Math.max(padLeft, xOf(runStart));
        var x2 = Math.min(padLeft + plotW, xOf(runEnd));
        if (x2 > x1) {
          svg.appendChild(svgNode("rect", {
            x: x1, y: padTop, width: x2 - x1, height: plotH,
            fill: "#c3ccca", "fill-opacity": "0.34"
          }));
        }
        runStart = null;
      }
    });

    /* Horizontal grid + y labels in kW. */
    var gridLines = 4;
    for (var g = 0; g <= gridLines; g += 1) {
      var watts = (yMax / gridLines) * g;
      var y = yOf(watts);
      svg.appendChild(svgNode("line", {
        x1: padLeft, y1: y, x2: padLeft + plotW, y2: y,
        stroke: g === 0 ? "#a8b4b1" : "#e2e8e6", "stroke-width": "1"
      }));
      var label = svgNode("text", {
        x: padLeft - 10, y: y + 4, "text-anchor": "end", fill: "#879592",
        "font-size": "11",
        "font-family": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
      });
      label.textContent = (watts / 1000).toFixed(yMax >= 4000 ? 0 : 1);
      svg.appendChild(label);
    }
    var yTitle = svgNode("text", {
      x: padLeft - 10, y: padTop - 3, "text-anchor": "end", fill: "#879592",
      "font-size": "10", "font-family": "system-ui, sans-serif"
    });
    yTitle.textContent = "kW";
    svg.appendChild(yTitle);

    /* Vertical ticks on whole plant-local hours. */
    var stepH = tickStepHours(series.window_hours);
    var firstTick = plantDate(startMs);
    firstTick.setUTCMinutes(0, 0, 0);
    var tickMs = firstTick.getTime() - offsetHours() * 3600000;
    while (tickMs <= nowMs) {
      var hourAtTick = plantDate(tickMs).getUTCHours();
      if (tickMs >= startMs && hourAtTick % stepH === 0) {
        var tx = xOf(tickMs);
        svg.appendChild(svgNode("line", {
          x1: tx, y1: padTop, x2: tx, y2: padTop + plotH,
          stroke: "#e2e8e6", "stroke-width": "1"
        }));
        var tickLabel = svgNode("text", {
          x: tx, y: padTop + plotH + 16, "text-anchor": "middle", fill: "#879592",
          "font-size": "10.5",
          "font-family": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
        });
        tickLabel.textContent = series.window_hours > 48
          ? dayLabelOf(tickMs)
          : hourMinuteOf(tickMs);
        svg.appendChild(tickLabel);
      }
      tickMs += 3600000;
    }

    /* Power curve: filled area plus a stroked outline. */
    var coords = series.points.map(function (point) {
      var t = new Date(point.bucket_start).getTime() + bucketMs / 2;
      return { x: xOf(t), y: yOf(point.average_power_w), point: point, t: t };
    }).filter(function (c) {
      return c.x >= padLeft - 2 && c.x <= padLeft + plotW + 2;
    });

    if (coords.length) {
      var baseline = yOf(0);
      var areaPath = "M " + coords[0].x.toFixed(2) + " " + baseline.toFixed(2);
      coords.forEach(function (c) {
        areaPath += " L " + c.x.toFixed(2) + " " + c.y.toFixed(2);
      });
      areaPath += " L " + coords[coords.length - 1].x.toFixed(2) + " " +
        baseline.toFixed(2) + " Z";
      svg.appendChild(svgNode("path", {
        d: areaPath, fill: "#e5a63a", "fill-opacity": "0.22", stroke: "none"
      }));

      var linePath = coords.map(function (c, i) {
        return (i === 0 ? "M " : " L ") + c.x.toFixed(2) + " " + c.y.toFixed(2);
      }).join("");
      svg.appendChild(svgNode("path", {
        d: linePath, fill: "none", stroke: "#c9861a", "stroke-width": "1.7",
        "stroke-linejoin": "round", "stroke-linecap": "round"
      }));

      var last = coords[coords.length - 1];
      svg.appendChild(svgNode("circle", {
        cx: last.x, cy: last.y, r: "3.2", fill: "#c9861a"
      }));
    }

    /* "Now" needle: the signature marker on the faceplate. */
    var nowX = xOf(nowMs);
    svg.appendChild(svgNode("line", {
      x1: nowX, y1: padTop - 4, x2: nowX, y2: padTop + plotH,
      stroke: "#0b7a66", "stroke-width": "1.6"
    }));
    svg.appendChild(svgNode("circle", {
      cx: nowX, cy: padTop - 6, r: "2.6", fill: "#0b7a66"
    }));

    el["chart-total"].textContent =
      num(series.total_energy_kwh, 2) + " kWh over " +
      (series.window_hours >= 48
        ? num(series.window_hours / 24, 0) + " days"
        : num(series.window_hours, 0) + " hours");

    attachHover(svg, coords, padLeft, padTop, plotW, plotH);
  }

  function attachHover(svg, coords, padLeft, padTop, plotW, plotH) {
    var marker = svgNode("circle", {
      r: "4.5", fill: "#10201e", "fill-opacity": "0", cx: "0", cy: "0"
    });
    svg.appendChild(marker);

    var overlay = svgNode("rect", {
      x: padLeft, y: padTop, width: plotW, height: plotH,
      fill: "transparent", style: "cursor:crosshair"
    });
    svg.appendChild(overlay);

    function hide() {
      el.tooltip.hidden = true;
      marker.setAttribute("fill-opacity", "0");
    }

    overlay.addEventListener("mousemove", function (event) {
      if (!coords.length) return;
      var box = svg.getBoundingClientRect();
      var x = event.clientX - box.left;
      var nearest = coords[0];
      var best = Infinity;
      coords.forEach(function (c) {
        var distance = Math.abs(c.x - x);
        if (distance < best) { best = distance; nearest = c; }
      });

      marker.setAttribute("cx", nearest.x);
      marker.setAttribute("cy", nearest.y);
      marker.setAttribute("fill-opacity", "1");

      el.tooltip.innerHTML =
        '<b>' + hourMinuteOf(nearest.t) + '</b> · ' + dayLabelOf(nearest.t) + '<br>' +
        '<span class="mono">' + num(nearest.point.average_power_w / 1000, 2) +
        '</span> kW average · peak <span class="mono">' +
        num(nearest.point.peak_power_w / 1000, 2) + '</span> kW<br>' +
        '<span class="mono">' + num(nearest.point.energy_wh / 1000, 3) +
        '</span> kWh · <span class="mono">' +
        num(nearest.point.average_poa_w_m2, 0) + '</span> W/m²';
      el.tooltip.hidden = false;
      el.tooltip.style.left = (nearest.x) + "px";
      el.tooltip.style.top = (nearest.y + 8) + "px";
    });

    overlay.addEventListener("mouseleave", hide);
  }

  /* ---------------- verification ---------------- */

  var CHECK_LABELS = {
    signature: "Device signature",
    sequence: "Replay protection",
    timestamp: "Timestamp window",
    night_generation: "No night generation",
    clear_sky_ceiling: "Clear-sky ceiling",
    rate_of_change: "Ramp rate",
    irradiance_consistency: "Reference cross-check",
    sustained_bias: "Sustained bias"
  };

  function renderTrust(report) {
    el["trust-rate"].textContent = num(report.trust_rate_percent, 2);
    el["trust-rejected"].textContent = report.rejected_count;
    el["trust-samples"].textContent = report.sample_count.toLocaleString("en-US");

    el["attack-caught"].textContent = report.attack_detected + " / " + report.attack_total;
    el["attack-rate"].textContent =
      report.attack_detection_rate_percent === null
        ? "no attacks run yet"
        : num(report.attack_detection_rate_percent, 1) + "%";

    if (report.latest_batch) {
      el["merkle-root"].textContent = report.latest_batch.merkle_root;
      el["batch-readings"].textContent = report.latest_batch.reading_count;
      el["anchor-target"].textContent = report.latest_batch.anchor_target;
      el["verify-link"].hidden = false;
      el["verify-link"].href =
        API + "/verify/batch/" + report.latest_batch.merkle_root;
      el["verify-link"].target = "_blank";
    } else {
      el["merkle-root"].textContent = "nothing anchored yet";
      el["batch-readings"].textContent = "0";
    }

    var failing = report.failing_checks || {};
    el.checks.textContent = "";
    Object.keys(CHECK_LABELS).forEach(function (key) {
      var failures = failing[key] || 0;
      var box = document.createElement("div");
      box.className = "check " + (failures ? "check--fail" : "check--pass");
      var mark = document.createElement("span");
      mark.className = "check__mark";
      mark.textContent = failures ? "\u2717" : "\u2713";
      var name = document.createElement("span");
      name.className = "check__name";
      name.textContent = CHECK_LABELS[key] + (failures ? " (" + failures + ")" : "");
      box.appendChild(mark);
      box.appendChild(name);
      el.checks.appendChild(box);
    });
  }

  function renderAttackResult(result) {
    var box = el["attack-result"];
    box.hidden = false;
    box.setAttribute("data-detected", String(result.detected));
    el["attack-verdict"].textContent = result.detected
      ? "\u2713 Caught \u2014 " + result.title + " rejected"
      : "\u2717 NOT DETECTED \u2014 " + result.title + " passed validation";
    el["attack-story"].textContent = result.story + " " + result.explanation;

    el["attack-checks"].textContent = "";
    result.checks.forEach(function (check) {
      var item = document.createElement("li");
      if (!check.passed) item.className = "fail";
      item.textContent =
        (check.passed ? "\u2713 " : "\u2717 ") +
        (CHECK_LABELS[check.name] || check.name) + " \u2014 " + check.detail;
      el["attack-checks"].appendChild(item);
    });
  }

  function buildAttackButtons(attacks) {
    el["attack-buttons"].textContent = "";
    attacks.forEach(function (attack) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn--attack";
      button.textContent = attack.title;
      button.title = attack.story;
      button.addEventListener("click", function () {
        button.disabled = true;
        postJson("/assets/" + state.assetId + "/attacks/" + attack.key)
          .then(function (result) {
            renderAttackResult(result);
            return Promise.all([refreshFast(), refreshTrust()]);
          })
          .catch(function (error) {
            el["attack-verdict"].textContent = "Injection failed: " + error.message;
            el["attack-result"].hidden = false;
          })
          .then(function () { button.disabled = false; });
      });
      el["attack-buttons"].appendChild(button);
    });
  }

  /* ---------------- investor ---------------- */

  function renderToken(ledger, holder) {
    el["token-sold"].textContent = ledger.circulating_supply.toLocaleString("en-US");
    el["token-supply"].textContent = ledger.total_supply.toLocaleString("en-US");
    el["token-price"].textContent = num(ledger.token_price_usdc, 2);
    el["token-holders"].textContent = ledger.holder_count;

    el["holder-tokens"].textContent = holder.token_balance.toLocaleString("en-US");
    el["holder-percent"].textContent = num(holder.ownership_percent, 4);
    el["holder-invested"].textContent = num(holder.investment_usdc, 2);
    el["holder-claimable"].textContent = num(holder.claimable_usdc, 6);
    el["holder-claimed"].textContent = num(holder.claimed_usdc, 6);

    if (ledger.paused) {
      el["invest-note"].textContent = "Distribution paused: " + ledger.pause_reason;
    }
  }

  function refreshToken() {
    if (!state.assetId) return Promise.resolve();
    return Promise.all([
      request("/assets/" + state.assetId + "/token"),
      request("/assets/" + state.assetId + "/token/holders/" + DEMO_ADDRESS)
    ]).then(function (results) {
      renderToken(results[0], results[1]);
    }).catch(function () { /* connectivity is reported by the fast poll */ });
  }

  function bindInvestorActions() {
    el["buy-btn"].addEventListener("click", function () {
      el["buy-btn"].disabled = true;
      el["invest-note"].textContent = "";
      postJson("/assets/" + state.assetId + "/token/purchase",
               { address: DEMO_ADDRESS, token_count: 1 })
        .then(function () {
          el["invest-note"].textContent = "Bought 1 token with mock USDC.";
          return refreshToken();
        })
        .catch(function (error) { el["invest-note"].textContent = error.message; })
        .then(function () { el["buy-btn"].disabled = false; });
    });

    el["claim-btn"].addEventListener("click", function () {
      el["claim-btn"].disabled = true;
      el["invest-note"].textContent = "";
      postJson("/assets/" + state.assetId + "/token/claim", { address: DEMO_ADDRESS })
        .then(function (result) {
          el["invest-note"].textContent =
            "Claimed " + num(result.claimed_usdc, 6) + " mock USDC.";
          return refreshToken();
        })
        .catch(function (error) { el["invest-note"].textContent = error.message; })
        .then(function () { el["claim-btn"].disabled = false; });
    });

    el["anchor-btn"].addEventListener("click", function () {
      el["anchor-btn"].disabled = true;
      el["invest-note"].textContent = "";
      postJson("/assets/" + state.assetId + "/anchor")
        .then(function (result) {
          el["invest-note"].textContent = result.anchored
            ? ("Anchored " + result.reading_count + " readings, root " +
               result.merkle_root.slice(0, 16) + "\u2026")
            : result.detail;
          return Promise.all([refreshTrust(), refreshToken()]);
        })
        .catch(function (error) { el["invest-note"].textContent = error.message; })
        .then(function () { el["anchor-btn"].disabled = false; });
    });
  }

  function refreshTrust() {
    if (!state.assetId) return Promise.resolve();
    return request("/assets/" + state.assetId + "/trust?window_hours=24")
      .then(renderTrust)
      .catch(function () { /* connectivity is reported by the fast poll */ });
  }

  /* ---------------- polling ---------------- */

  function refreshFast() {
    if (!state.assetId) return Promise.resolve();
    return Promise.all([
      request("/assets/" + state.assetId + "/summary"),
      request("/assets/" + state.assetId + "/readings?limit=12&order=desc")
    ]).then(function (results) {
      hideBanner();
      state.offline = false;
      renderSummary(results[0]);
      renderReadings(results[1]);
    }).catch(function (error) {
      state.offline = true;
      el.status.setAttribute("data-state", "error");
      el["status-label"].textContent = "API unreachable";
      el["status-detail"].textContent = error.message;
      showBanner(
        "Cannot reach the API at " + API + ". Start the server with: " +
        "uvicorn backend.main:app --reload"
      );
    });
  }

  function refreshSeries() {
    if (!state.assetId) return Promise.resolve();
    var query = "?window_hours=" + state.windowHours +
      "&bucket_minutes=" + state.bucketMinutes;
    return request("/assets/" + state.assetId + "/series" + query)
      .then(function (series) {
        state.series = series;
        drawChart();
      })
      .catch(function () { /* the fast poll already reports connectivity */ });
  }

  function tickClock() {
    if (!state.asset) return;
    el["plant-clock"].textContent = clockOf(Date.now());
  }

  /* ---------------- wiring ---------------- */

  function bindWindowButtons() {
    var buttons = document.querySelectorAll(".windows button");
    Array.prototype.forEach.call(buttons, function (button) {
      button.addEventListener("click", function () {
        Array.prototype.forEach.call(buttons, function (other) {
          other.classList.toggle("is-active", other === button);
        });
        state.windowHours = Number(button.dataset.hours);
        state.bucketMinutes = Number(button.dataset.bucket);
        refreshSeries();
      });
    });
  }

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(drawChart, 150);
  });

  function start() {
    bindWindowButtons();

    request("/assets").then(function (assets) {
      if (!assets.length) {
        showBanner("The API returned no assets. Restart the server to seed the demo asset.");
        return;
      }
      state.assetId = assets[0].id;
      renderAsset(assets[0]);
      tickClock();
      setInterval(tickClock, 1000);

      bindInvestorActions();
      refreshFast();
      refreshSeries();
      refreshTrust();
      refreshToken();

      request("/attacks").then(buildAttackButtons).catch(function () {
        el["attack-buttons"].textContent = "Attack console is disabled on this server.";
      });

      setInterval(refreshFast, FAST_POLL_MS);
      setInterval(refreshSeries, SERIES_POLL_MS);
      setInterval(refreshTrust, SERIES_POLL_MS);
      setInterval(refreshToken, SERIES_POLL_MS);
    }).catch(function (error) {
      showBanner(
        "Cannot reach the API at " + API + " (" + error.message +
        "). Start the server with: uvicorn backend.main:app --reload"
      );
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
