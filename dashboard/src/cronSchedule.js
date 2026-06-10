/**
 * Pure helpers for the CronSchedule dropdown builder.
 *
 * parseSchedule(str) — parse a saved schedule string into dropdown state.
 * buildSchedule(state) — build a schedule string from dropdown state.
 *
 * Accepted formats (Hermes cron compatible):
 *   Recurring: "every <N>m|h|d"
 *   Weekly:    "M H * * D"     (D = 0–6, Sun–Sat)
 *   Monthly:   "M H DOM * *"    (DOM = 1–28)
 *   Custom:    anything else (raw passthrough)
 *
 * Legacy bare intervals ("60m", "2h", "3d") are parsed into the recurring
 * form so they survive a round-trip as "every Nx".
 */

function parseSchedule(str) {
  if (!str || typeof str !== "string") {
    return { unit: "Custom", n: null, dow: null, dom: null, hour: null, minute: null, raw: str || "" };
  }

  var s = str.trim();
  if (!s) {
    return { unit: "Custom", n: null, dow: null, dom: null, hour: null, minute: null, raw: s };
  }

  // Recurring interval: "every <N>m|h|d"
  var everyMatch = s.match(/^every\s+(\d+)\s*([mhd])$/i);
  if (everyMatch) {
    var n = everyMatch[1];
    var unitChar = everyMatch[2].toLowerCase();
    var unitMap = { m: "Minutes", h: "Hours", d: "Days" };
    return {
      unit: unitMap[unitChar],
      n: n,
      dow: null,
      dom: null,
      hour: null,
      minute: null,
      raw: null,
    };
  }

  // Legacy bare interval: "<N>m|h|d" (no "every" prefix)
  var bareMatch = s.match(/^(\d+)\s*([mhd])$/i);
  if (bareMatch) {
    var nBare = bareMatch[1];
    var unitCharBare = bareMatch[2].toLowerCase();
    var unitMapBare = { m: "Minutes", h: "Hours", d: "Days" };
    return {
      unit: unitMapBare[unitCharBare],
      n: nBare,
      dow: null,
      dom: null,
      hour: null,
      minute: null,
      raw: null,
    };
  }

  // Cron expression: 5 fields
  var fields = s.split(/\s+/);
  if (fields.length === 5) {
    var minute = fields[0];
    var hour = fields[1];
    var dom = fields[2];
    var month = fields[3];
    var dow = fields[4];

    // Weekly: DOW is a digit 0–6 and DOM is "*"
    if (/^[0-6]$/.test(dow) && dom === "*" && month === "*") {
      return {
        unit: "Weekly",
        n: null,
        dow: dow,
        dom: null,
        hour: hour,
        minute: minute,
        raw: null,
      };
    }

    // Monthly: DOM is a digit 1–28 and DOW is "*"
    var domNum = parseInt(dom, 10);
    if (!isNaN(domNum) && domNum >= 1 && domNum <= 28 && dow === "*" && month === "*") {
      return {
        unit: "Monthly",
        n: null,
        dow: null,
        dom: dom,
        hour: hour,
        minute: minute,
        raw: null,
      };
    }
  }

  // Anything else is Custom
  return {
    unit: "Custom",
    n: null,
    dow: null,
    dom: null,
    hour: null,
    minute: null,
    raw: s,
  };
}

function buildSchedule(state) {
  if (!state) return "";

  var unit = state.unit;

  if (unit === "Minutes") {
    return "every " + (state.n || "60") + "m";
  }
  if (unit === "Hours") {
    return "every " + (state.n || "1") + "h";
  }
  if (unit === "Days") {
    return "every " + (state.n || "1") + "d";
  }

  if (unit === "Weekly") {
    var m = state.minute || "0";
    var h = state.hour || "9";
    var d = state.dow || "1";
    return m + " " + h + " * * " + d;
  }

  if (unit === "Monthly") {
    var m2 = state.minute || "0";
    var h2 = state.hour || "9";
    var dom = state.dom || "1";
    return m2 + " " + h2 + " " + dom + " * *";
  }

  if (unit === "Custom") {
    return state.raw || "";
  }

  return "";
}

module.exports = { parseSchedule, buildSchedule };
