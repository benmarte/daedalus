/**
 * Pure helper for the deliver method→channel cascade.
 *
 * Derives the messaging METHOD name (e.g. "Slack") from a saved `cron.deliver`
 * value (e.g. "slack:tasks") given the notifications map returned by
 * /api/plugins/daedalus/meta/notifications ({ "Slack": [...], "Discord": [...] }).
 *
 * No React / DOM / SDK dependency — kept in its own module so it can be unit
 * tested in plain node (see test-derive.js). The cascade bug it supports:
 * the config modal's "Derive selected method" effect must ONLY set a method
 * from a non-empty deliver; it must never reset the user's in-progress choice.
 *
 * Returns the matching method key, or "" when there is no usable match.
 */
function deriveMethodFromDeliver(deliver, notifications) {
  if (!deliver || !notifications || !Object.keys(notifications).length) return "";
  var prefix = String(deliver).split(":")[0].toLowerCase();
  var methods = Object.keys(notifications);
  for (var i = 0; i < methods.length; i++) {
    var m = methods[i].toLowerCase();
    if (m === prefix || m.indexOf(prefix) === 0) return methods[i];
  }
  // Fall back to an exact key match (e.g. deliver is itself a method name).
  if (notifications[deliver]) return deliver;
  return "";
}

module.exports = { deriveMethodFromDeliver };
