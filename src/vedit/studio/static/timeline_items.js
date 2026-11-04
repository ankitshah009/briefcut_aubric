"use strict";

(function (root) {
  function anchorEndCallout(item, beforeDuration, afterDuration) {
    if (!item || item.style !== "callout") return false;
    const start = Number(item.start), duration = Number(item.duration);
    if (!Number.isFinite(start) || !Number.isFinite(duration) || duration <= 0) return false;
    if (!Number.isFinite(beforeDuration) || !Number.isFinite(afterDuration) || beforeDuration <= 0 || afterDuration <= 0) return false;
    if (Math.abs(start + duration - beforeDuration) > 0.05) return false;
    const nextDuration = Math.min(duration, afterDuration);
    item.duration = Math.round(nextDuration * 100) / 100;
    item.start = Math.round(Math.max(0, afterDuration - nextDuration) * 100) / 100;
    return true;
  }

  const api = { anchorEndCallout };
  root.BriefcutTimelineItems = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof window === "undefined" ? globalThis : window);
